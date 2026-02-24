"""
이벤트 감지 모듈
1차 상승, 쿨링 구간, 2차 상승 감지
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class EventType(Enum):
    NONE = "none"
    FIRST_SURGE = "first_surge"       # 1차 상승
    COOLING = "cooling"                # 쿨링 구간
    SECOND_SURGE = "second_surge"      # 2차 상승 (매수 트리거)
    THIRD_SURGE = "third_surge"        # 3차 이상 상승


@dataclass
class SurgeEvent:
    event_type: EventType
    start_idx: int
    end_idx: Optional[int]
    start_price: float
    peak_price: float
    trough_price: Optional[float]
    change_pct: float
    volume_ratio: float
    bb_breakout: bool = False
    timestamp_start: Optional[str] = None
    timestamp_peak: Optional[str] = None


@dataclass
class DetectionResult:
    events: list = field(default_factory=list)
    first_surge: Optional[SurgeEvent] = None
    cooling: Optional[SurgeEvent] = None
    second_surge: Optional[SurgeEvent] = None
    third_surge: Optional[SurgeEvent] = None
    buy_signal: bool = False
    buy_idx: Optional[int] = None
    buy_price: Optional[float] = None


class EventDetector:
    def __init__(
        self,
        # 1차 상승 기준
        first_surge_pct: float = 0.10,       # 5분 내 +10%
        first_surge_volume_ratio: float = 3.0, # 평균 대비 300%
        first_surge_window: int = 5,           # 5분 윈도우
        # 쿨링 기준
        cooling_drop_pct: float = 0.05,       # 피크 대비 -5%
        cooling_min_duration: int = 15,        # 15분 이상
        # 2차 상승 기준
        second_surge_pct: float = 0.05,       # 쿨링 저점 대비 +5%
        require_bb_breakout: bool = True,      # BB 상단 돌파 필요
    ):
        self.first_surge_pct = first_surge_pct
        self.first_surge_volume_ratio = first_surge_volume_ratio
        self.first_surge_window = first_surge_window
        self.cooling_drop_pct = cooling_drop_pct
        self.cooling_min_duration = cooling_min_duration
        self.second_surge_pct = second_surge_pct
        self.require_bb_breakout = require_bb_breakout

    def detect(self, df: pd.DataFrame) -> DetectionResult:
        """
        1분봉 DataFrame에서 이벤트 감지
        df: feature_engine.compute_all() 적용된 DataFrame
        """
        result = DetectionResult()

        if df.empty or len(df) < 20:
            return result

        # 1. 1차 상승 감지
        first_surge = self._detect_first_surge(df)
        if first_surge is None:
            return result

        result.first_surge = first_surge
        result.events.append(first_surge)

        # 2. 쿨링 구간 감지
        cooling = self._detect_cooling(df, first_surge)
        if cooling is None:
            return result

        result.cooling = cooling
        result.events.append(cooling)

        # 3. 2차 상승 감지
        second_surge = self._detect_second_surge(df, cooling)
        if second_surge is None:
            return result

        result.second_surge = second_surge
        result.events.append(second_surge)

        # 매수 신호
        result.buy_signal = True
        result.buy_idx = second_surge.start_idx
        result.buy_price = second_surge.start_price

        # 4. 3차 이상 상승 감지 (2차 이후 추가 상승)
        third_surge = self._detect_third_surge(df, second_surge)
        if third_surge:
            result.third_surge = third_surge
            result.events.append(third_surge)

        return result

    def _detect_first_surge(self, df: pd.DataFrame) -> Optional[SurgeEvent]:
        """1차 상승 감지: 5분 내 +10% 이상 + 거래량 300% 이상"""
        closes = df["close"].values
        volumes = df["volume"].values
        vol_ratios = df.get("volume_ratio", pd.Series(np.ones(len(df)))).values
        bb_breakout = df.get("bb_breakout_upper", pd.Series(np.zeros(len(df)))).values

        n = len(df)
        window = self.first_surge_window

        for i in range(window, n):
            start_idx = max(0, i - window)
            start_price = closes[start_idx]
            current_price = closes[i]

            if start_price <= 0:
                continue

            change_pct = (current_price - start_price) / start_price

            if change_pct >= self.first_surge_pct:
                # 거래량 확인
                avg_vol_ratio = np.mean(vol_ratios[start_idx:i+1])
                if avg_vol_ratio >= self.first_surge_volume_ratio:
                    # 피크 찾기
                    peak_idx = np.argmax(closes[i:min(i+30, n)]) + i
                    peak_price = closes[peak_idx]

                    ts_start = str(df["timestamp"].iloc[start_idx]) if "timestamp" in df.columns else None
                    ts_peak = str(df["timestamp"].iloc[peak_idx]) if "timestamp" in df.columns else None

                    return SurgeEvent(
                        event_type=EventType.FIRST_SURGE,
                        start_idx=start_idx,
                        end_idx=peak_idx,
                        start_price=start_price,
                        peak_price=peak_price,
                        trough_price=None,
                        change_pct=change_pct,
                        volume_ratio=avg_vol_ratio,
                        bb_breakout=bool(bb_breakout[i]),
                        timestamp_start=ts_start,
                        timestamp_peak=ts_peak,
                    )

        return None

    def _detect_cooling(self, df: pd.DataFrame, first_surge: SurgeEvent) -> Optional[SurgeEvent]:
        """쿨링 구간 감지: 1차 피크 대비 -5% 이상 하락 + 15분 이상 지속"""
        closes = df["close"].values
        peak_idx = first_surge.end_idx
        peak_price = first_surge.peak_price
        n = len(df)

        if peak_idx is None or peak_idx >= n - self.cooling_min_duration:
            return None

        # 피크 이후 하락 구간 탐색
        trough_price = peak_price
        trough_idx = peak_idx
        cooling_start_idx = peak_idx
        cooling_confirmed = False

        for i in range(peak_idx + 1, min(peak_idx + 120, n)):  # 최대 120분 탐색
            current_price = closes[i]
            drop_pct = (peak_price - current_price) / peak_price

            if drop_pct >= self.cooling_drop_pct:
                # 쿨링 시작 확인
                if not cooling_confirmed:
                    cooling_start_idx = i
                    cooling_confirmed = True

                if current_price < trough_price:
                    trough_price = current_price
                    trough_idx = i

                # 최소 지속 시간 확인
                duration = i - cooling_start_idx
                if duration >= self.cooling_min_duration:
                    ts_start = str(df["timestamp"].iloc[cooling_start_idx]) if "timestamp" in df.columns else None
                    ts_peak = str(df["timestamp"].iloc[trough_idx]) if "timestamp" in df.columns else None

                    return SurgeEvent(
                        event_type=EventType.COOLING,
                        start_idx=cooling_start_idx,
                        end_idx=trough_idx,
                        start_price=peak_price,
                        peak_price=peak_price,
                        trough_price=trough_price,
                        change_pct=-(peak_price - trough_price) / peak_price,
                        volume_ratio=0.0,
                        timestamp_start=ts_start,
                        timestamp_peak=ts_peak,
                    )
            elif cooling_confirmed and current_price > peak_price * (1 - self.cooling_drop_pct * 0.5):
                # 쿨링 중단 (가격 회복)
                break

        return None

    def _detect_second_surge(self, df: pd.DataFrame, cooling: SurgeEvent) -> Optional[SurgeEvent]:
        """2차 상승 감지: 쿨링 저점 대비 +5% 이상 + BB 상단 돌파"""
        closes = df["close"].values
        bb_breakout = df.get("bb_breakout_upper", pd.Series(np.zeros(len(df)))).values
        vol_ratios = df.get("volume_ratio", pd.Series(np.ones(len(df)))).values
        n = len(df)

        trough_idx = cooling.end_idx
        trough_price = cooling.trough_price

        if trough_idx is None or trough_price is None or trough_price <= 0:
            return None

        # 저점 이후 상승 탐색
        for i in range(trough_idx + 1, min(trough_idx + 60, n)):
            current_price = closes[i]
            rise_pct = (current_price - trough_price) / trough_price

            if rise_pct >= self.second_surge_pct:
                # BB 돌파 확인
                bb_ok = not self.require_bb_breakout or bool(bb_breakout[i])

                if bb_ok:
                    avg_vol_ratio = np.mean(vol_ratios[trough_idx:i+1])

                    ts_start = str(df["timestamp"].iloc[trough_idx]) if "timestamp" in df.columns else None
                    ts_peak = str(df["timestamp"].iloc[i]) if "timestamp" in df.columns else None

                    return SurgeEvent(
                        event_type=EventType.SECOND_SURGE,
                        start_idx=trough_idx,
                        end_idx=i,
                        start_price=trough_price,
                        peak_price=current_price,
                        trough_price=trough_price,
                        change_pct=rise_pct,
                        volume_ratio=avg_vol_ratio,
                        bb_breakout=bb_ok,
                        timestamp_start=ts_start,
                        timestamp_peak=ts_peak,
                    )

        return None

    def _detect_third_surge(self, df: pd.DataFrame, second_surge: SurgeEvent) -> Optional[SurgeEvent]:
        """3차 이상 상승 감지 (2차 이후 추가 상승)"""
        closes = df["close"].values
        n = len(df)

        if second_surge.end_idx is None or second_surge.end_idx >= n - 10:
            return None

        start_idx = second_surge.end_idx
        start_price = closes[start_idx]
        peak_price = start_price
        peak_idx = start_idx

        # 2차 이후 최고점 탐색
        for i in range(start_idx + 1, n):
            if closes[i] > peak_price:
                peak_price = closes[i]
                peak_idx = i

        rise_pct = (peak_price - start_price) / start_price if start_price > 0 else 0

        if rise_pct >= self.second_surge_pct:
            return SurgeEvent(
                event_type=EventType.THIRD_SURGE,
                start_idx=start_idx,
                end_idx=peak_idx,
                start_price=start_price,
                peak_price=peak_price,
                trough_price=None,
                change_pct=rise_pct,
                volume_ratio=0.0,
            )

        return None

    def detect_realtime(self, df: pd.DataFrame, current_state: dict = None) -> dict:
        """
        실시간 감지 (스트리밍 데이터용)
        current_state: 이전 상태 딕셔너리
        """
        if current_state is None:
            current_state = {
                "phase": "watching",  # watching, first_surge, cooling, ready_to_buy
                "first_surge": None,
                "cooling": None,
                "buy_signal": False,
            }

        result = self.detect(df)

        if result.buy_signal:
            current_state["phase"] = "ready_to_buy"
            current_state["buy_signal"] = True
            current_state["buy_price"] = result.buy_price
        elif result.cooling:
            current_state["phase"] = "cooling"
            current_state["cooling"] = result.cooling
        elif result.first_surge:
            current_state["phase"] = "first_surge"
            current_state["first_surge"] = result.first_surge

        return current_state
