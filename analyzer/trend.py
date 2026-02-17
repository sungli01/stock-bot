"""
추세 판단 모듈
- EMA(5,20) 크로스
- MACD(12,26,9)
- RSI(14)
- 볼린저밴드
- 상승/하락/횡보 판단 + 강도(0-100)
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


@dataclass
class TrendResult:
    """추세 분석 결과"""
    direction: str       # "UP", "DOWN", "SIDEWAYS"
    strength: float      # 0-100
    indicators: dict     # 전체 지표 스냅샷

    # 개별 시그널
    ema_bullish: bool = False
    macd_bullish: bool = False
    rsi_value: float = 50.0
    bb_breakout: bool = False
    volume_surge: bool = False


class TrendAnalyzer:
    """기술적 추세 분석기"""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.ema_fast = cfg.get("ema_fast", 5)
        self.ema_slow = cfg.get("ema_slow", 20)
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.macd_fast = cfg.get("macd_fast", 12)
        self.macd_slow = cfg.get("macd_slow", 26)
        self.macd_signal = cfg.get("macd_signal", 9)

    def analyze(self, df: pd.DataFrame) -> Optional[TrendResult]:
        """
        DataFrame(OHLCV)으로 추세 분석 실행
        최소 26개 봉 필요 (MACD slow period)
        """
        if df is None or len(df) < self.macd_slow + self.macd_signal:
            logger.warning("데이터 부족 — 추세 분석 불가")
            return None

        close = df["close"]
        volume = df["volume"]

        # ── EMA 크로스 ────────────────────────────────────
        ema_fast = ta.ema(close, length=self.ema_fast)
        ema_slow = ta.ema(close, length=self.ema_slow)
        ema_bullish = False
        if ema_fast is not None and ema_slow is not None:
            ema_bullish = bool(ema_fast.iloc[-1] > ema_slow.iloc[-1])

        # ── MACD ──────────────────────────────────────────
        macd_df = ta.macd(close, fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        macd_bullish = False
        macd_val = macd_sig = macd_hist = 0.0
        if macd_df is not None and not macd_df.empty:
            cols = macd_df.columns
            macd_val = float(macd_df[cols[0]].iloc[-1] or 0)
            macd_sig = float(macd_df[cols[1]].iloc[-1] or 0)
            macd_hist = float(macd_df[cols[2]].iloc[-1] or 0)
            macd_bullish = macd_hist > 0

        # ── RSI ───────────────────────────────────────────
        rsi = ta.rsi(close, length=self.rsi_period)
        rsi_value = float(rsi.iloc[-1]) if rsi is not None and not rsi.empty else 50.0

        # ── 볼린저밴드 ────────────────────────────────────
        bbands = ta.bbands(close, length=20, std=2)
        bb_breakout = False
        bb_upper = bb_lower = bb_mid = 0.0
        if bbands is not None and not bbands.empty:
            cols = bbands.columns
            bb_lower = float(bbands[cols[0]].iloc[-1] or 0)
            bb_mid = float(bbands[cols[1]].iloc[-1] or 0)
            bb_upper = float(bbands[cols[2]].iloc[-1] or 0)
            bb_breakout = float(close.iloc[-1]) > bb_upper

        # ── 거래량 급증 ──────────────────────────────────
        avg_vol = volume.iloc[:-5].mean() if len(volume) > 5 else volume.mean()
        recent_vol = volume.iloc[-3:].mean()
        volume_surge = (recent_vol / avg_vol) > 2.0 if avg_vol > 0 else False

        # ── 추세 방향 + 강도 결정 ─────────────────────────
        bullish_signals = sum([ema_bullish, macd_bullish, rsi_value < self.rsi_overbought and rsi_value > self.rsi_oversold, bb_breakout])
        bearish_signals = sum([not ema_bullish, not macd_bullish, rsi_value > self.rsi_overbought])

        if bullish_signals >= 3:
            direction = "UP"
            strength = min(100, 40 + bullish_signals * 15 + (10 if volume_surge else 0))
        elif bearish_signals >= 3:
            direction = "DOWN"
            strength = min(100, 40 + bearish_signals * 15)
        else:
            direction = "SIDEWAYS"
            strength = 30 + abs(bullish_signals - bearish_signals) * 10

        # 지표 스냅샷
        indicators = {
            "ema_5": float(ema_fast.iloc[-1]) if ema_fast is not None else 0,
            "ema_20": float(ema_slow.iloc[-1]) if ema_slow is not None else 0,
            "rsi_14": rsi_value,
            "macd_value": macd_val,
            "macd_signal": macd_sig,
            "macd_histogram": macd_hist,
            "bollinger_upper": bb_upper,
            "bollinger_lower": bb_lower,
            "volume_ratio": round(recent_vol / avg_vol * 100, 1) if avg_vol > 0 else 0,
        }

        return TrendResult(
            direction=direction,
            strength=strength,
            indicators=indicators,
            ema_bullish=ema_bullish,
            macd_bullish=macd_bullish,
            rsi_value=rsi_value,
            bb_breakout=bb_breakout,
            volume_surge=volume_surge,
        )

    def is_trend_reversing(self, df: pd.DataFrame, min_signals: int = 2) -> bool:
        """
        추세 꺾임 판단 (매도 트리거)
        3개 중 min_signals개 이상 충족 시 True:
        1. 5EMA가 20EMA 하향 돌파
        2. MACD 히스토그램 음전환
        3. RSI 70 이상에서 하락 시작
        """
        result = self.analyze(df)
        if not result:
            return False

        reversal_signals = 0

        # 1. EMA 하향 돌파
        if not result.ema_bullish:
            reversal_signals += 1

        # 2. MACD 음전환
        if not result.macd_bullish:
            reversal_signals += 1

        # 3. RSI 과매수 후 하락
        if result.rsi_value > self.rsi_overbought:
            reversal_signals += 1

        return reversal_signals >= min_signals
