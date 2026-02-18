"""
시장 거버넌스 모듈
- 시장 전체 흐름(SPY, QQQ) 감지
- 상승/보합/하락 상태에 따라 투자 캡 자동 조정
- 절대 상한 ₩25,000,000 초과 금지
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 절대 상한 (형님 지시: 2500만원 초과 불가)
ABSOLUTE_CAP = 25_000_000

# 시장 지표 티커
MARKET_TICKERS = ["SPY", "QQQ"]

# 거버넌스 레벨
class MarketState:
    BULL = "bull"       # 상승장
    NEUTRAL = "neutral" # 보합
    BEAR = "bear"       # 하락장
    CRASH = "crash"     # 급락장


class MarketGovernor:
    """시장 상태 기반 투자 캡 자동 조정"""

    def __init__(self, config: dict):
        gov_cfg = config.get("governance", {})
        self.base_cap = config.get("trading", {}).get("compound_cap", 5_000_000)

        # 거버넌스 임계값
        self.bull_threshold = gov_cfg.get("bull_threshold", 1.0)    # SPY +1% 이상 = 상승
        self.bear_threshold = gov_cfg.get("bear_threshold", -1.0)   # SPY -1% 이하 = 하락
        self.crash_threshold = gov_cfg.get("crash_threshold", -3.0) # SPY -3% 이하 = 급락

        # 캡 배율 (base_cap 대비)
        self.cap_multipliers = {
            MarketState.BULL: gov_cfg.get("bull_multiplier", 5.0),       # 상승: 5배 (500만→2500만)
            MarketState.NEUTRAL: gov_cfg.get("neutral_multiplier", 1.0), # 보합: 1배 (기본)
            MarketState.BEAR: gov_cfg.get("bear_multiplier", 0.5),       # 하락: 0.5배 (250만)
            MarketState.CRASH: gov_cfg.get("crash_multiplier", 0.0),     # 급락: 매매 중단
        }

        self._current_state = MarketState.NEUTRAL
        self._market_changes: dict[str, float] = {}  # ticker → change_pct

    def update_market_data(self, snapshot_map: dict):
        """스냅샷에서 SPY, QQQ 변동률 업데이트"""
        for ticker in MARKET_TICKERS:
            snap = snapshot_map.get(ticker)
            if snap:
                self._market_changes[ticker] = snap.get("change_pct", 0)

    def evaluate_state(self) -> str:
        """시장 상태 판단 (SPY 기준, QQQ 보조)"""
        spy_change = self._market_changes.get("SPY", 0)
        qqq_change = self._market_changes.get("QQQ", 0)

        # 평균 사용 (SPY 70%, QQQ 30%)
        avg_change = spy_change * 0.7 + qqq_change * 0.3

        prev_state = self._current_state

        if avg_change <= self.crash_threshold:
            self._current_state = MarketState.CRASH
        elif avg_change <= self.bear_threshold:
            self._current_state = MarketState.BEAR
        elif avg_change >= self.bull_threshold:
            self._current_state = MarketState.BULL
        else:
            self._current_state = MarketState.NEUTRAL

        if prev_state != self._current_state:
            logger.info(
                f"📊 시장 상태 변경: {prev_state} → {self._current_state} "
                f"(SPY {spy_change:+.2f}%, QQQ {qqq_change:+.2f}%)"
            )

        return self._current_state

    def get_adjusted_cap(self) -> int:
        """현재 시장 상태 기반 조정된 캡 반환 (절대 상한 적용)"""
        multiplier = self.cap_multipliers.get(self._current_state, 1.0)
        adjusted = int(self.base_cap * multiplier)
        # 절대 상한: ₩25,000,000
        final = min(adjusted, ABSOLUTE_CAP)
        return final

    def should_trade(self) -> bool:
        """매매 가능 여부 (급락 시 매매 중단)"""
        return self._current_state != MarketState.CRASH

    @property
    def state(self) -> str:
        return self._current_state

    @property
    def market_info(self) -> dict:
        return {
            "state": self._current_state,
            "spy_change": self._market_changes.get("SPY", 0),
            "qqq_change": self._market_changes.get("QQQ", 0),
            "adjusted_cap": self.get_adjusted_cap(),
            "absolute_cap": ABSOLUTE_CAP,
        }
