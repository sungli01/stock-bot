"""
리스크 관리 모듈
- 1일 최대 투자: 총 자산의 20%
- 종목당 최대: 총 자산의 10%
- 최대 동시 포지션: 3개
- 일일 최대 손실: -5% 시 거래 중단
"""

import os
import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.max_position_pct = float(os.environ.get("MAX_POSITION_PCT", 0.10))
        self.max_daily_loss_pct = float(os.environ.get("MAX_DAILY_LOSS_PCT", 0.05))
        self.max_positions = int(os.environ.get("MAX_POSITIONS", 3))
        self.max_daily_invest_pct = 0.20  # 일일 최대 투자 20%

    def can_trade(self, ticker: str, balance: float, daily_pnl: float, current_positions: dict = None) -> bool:
        """거래 가능 여부 체크"""
        current_positions = current_positions or {}

        # 일일 손실 한도 체크
        daily_loss_pct = daily_pnl / self.initial_balance
        if daily_loss_pct <= -self.max_daily_loss_pct:
            logger.warning(f"일일 손실 한도 초과: {daily_loss_pct:.2%}")
            return False

        # 최대 동시 포지션 체크
        if len(current_positions) >= self.max_positions:
            logger.info(f"최대 포지션 수 도달: {len(current_positions)}/{self.max_positions}")
            return False

        # 잔고 부족 체크
        min_trade_amount = self.initial_balance * 0.01  # 최소 1%
        if balance < min_trade_amount:
            logger.warning(f"잔고 부족: {balance:,.0f}원")
            return False

        return True

    def calc_position_size(self, balance: float) -> float:
        """포지션 크기 계산 (총 자산의 10%)"""
        position_size = min(
            balance * self.max_position_pct,
            self.initial_balance * self.max_daily_invest_pct
        )
        return max(position_size, 0)

    def calc_stop_loss(self, case_type: str, entry_price: float) -> float:
        """케이스별 손절가 계산"""
        stop_loss_pct = {
            "A": 0.07,   # -7%
            "B": 0.05,   # -5%
            "E": 0.07,   # -7%
        }.get(case_type, 0.07)
        return entry_price * (1 - stop_loss_pct)

    def calc_trailing_stop(self, case_type: str, peak_price: float) -> float:
        """케이스별 트레일링 스탑 계산"""
        trailing_pct = {
            "A": 0.05,   # 피크 -5%
            "B": 0.03,   # 피크 -3%
            "E": 0.05,   # 피크 -5%
        }.get(case_type, 0.05)
        return peak_price * (1 - trailing_pct)
