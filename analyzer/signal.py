"""
시그널 생성 모듈
- Redis channel:screened subscribe
- 기술지표 계산 → 시그널 생성 (BUY/SELL/STOP/WATCH)
- confidence 계산 (지표 가중치 기반)
- Redis channel:signal publish
"""
import json
import time
import logging
from typing import Optional

try:
    import redis
except ImportError:
    redis = None
import yaml

from analyzer.trend import TrendAnalyzer, TrendResult
from collector.market_data import MarketDataClient

logger = logging.getLogger(__name__)

# 시그널 타입
SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_STOP = "STOP"
SIGNAL_WATCH = "WATCH"

# 기본 지표 가중치 (학습으로 자동 조정됨)
DEFAULT_WEIGHTS = {
    "ema_cross": 0.25,
    "macd": 0.25,
    "rsi": 0.20,
    "volume": 0.30,
}


class SignalGenerator:
    """시그널 생성기 — 기술지표 기반 매수/매도 시그널"""

    def __init__(self, redis_client, config: Optional[dict] = None):
        self.redis = redis_client
        if config is None:
            with open("config/config.yaml", "r") as f:
                config = yaml.safe_load(f)
        self.config = config
        self.analyzer_cfg = config.get("analyzer", {})
        self.trend = TrendAnalyzer(self.analyzer_cfg)
        self.market_data = MarketDataClient()
        self.weights = self._load_weights()

    def _load_weights(self) -> dict:
        """Redis에서 최신 가중치 로드, 없으면 기본값"""
        if self.redis is not None:
            try:
                cached = self.redis.get("indicator_weights")
                if cached:
                    return json.loads(cached)
            except Exception:
                pass
        return DEFAULT_WEIGHTS.copy()

    def evaluate(self, ticker: str, screened_data: dict) -> Optional[dict]:
        """
        종목 평가 → 시그널 생성 (v5 전략)
        
        매수 조건 (형님 룰):
        1. 스냅샷에서 거래량 급등 감지 (scanner가 이미 필터링)
        2. 가격 변동률 10%+ → 추격 매수
        
        5분봉 BB는 매수 후 매도 판단에만 사용 (bb_trailing.py)
        """
        change_pct = screened_data.get("change_pct", 0)
        volume_ratio = screened_data.get("volume_ratio", 0)
        price = screened_data.get("price", 0)

        # 가격 10% 이상 급등 확인 (형님 전략 핵심)
        min_change = self.config.get("trading", {}).get("min_chase_change_pct", 10.0)
        if change_pct < min_change:
            logger.debug(f"{ticker} 가격 변동 {change_pct:+.1f}% < {min_change}% — 스킵")
            return None

        # 거래량 급증 확인 (스캐너에서 이미 필터링되지만 이중 체크)
        min_vol = self.config.get("screener", {}).get("volume_spike", 200)
        if volume_ratio < min_vol:
            logger.debug(f"{ticker} 거래량 {volume_ratio:.0f}% < {min_vol}% — 스킵")
            return None

        # confidence 계산: 가격 변동 + 거래량 기반
        # 가격 변동: 10%→50, 20%→70, 30%+→85
        price_score = min(85, 50 + (change_pct - 10) * 2)
        # 거래량: 200%→+5, 500%→+10, 999%→+15
        vol_score = min(15, (volume_ratio - 200) / 53)
        confidence = price_score + vol_score

        signal = {
            "ticker": ticker,
            "signal": SIGNAL_BUY,
            "confidence": round(confidence, 2),
            "price": price,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "trend_direction": "UP",
            "trend_strength": change_pct,
            "indicators": {
                "change_pct": change_pct,
                "volume_ratio": volume_ratio,
            },
        }

        logger.info(
            f"🚀 {ticker} → BUY (신뢰도 {confidence:.0f}%) "
            f"가격 {change_pct:+.1f}% 거래량 {volume_ratio:.0f}%"
        )
        return signal

    def _decide_signal(self, trend: TrendResult, screened: dict) -> tuple[Optional[str], float]:
        """
        추세 결과 + 스크리닝 데이터 → 시그널 타입과 confidence 결정
        """
        w = self.weights

        # 각 지표별 점수 (0~1)
        ema_score = 1.0 if trend.ema_bullish else 0.0
        macd_score = 1.0 if trend.macd_bullish else 0.0

        # RSI: 과매도 근처면 높은 점수, 과매수 근처면 낮은 점수
        if trend.rsi_value < self.analyzer_cfg.get("rsi_oversold", 30):
            rsi_score = 1.0  # 과매도 = 반등 기대
        elif trend.rsi_value > self.analyzer_cfg.get("rsi_overbought", 70):
            rsi_score = 0.0  # 과매수 = 위험
        else:
            rsi_score = 0.5 + (50 - trend.rsi_value) / 100  # 중립 근처

        # 거래량 급증 점수
        vol_ratio = screened.get("volume_ratio", 100)
        vol_score = min(1.0, (vol_ratio - 100) / 300)  # 200%→0.33, 400%→1.0

        # 가중 합산
        confidence = (
            w.get("ema_cross", 0.25) * ema_score +
            w.get("macd", 0.25) * macd_score +
            w.get("rsi", 0.20) * rsi_score +
            w.get("volume", 0.30) * vol_score
        ) * 100

        # 시그널 결정
        if trend.direction == "UP" and confidence >= 65:
            return SIGNAL_BUY, confidence
        elif trend.direction == "UP" and confidence >= 45:
            return SIGNAL_WATCH, confidence
        elif trend.direction == "DOWN" and confidence < 30:
            return SIGNAL_SELL, confidence
        else:
            return SIGNAL_WATCH, confidence

    def _publish_signal(self, signal: dict):
        """Redis channel:signal 로 publish (Redis 없으면 스킵)"""
        if self.redis is None:
            return
        try:
            self.redis and self.redis.publish("channel:signal", json.dumps(signal))
        except Exception as e:
            logger.warning(f"Redis publish 실패: {e}")

    def run_subscriber(self):
        """
        Redis channel:screened 구독 → 시그널 생성 → channel:signal publish
        """
        logger.info("📡 시그널 생성기 시작 — channel:screened 구독 중...")
        pubsub = self.redis.pubsub()
        pubsub.subscribe("channel:screened")

        for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
                ticker = data.get("ticker")
                if not ticker:
                    continue

                signal = self.evaluate(ticker, data)
                if signal and signal["signal"] in (SIGNAL_BUY, SIGNAL_SELL, SIGNAL_STOP):
                    self._publish_signal(signal)

            except Exception as e:
                logger.error(f"시그널 생성 오류: {e}", exc_info=True)
