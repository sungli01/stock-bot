"""
하이브리드 플로어 매도 모듈 (v5)
- 계단식 플로어: 120%+ 도달 시 활성화
- 기본 30% 단일 플로어
- 절대 손절 -50%
- 장마감 05:45 KST 강제 청산
"""
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 계단식 플로어 (peak_profit이 해당 값 이상이면 그 값이 플로어)
STAIRCASE_FLOORS = [120, 300, 400, 500, 600, 700, 800, 900, 1000]
BASE_FLOOR = 30  # 기본 플로어 (%)
ABSOLUTE_STOP_LOSS = -50.0  # 절대 손절 (%)


class BBTrailingStop:
    """하이브리드 플로어 기반 매도 관리"""

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        self.force_close_before_min = trading_cfg.get("force_close_before_min", 15)

        # 종목별 상태: peak profit % 추적
        self._peak_profit: dict[str, float] = {}  # ticker → peak profit %

    def check_exit(self, ticker: str, current_price: float, avg_price: float) -> Optional[dict]:
        """
        종목의 현재가로 매도 조건 체크
        Returns: {"action": "SELL"|"STOP", "reason": "...", "pnl_pct": float} or None
        """
        if not current_price or not avg_price or avg_price <= 0:
            return None

        current_profit_pct = ((current_price - avg_price) / avg_price) * 100

        # peak profit 갱신 (절대 내려가지 않음)
        prev_peak = self._peak_profit.get(ticker, 0.0)
        peak_profit_pct = max(prev_peak, current_profit_pct)
        self._peak_profit[ticker] = peak_profit_pct

        # 1. 절대 손절 -50%
        if current_profit_pct <= ABSOLUTE_STOP_LOSS:
            self._cleanup(ticker)
            return {
                "action": "STOP",
                "reason": f"절대 손절 {current_profit_pct:.1f}%",
                "pnl_pct": current_profit_pct,
            }

        # 2. 120%+ 도달 시 계단식 플로어
        if peak_profit_pct >= 120:
            current_floor = 120
            for f in STAIRCASE_FLOORS:
                if peak_profit_pct >= f:
                    current_floor = f
                else:
                    break

            if current_profit_pct < current_floor:
                self._cleanup(ticker)
                return {
                    "action": "SELL",
                    "reason": f"계단식 플로어 {current_floor}% (peak {peak_profit_pct:.0f}%)",
                    "pnl_pct": current_profit_pct,
                }

            logger.debug(
                f"📊 {ticker} 계단식 홀딩: current={current_profit_pct:.1f}% "
                f"peak={peak_profit_pct:.0f}% floor={current_floor}%"
            )
            return None

        # 3. 120% 미만: 기본 30% 단일 플로어
        if peak_profit_pct >= BASE_FLOOR and current_profit_pct < BASE_FLOOR:
            self._cleanup(ticker)
            return {
                "action": "SELL",
                "reason": f"30% 플로어 보호 (peak {peak_profit_pct:.0f}%)",
                "pnl_pct": current_profit_pct,
            }

        # 4. 홀딩
        return None

    def _cleanup(self, ticker: str):
        """종목 상태 정리"""
        self._peak_profit.pop(ticker, None)

    def reset(self):
        """세션 리셋"""
        self._peak_profit.clear()
