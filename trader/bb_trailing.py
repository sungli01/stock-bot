"""
급등 스캘핑 매도 모듈 (v6)
- +5% 1차 익절 (50% 물량)
- +8% 트레일링 스탑 활성화 (고점 -3% 하락 시 매도)
- +10% 2차 익절 (잔여 전량)
- -7% 절대 손절
- 45분 보유 제한 (미익절 시 자동 청산)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class BBTrailingStop:
    """급등 스캘핑 매도 관리"""

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        sell_cfg = config.get("sell_strategy", {})

        self.force_close_before_min = trading_cfg.get("force_close_before_min", 15)
        self.max_hold_minutes = trading_cfg.get("max_hold_minutes", 45)

        # 익절/손절 설정
        self.take_profit_1_pct = sell_cfg.get("take_profit_1_pct", 5.0)
        self.take_profit_2_pct = sell_cfg.get("take_profit_2_pct", 10.0)
        self.trailing_activate_pct = sell_cfg.get("trailing_activate_pct", 8.0)
        self.trailing_drop_pct = sell_cfg.get("trailing_drop_pct", 3.0)
        self.absolute_stop_loss = sell_cfg.get("absolute_stop_loss_pct", -7.0)

        # 종목별 상태
        self._peak_profit: dict[str, float] = {}       # ticker → peak profit %
        self._entry_time: dict[str, datetime] = {}      # ticker → 진입 시각
        self._partial_sold: dict[str, bool] = {}        # ticker → 1차 익절 완료 여부
        self._trailing_active: dict[str, bool] = {}     # ticker → 트레일링 활성화 여부

    def register_entry(self, ticker: str):
        """매수 시 호출 — 진입 시각 기록"""
        self._entry_time[ticker] = datetime.now(timezone.utc)
        self._partial_sold[ticker] = False
        self._trailing_active[ticker] = False
        self._peak_profit[ticker] = 0.0
        logger.info(f"⏱️ {ticker} 진입 등록 (max {self.max_hold_minutes}분)")

    def check_exit(self, ticker: str, current_price: float, avg_price: float) -> Optional[dict]:
        """
        매도 조건 체크
        Returns: {"action": "SELL"|"STOP"|"PARTIAL_SELL", "reason": "...", "pnl_pct": float} or None
        """
        if not current_price or not avg_price or avg_price <= 0:
            return None

        current_profit_pct = ((current_price - avg_price) / avg_price) * 100

        # peak profit 갱신
        prev_peak = self._peak_profit.get(ticker, 0.0)
        peak_profit_pct = max(prev_peak, current_profit_pct)
        self._peak_profit[ticker] = peak_profit_pct

        # 1. 절대 손절
        if current_profit_pct <= self.absolute_stop_loss:
            self._cleanup(ticker)
            return {
                "action": "STOP",
                "reason": f"손절 {current_profit_pct:.1f}% (한도 {self.absolute_stop_loss}%)",
                "pnl_pct": current_profit_pct,
            }

        # 2. 보유 시간 제한
        entry_time = self._entry_time.get(ticker)
        if entry_time:
            elapsed_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
            if elapsed_min >= self.max_hold_minutes:
                self._cleanup(ticker)
                return {
                    "action": "SELL",
                    "reason": f"보유 {elapsed_min:.0f}분 초과 (한도 {self.max_hold_minutes}분)",
                    "pnl_pct": current_profit_pct,
                }

        # 3. +10% 이상 → 전량 익절 (2차)
        if current_profit_pct >= self.take_profit_2_pct:
            self._cleanup(ticker)
            return {
                "action": "SELL",
                "reason": f"2차 익절 +{current_profit_pct:.1f}%",
                "pnl_pct": current_profit_pct,
            }

        # 4. 트레일링 스탑 체크 (+8% 도달 후 고점 -3% 하락)
        if peak_profit_pct >= self.trailing_activate_pct:
            self._trailing_active[ticker] = True

        if self._trailing_active.get(ticker, False):
            drop_from_peak = peak_profit_pct - current_profit_pct
            if drop_from_peak >= self.trailing_drop_pct:
                self._cleanup(ticker)
                return {
                    "action": "SELL",
                    "reason": f"트레일링 매도 (peak +{peak_profit_pct:.1f}% → +{current_profit_pct:.1f}%, -{drop_from_peak:.1f}%p)",
                    "pnl_pct": current_profit_pct,
                }

        # 5. +5% 1차 익절 (50% 물량) — 아직 1차 익절 안 했으면
        if not self._partial_sold.get(ticker, False) and current_profit_pct >= self.take_profit_1_pct:
            self._partial_sold[ticker] = True
            return {
                "action": "PARTIAL_SELL",
                "reason": f"1차 익절 +{current_profit_pct:.1f}% (50% 물량)",
                "pnl_pct": current_profit_pct,
                "sell_ratio": 0.5,
            }

        # 6. 홀딩
        return None

    def get_status(self, ticker: str) -> dict:
        """종목별 상태 조회"""
        peak = self._peak_profit.get(ticker, 0.0)
        entry = self._entry_time.get(ticker)
        elapsed = None
        if entry:
            elapsed = (datetime.now(timezone.utc) - entry).total_seconds() / 60
        return {
            "peak": peak,
            "partial_sold": self._partial_sold.get(ticker, False),
            "trailing_active": self._trailing_active.get(ticker, False),
            "elapsed_min": elapsed,
        }

    def _cleanup(self, ticker: str):
        """종목 상태 정리"""
        self._peak_profit.pop(ticker, None)
        self._entry_time.pop(ticker, None)
        self._partial_sold.pop(ticker, None)
        self._trailing_active.pop(ticker, None)

    def reset(self):
        """세션 리셋"""
        self._peak_profit.clear()
        self._entry_time.clear()
        self._partial_sold.clear()
        self._trailing_active.clear()
