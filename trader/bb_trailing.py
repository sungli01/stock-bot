"""
급등 스캘핑 매도 모듈 (v7) — 동적 트레일링
- 수익 구간별 트레일링 폭 자동 조정
- 시간 경과에 따른 가중치
- -7% 절대 손절
- 45분 보유 제한
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# 수익 구간별 트레일링 폭 (peak_profit_pct, trailing_drop_pct)
# peak가 해당 구간 이상이면 그 폭 적용 (마지막 매칭)
DYNAMIC_TRAILING = [
    (6,   2.0),   # [v10] +6~15%: -2%p (초기 급등, 타이트)
    (15,  5.0),   # +15~50%: -5%p (등락 시작, 여유)
    (50,  8.0),   # +50~80%: -8%p (큰 등락 허용)
    (80,  30.0),  # +80%~: -30%p (대폭등, 넉넉한 여유)
]

# 시간 가중치: 경과 시간에 따라 트레일링 폭 조정 (배수)
TIME_WEIGHT = [
    (0,  1.0),   # 0~10분: 표준
    (10, 1.0),   # 10~30분: 표준
    (30, 0.8),   # 30분~: 모멘텀 소진 가능, 약간 타이트
]


def _get_trailing_drop(peak_pct: float, elapsed_min: float) -> float:
    """수익률과 경과 시간에 따른 동적 트레일링 폭 계산"""
    # 수익 구간별 기본 폭
    base_drop = 3.0
    for threshold, drop in DYNAMIC_TRAILING:
        if peak_pct >= threshold:
            base_drop = drop
        else:
            break

    # 시간 가중치 적용
    time_mult = 1.0
    for min_threshold, mult in TIME_WEIGHT:
        if elapsed_min >= min_threshold:
            time_mult = mult

    return base_drop * time_mult


class BBTrailingStop:
    """동적 트레일링 기반 급등 스캘핑 매도 관리"""

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        sell_cfg = config.get("sell_strategy", {})

        self.force_close_before_min = trading_cfg.get("force_close_before_min", 15)
        self.max_hold_minutes = trading_cfg.get("max_hold_minutes", 45)

        # 트레일링 활성화 기준
        self.trailing_activate_pct = sell_cfg.get("trailing_activate_pct", 8.0)
        self.absolute_stop_loss = sell_cfg.get("absolute_stop_loss_pct", -7.0)

        # 종목별 상태
        self._peak_profit: dict[str, float] = {}
        self._entry_time: dict[str, datetime] = {}
        self._trailing_active: dict[str, bool] = {}

    def register_entry(self, ticker: str):
        """매수 시 호출 — 진입 시각 기록"""
        self._entry_time[ticker] = datetime.now(timezone.utc)
        self._trailing_active[ticker] = False
        self._peak_profit[ticker] = 0.0
        logger.info(f"⏱️ {ticker} 진입 등록 (max {self.max_hold_minutes}분)")

    def check_exit(self, ticker: str, current_price: float, avg_price: float) -> Optional[dict]:
        """매도 조건 체크"""
        if not current_price or not avg_price or avg_price <= 0:
            return None

        current_profit_pct = ((current_price - avg_price) / avg_price) * 100

        # peak profit 갱신
        prev_peak = self._peak_profit.get(ticker, 0.0)
        peak_profit_pct = max(prev_peak, current_profit_pct)
        self._peak_profit[ticker] = peak_profit_pct

        # 경과 시간
        entry_time = self._entry_time.get(ticker)
        elapsed_min = 0
        if entry_time:
            elapsed_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60

        # 1. 절대 손절
        if current_profit_pct <= self.absolute_stop_loss:
            self._cleanup(ticker)
            return {
                "action": "STOP",
                "reason": f"손절 {current_profit_pct:.1f}% (한도 {self.absolute_stop_loss}%)",
                "pnl_pct": current_profit_pct,
            }

        # 2. 보유 시간 제한
        if elapsed_min >= self.max_hold_minutes:
            self._cleanup(ticker)
            return {
                "action": "SELL",
                "reason": f"보유 {elapsed_min:.0f}분 초과 (한도 {self.max_hold_minutes}분)",
                "pnl_pct": current_profit_pct,
            }

        # 3. 트레일링 활성화 체크
        if peak_profit_pct >= self.trailing_activate_pct:
            self._trailing_active[ticker] = True

        # 4. 동적 트레일링 매도
        if self._trailing_active.get(ticker, False):
            trailing_drop = _get_trailing_drop(peak_profit_pct, elapsed_min)
            drop_from_peak = peak_profit_pct - current_profit_pct

            if drop_from_peak >= trailing_drop:
                self._cleanup(ticker)
                return {
                    "action": "SELL",
                    "reason": (
                        f"트레일링 매도 (peak +{peak_profit_pct:.1f}% → "
                        f"+{current_profit_pct:.1f}%, -{drop_from_peak:.1f}%p, "
                        f"허용폭 {trailing_drop:.1f}%p, {elapsed_min:.0f}분)"
                    ),
                    "pnl_pct": current_profit_pct,
                }

            # 디버그 로그 (20초마다 정도만)
            if int(elapsed_min * 3) % 10 == 0:
                logger.debug(
                    f"📊 {ticker} 홀딩: +{current_profit_pct:.1f}% "
                    f"(peak +{peak_profit_pct:.1f}%, drop {drop_from_peak:.1f}%p/"
                    f"{trailing_drop:.1f}%p, {elapsed_min:.0f}분)"
                )

        # 5. 홀딩
        return None

    def get_status(self, ticker: str) -> dict:
        """종목별 상태 조회"""
        peak = self._peak_profit.get(ticker, 0.0)
        entry = self._entry_time.get(ticker)
        elapsed = 0
        if entry:
            elapsed = (datetime.now(timezone.utc) - entry).total_seconds() / 60
        trailing_drop = _get_trailing_drop(peak, elapsed) if peak >= self.trailing_activate_pct else None
        return {
            "peak": peak,
            "trailing_active": self._trailing_active.get(ticker, False),
            "trailing_drop": trailing_drop,
            "elapsed_min": elapsed,
        }

    def _cleanup(self, ticker: str):
        self._peak_profit.pop(ticker, None)
        self._entry_time.pop(ticker, None)
        self._trailing_active.pop(ticker, None)

    def reset(self):
        self._peak_profit.clear()
        self._entry_time.clear()
        self._trailing_active.clear()
