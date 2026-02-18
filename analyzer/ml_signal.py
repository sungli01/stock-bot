"""
ML 모델 기반 시그널 생성기
- 규칙 기반 signal.py와 동일 인터페이스
- confidence = ML 모델 예측 확률
- 데이터 부족 시 규칙 기반 자동 fallback
"""
import logging
from typing import Optional

try:
    import redis
except ImportError:
    redis = None

from knowledge.ml_model import get_ml_model

logger = logging.getLogger(__name__)


class MLSignalGenerator:
    """ML 모델 기반 시그널 생성기"""

    def __init__(self, redis_client: redis.Redis, config: dict = None):
        self.redis = redis_client
        self.config = config or {}
        self.ml_model = get_ml_model()

    def evaluate(self, ticker: str, screened_data: dict) -> Optional[dict]:
        """
        ML 모델로 종목 평가 → 시그널 생성
        규칙 기반 signal.py와 동일 반환 형식
        
        Returns: {"ticker", "signal", "confidence", "source", ...} or None
        """
        indicators = screened_data.get("indicators", screened_data)

        # ML 예측
        win_prob = self.ml_model.predict(indicators)

        if win_prob is None:
            # 모델 미준비 → fallback
            logger.debug(f"{ticker}: ML 모델 미준비, 규칙 기반 fallback")
            return self._fallback_evaluate(ticker, screened_data)

        # 시그널 결정
        if win_prob >= 65:
            signal_type = "BUY"
        elif win_prob >= 45:
            signal_type = "WATCH"
        elif win_prob < 30:
            signal_type = "SELL"
        else:
            signal_type = "WATCH"

        signal = {
            "ticker": ticker,
            "signal": signal_type,
            "confidence": round(win_prob, 2),
            "source": "ml_xgboost",
            "price": screened_data.get("price", 0),
            "change_pct": screened_data.get("change_pct", 0),
            "volume_ratio": screened_data.get("volume_ratio", 0),
            "indicators": indicators,
        }

        logger.info(
            f"🤖 ML {ticker} → {signal_type} (신뢰도 {win_prob:.0f}%)"
        )
        return signal

    def _fallback_evaluate(self, ticker: str, screened_data: dict) -> Optional[dict]:
        """규칙 기반 fallback (signal.py의 간소화 버전)"""
        indicators = screened_data.get("indicators", screened_data)

        scores = []
        # EMA 크로스
        if indicators.get("ema_5", 0) > indicators.get("ema_20", 0):
            scores.append(1.0)
        else:
            scores.append(0.0)

        # MACD
        if indicators.get("macd_histogram", 0) > 0:
            scores.append(1.0)
        else:
            scores.append(0.0)

        # RSI
        rsi = indicators.get("rsi_14", 50)
        if 30 < rsi < 70:
            scores.append(0.7)
        elif rsi <= 30:
            scores.append(1.0)
        else:
            scores.append(0.0)

        # 거래량
        vol = screened_data.get("volume_ratio", 100)
        scores.append(min(1.0, (vol - 100) / 300))

        confidence = sum(scores) / len(scores) * 100 if scores else 50

        if confidence >= 65:
            signal_type = "BUY"
        elif confidence < 30:
            signal_type = "SELL"
        else:
            signal_type = "WATCH"

        return {
            "ticker": ticker,
            "signal": signal_type,
            "confidence": round(confidence, 2),
            "source": "rule_fallback",
            "price": screened_data.get("price", 0),
            "change_pct": screened_data.get("change_pct", 0),
            "volume_ratio": screened_data.get("volume_ratio", 0),
            "indicators": indicators,
        }
