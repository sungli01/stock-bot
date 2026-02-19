"""
전종목 스캐너 모듈
- 전종목 스캔 루프
- 1차 필터: 주당 $1↑, 시총 $5천만↑, 5분봉 변동률 5%↑, 거래량 200%↑, 1만주↑
- 필터 통과 종목 → Redis channel:screened publish
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

from collector.market_data import MarketDataClient

logger = logging.getLogger(__name__)


def load_config() -> dict:
    """config.yaml 로드"""
    with open("config/config.yaml", "r") as f:
        return yaml.safe_load(f)


class StockScanner:
    """전종목 스캐너 — 1차 필터링 후 Redis publish"""

    def __init__(self, redis_client, config: Optional[dict] = None):
        self.redis = redis_client
        self.config = config or load_config()
        self.scanner_cfg = self.config["scanner"]
        self.market_data = MarketDataClient()

    def scan_once(self) -> list[dict]:
        """
        전종목 1회 스캔
        Returns: 필터 통과 종목 리스트
        """
        logger.info("🔍 전종목 스캔 시작...")
        tickers = self.market_data.get_all_tickers(
            min_price=self.scanner_cfg["min_price"],
            min_market_cap=self.scanner_cfg["min_market_cap"],
        )
        logger.info(f"  총 {len(tickers)}개 종목 조회됨")

        screened = []
        for t in tickers:
            result = self._check_ticker(t["ticker"])
            if result:
                screened.append(result)
                self._publish(result)
                logger.info(
                    f"  ✅ {result['ticker']} 통과 — "
                    f"변동 {result['change_pct']:.1f}%, "
                    f"거래량비 {result['volume_ratio']:.0f}%"
                )

        logger.info(f"🔍 스캔 완료: {len(screened)}/{len(tickers)} 종목 통과")
        return screened

    def _check_ticker(self, ticker: str) -> Optional[dict]:
        """
        개별 종목 필터 체크
        조건: 주당 $1↑, 시총 $5천만↑, 5분봉 변동률 5%↑, 거래량 200%↑, 1만주↑
        """
        snap = self.market_data.get_snapshot(ticker)
        if not snap:
            return None

        # 가격 필터
        if snap["price"] < self.scanner_cfg["min_price"]:
            return None

        # 시총 필터
        if snap.get("market_cap", 0) < self.scanner_cfg["min_market_cap"]:
            return None

        # 5분봉 변동률 필터
        if abs(snap["change_pct"]) < self.scanner_cfg["price_change_pct"]:
            return None

        # 거래량 필터 (절대량)
        if snap["volume"] < self.scanner_cfg["min_volume"]:
            return None

        # 거래량 급증 확인 (1분봉 기반)
        bars = self.market_data.get_bars(ticker, timeframe="1min", limit=30)
        if bars.empty or len(bars) < 10:
            return None

        avg_volume = bars["volume"].iloc[:-5].mean()  # 최근 5개 제외 평균
        recent_volume = bars["volume"].iloc[-5:].mean()  # 최근 5개 평균

        if avg_volume <= 0:
            return None

        volume_ratio = (recent_volume / avg_volume) * 100
        if volume_ratio < self.scanner_cfg["volume_spike_pct"]:
            return None

        # RSI 과매수 필터
        rsi_max = self.scanner_cfg.get("rsi_max", 70)
        rsi = self._calc_rsi(bars["close"], period=14)
        if rsi is not None and rsi > rsi_max:
            logger.debug(f"  ❌ {ticker} RSI {rsi:.1f} > {rsi_max} 과매수 제외")
            return None

        # 모든 필터 통과
        return {
            "ticker": ticker,
            "price": snap["price"],
            "change_pct": snap["change_pct"],
            "volume": snap["volume"],
            "volume_ratio": volume_ratio,
            "market_cap": snap.get("market_cap", 0),
            "prev_close": snap.get("prev_close", 0),
        }

    @staticmethod
    def _calc_rsi(closes, period: int = 14) -> Optional[float]:
        """1분봉 close 시리즈로 RSI 계산"""
        if closes is None or len(closes) < period + 1:
            return None
        deltas = closes.diff().dropna()
        gains = deltas.where(deltas > 0, 0.0)
        losses = (-deltas.where(deltas < 0, 0.0))
        avg_gain = gains.iloc[:period].mean()
        avg_loss = losses.iloc[:period].mean()
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains.iloc[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses.iloc[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _publish(self, data: dict):
        """Redis channel:screened 으로 publish (Redis 없으면 스킵)"""
        if self.redis is None:
            return
        try:
            self.redis and self.redis.publish("channel:screened", json.dumps(data))
        except Exception as e:
            logger.warning(f"Redis publish 실패: {e}")

    def run_loop(self, interval_sec: int = 60):
        """
        스캔 루프 — interval_sec 간격으로 반복
        """
        logger.info(f"📡 스캐너 루프 시작 (간격: {interval_sec}초)")
        while True:
            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"스캔 오류: {e}", exc_info=True)
            time.sleep(interval_sec)
