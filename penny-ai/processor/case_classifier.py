"""
케이스 분류 모듈
A/B/C/D/E 케이스 분류 및 전략 결정
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import numpy as np
import pandas as pd

from processor.event_detector import EventDetector, DetectionResult, EventType

logger = logging.getLogger(__name__)


class CaseType(Enum):
    A = "A"  # 2차 상승 + BB돌파 + 지속 상승 → 피크 -5% 트레일링
    B = "B"  # 2차 상승 + BB돌파 + 급등 후 급락 → 피크 -3% 빠른 이탈
    C = "C"  # 2차 상승 + BB돌파 실패 → 매수 금지
    D = "D"  # 2차 상승 없음 → 매수 금지
    E = "E"  # 3차 이상 상승 → 추가 매수 허용
    UNKNOWN = "UNKNOWN"


@dataclass
class CaseResult:
    case_type: CaseType
    can_buy: bool
    trailing_stop_pct: float       # 트레일링 스탑 %
    target_profit_pct: float       # 목표 수익률 %
    max_hold_minutes: int          # 최대 보유 시간 (분)
    detection: Optional[DetectionResult] = None
    confidence: float = 0.0        # 분류 신뢰도 (0~1)
    reason: str = ""


# 케이스별 전략 파라미터
CASE_STRATEGIES = {
    CaseType.A: {
        "can_buy": True,
        "trailing_stop_pct": 0.05,    # 피크 -5%
        "target_profit_pct": 0.20,    # 목표 +20%
        "max_hold_minutes": 60,
    },
    CaseType.B: {
        "can_buy": True,
        "trailing_stop_pct": 0.03,    # 피크 -3% (빠른 이탈)
        "target_profit_pct": 0.10,    # 목표 +10%
        "max_hold_minutes": 30,
    },
    CaseType.C: {
        "can_buy": False,
        "trailing_stop_pct": 0.0,
        "target_profit_pct": 0.0,
        "max_hold_minutes": 0,
    },
    CaseType.D: {
        "can_buy": False,
        "trailing_stop_pct": 0.0,
        "target_profit_pct": 0.0,
        "max_hold_minutes": 0,
    },
    CaseType.E: {
        "can_buy": True,
        "trailing_stop_pct": 0.05,
        "target_profit_pct": 0.15,
        "max_hold_minutes": 90,
    },
}


class CaseClassifier:
    def __init__(
        self,
        event_detector: Optional[EventDetector] = None,
        # A형 vs B형 구분 기준
        sustained_rise_pct: float = 0.10,     # 2차 이후 10% 이상 지속 상승 → A형
        fast_drop_pct: float = 0.05,          # 2차 피크 이후 5분 내 -5% 급락 → B형
        fast_drop_window: int = 10,            # 급락 판단 윈도우 (분)
    ):
        self.detector = event_detector or EventDetector()
        self.sustained_rise_pct = sustained_rise_pct
        self.fast_drop_pct = fast_drop_pct
        self.fast_drop_window = fast_drop_window

    def classify(self, df: pd.DataFrame) -> CaseResult:
        """
        1분봉 DataFrame으로 케이스 분류
        df: feature_engine.compute_all() 적용된 DataFrame
        """
        detection = self.detector.detect(df)

        # D형: 2차 상승 없음
        if not detection.second_surge:
            case_type = CaseType.D
            reason = "2차 상승 미감지"
            if detection.first_surge and not detection.cooling:
                reason = "1차 상승 후 쿨링 구간 없음"
            elif not detection.first_surge:
                reason = "1차 상승 미감지"
            return self._build_result(case_type, detection, reason=reason)

        second_surge = detection.second_surge

        # C형: 2차 상승 + BB돌파 실패
        if not second_surge.bb_breakout:
            return self._build_result(
                CaseType.C, detection,
                reason="2차 상승 감지되었으나 BB 상단 돌파 실패"
            )

        # E형: 3차 이상 상승
        if detection.third_surge and detection.third_surge.change_pct >= 0.10:
            return self._build_result(
                CaseType.E, detection,
                reason=f"3차 상승 감지 (+{detection.third_surge.change_pct*100:.1f}%)",
                confidence=0.8
            )

        # A형 vs B형 구분: 2차 상승 이후 패턴 분석
        closes = df["close"].values
        second_end_idx = second_surge.end_idx

        if second_end_idx is None or second_end_idx >= len(closes) - 5:
            # 데이터 부족 → 보수적으로 B형
            return self._build_result(
                CaseType.B, detection,
                reason="2차 상승 이후 데이터 부족 (보수적 B형)",
                confidence=0.5
            )

        # 2차 이후 패턴 분석
        post_surge_closes = closes[second_end_idx:]
        peak_after_second = np.max(post_surge_closes) if len(post_surge_closes) > 0 else closes[second_end_idx]
        second_price = closes[second_end_idx]

        sustained_rise = (peak_after_second - second_price) / second_price if second_price > 0 else 0

        # 빠른 급락 확인
        window = min(self.fast_drop_window, len(post_surge_closes))
        if window > 0:
            min_after = np.min(post_surge_closes[:window])
            fast_drop = (peak_after_second - min_after) / peak_after_second if peak_after_second > 0 else 0
        else:
            fast_drop = 0

        if sustained_rise >= self.sustained_rise_pct and fast_drop < self.fast_drop_pct:
            # A형: 지속 상승
            return self._build_result(
                CaseType.A, detection,
                reason=f"2차 상승 후 지속 상승 (+{sustained_rise*100:.1f}%)",
                confidence=min(0.9, 0.5 + sustained_rise * 2)
            )
        elif fast_drop >= self.fast_drop_pct:
            # B형: 급락
            return self._build_result(
                CaseType.B, detection,
                reason=f"2차 상승 후 급락 (-{fast_drop*100:.1f}%)",
                confidence=min(0.9, 0.5 + fast_drop * 2)
            )
        else:
            # 판단 불명확 → B형 (보수적)
            return self._build_result(
                CaseType.B, detection,
                reason="패턴 불명확 (보수적 B형)",
                confidence=0.4
            )

    def _build_result(
        self,
        case_type: CaseType,
        detection: DetectionResult,
        reason: str = "",
        confidence: float = 0.7
    ) -> CaseResult:
        strategy = CASE_STRATEGIES.get(case_type, CASE_STRATEGIES[CaseType.D])
        return CaseResult(
            case_type=case_type,
            can_buy=strategy["can_buy"],
            trailing_stop_pct=strategy["trailing_stop_pct"],
            target_profit_pct=strategy["target_profit_pct"],
            max_hold_minutes=strategy["max_hold_minutes"],
            detection=detection,
            confidence=confidence,
            reason=reason,
        )

    def classify_from_features(self, features: np.ndarray, case_probs: np.ndarray = None) -> CaseResult:
        """
        AI 모델 출력(확률)으로 케이스 분류
        case_probs: [A, B, C, D, E] 확률 배열
        """
        if case_probs is None or len(case_probs) < 5:
            return self._build_result(CaseType.UNKNOWN, DetectionResult(), reason="확률 없음", confidence=0)

        case_map = [CaseType.A, CaseType.B, CaseType.C, CaseType.D, CaseType.E]
        best_idx = int(np.argmax(case_probs))
        case_type = case_map[best_idx]
        confidence = float(case_probs[best_idx])

        strategy = CASE_STRATEGIES.get(case_type, CASE_STRATEGIES[CaseType.D])
        return CaseResult(
            case_type=case_type,
            can_buy=strategy["can_buy"],
            trailing_stop_pct=strategy["trailing_stop_pct"],
            target_profit_pct=strategy["target_profit_pct"],
            max_hold_minutes=strategy["max_hold_minutes"],
            confidence=confidence,
            reason=f"AI 분류 (신뢰도: {confidence:.2f})",
        )

    def get_case_label(self, case_type: CaseType) -> int:
        """케이스를 정수 레이블로 변환 (학습용)"""
        mapping = {
            CaseType.A: 0,
            CaseType.B: 1,
            CaseType.C: 2,
            CaseType.D: 3,
            CaseType.E: 4,
            CaseType.UNKNOWN: 3,
        }
        return mapping.get(case_type, 3)

    def batch_classify(self, data_dict: dict) -> dict:
        """
        여러 종목 일괄 분류
        data_dict: {ticker: df}
        """
        results = {}
        for ticker, df in data_dict.items():
            try:
                result = self.classify(df)
                results[ticker] = result
                logger.info(f"{ticker}: {result.case_type.value}형 (신뢰도: {result.confidence:.2f})")
            except Exception as e:
                logger.error(f"{ticker} 분류 실패: {e}")
                results[ticker] = self._build_result(
                    CaseType.UNKNOWN, DetectionResult(), reason=str(e), confidence=0
                )
        return results
