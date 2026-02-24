"""
백테스트 엔진
- S3 데이터로 과거 시뮬레이션
- 시드: 100만원
- 복리 적용
- 슬리피지: 0.1%, 수수료: 0.1%
- 결과: 일별 수익 곡선, MDD, 샤프비율, 승률
"""

import os
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class Backtester:
    """
    ✅ 실거래 비용 정확 반영 백테스터
    
    비용 구조:
    - 수수료: 매수 0.1% + 매도 0.1% = 왕복 0.2%
    - 슬리피지: 매수 +0.2% + 매도 -0.2% = 왕복 0.4%
      (페니스탁은 스프레드가 크므로 일반주 0.1%보다 높게 설정)
    - 총 왕복 비용: ~0.6%
    - 손익분기: 매매당 최소 +0.6% 수익 필요
    """
    def __init__(
        self,
        initial_balance: float = 1_000_000,
        commission: float = 0.001,       # 수수료 0.1% (편도)
        slippage: float = 0.002,         # ✅ 슬리피지 0.2% (페니스탁 스프레드 반영)
        max_position_pct: float = 0.10,
        max_daily_loss_pct: float = 0.05,
        max_positions: int = 3,
    ):
        self.initial_balance = initial_balance
        self.commission = commission
        self.slippage = slippage
        self.max_position_pct = max_position_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_positions = max_positions
        
        # 왕복 총비용 계산 (로깅용)
        self.total_roundtrip_cost = (commission + slippage) * 2
        logger.info(
            f"백테스터 초기화 — 왕복 총비용: {self.total_roundtrip_cost*100:.2f}% "
            f"(수수료 {commission*100:.1f}%×2 + 슬리피지 {slippage*100:.1f}%×2)"
        )

    def run(self, data: dict, strategy_params: dict = None) -> dict:
        """
        백테스트 실행

        Args:
            data: {date → [{ticker, bars_df, case, events}]} 형태의 데이터
            strategy_params: 전략 파라미터 (trailing_stop_A, trailing_stop_B 등)

        Returns:
            결과 딕셔너리
        """
        params = strategy_params or {
            "trailing_stop_A": 0.05,
            "trailing_stop_B": 0.03,
            "trailing_stop_E": 0.05,
            "stop_loss": 0.07,
            "max_hold_minutes": 60,
        }

        balance = self.initial_balance
        equity_curve = []
        all_trades = []
        positions = {}

        dates = sorted(data.keys())
        logger.info(f"백테스트 시작: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

        for date in dates:
            daily_start_balance = balance
            daily_pnl = 0.0
            day_data = data[date]

            for item in day_data:
                ticker = item["ticker"]
                bars = item["bars_df"]
                case = item.get("case", {})
                case_type = case.get("type", "D")

                if case_type in ["C", "D"]:
                    continue  # 매수 금지

                if len(positions) >= self.max_positions:
                    continue

                # 2차 상승 진입 시점 찾기
                entry_idx = self._find_entry(bars, case)
                if entry_idx is None:
                    continue

                # 매수
                entry_price = bars.iloc[entry_idx]["close"] * (1 + self.slippage)
                position_size = min(balance * self.max_position_pct,
                                    self.initial_balance * 0.20)
                qty = int(position_size / entry_price)
                if qty <= 0:
                    continue

                cost = qty * entry_price * (1 + self.commission)
                if cost > balance:
                    continue

                balance -= cost
                peak_price = entry_price

                # 매도 시점 탐색
                exit_idx, exit_reason = self._find_exit(
                    bars, entry_idx, entry_price, case_type, params
                )

                exit_price = bars.iloc[exit_idx]["close"] * (1 - self.slippage)
                revenue = qty * exit_price * (1 - self.commission)
                pnl = revenue - cost
                pnl_pct = (exit_price - entry_price) / entry_price * 100

                balance += revenue
                daily_pnl += pnl

                trade = {
                    "date": date,
                    "ticker": ticker,
                    "case_type": case_type,
                    "entry_idx": entry_idx,
                    "exit_idx": exit_idx,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "qty": qty,
                    "cost": cost,
                    "revenue": revenue,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                    "hold_minutes": exit_idx - entry_idx,
                }
                all_trades.append(trade)

                # 일일 손실 한도
                daily_loss_pct = daily_pnl / daily_start_balance
                if daily_loss_pct <= -self.max_daily_loss_pct:
                    logger.info(f"{date} 일일 손실 한도 도달 — 거래 중단")
                    break

            equity_curve.append({
                "date": date,
                "balance": balance,
                "daily_pnl": daily_pnl,
                "daily_return_pct": (balance - daily_start_balance) / daily_start_balance * 100
            })

        return self._calc_stats(all_trades, equity_curve, balance)

    def _find_entry(self, bars: pd.DataFrame, case: dict) -> Optional[int]:
        """2차 상승 진입 시점 탐색"""
        second_surge_idx = case.get("second_surge_idx")
        if second_surge_idx is not None and second_surge_idx < len(bars) - 1:
            return second_surge_idx + 1  # 다음 봉에 진입
        return None

    def _find_exit(self, bars: pd.DataFrame, entry_idx: int,
                   entry_price: float, case_type: str, params: dict):
        """매도 시점 탐색"""
        trailing_pct = {
            "A": params["trailing_stop_A"],
            "B": params["trailing_stop_B"],
            "E": params["trailing_stop_E"],
        }.get(case_type, 0.05)

        stop_loss_price = entry_price * (1 - params["stop_loss"])
        peak_price = entry_price
        max_hold = params["max_hold_minutes"]

        for i in range(entry_idx + 1, min(entry_idx + max_hold, len(bars))):
            close = bars.iloc[i]["close"]

            # 피크 업데이트
            if close > peak_price:
                peak_price = close

            # 트레일링 스탑
            trailing_stop = peak_price * (1 - trailing_pct)
            if close <= trailing_stop:
                return i, f"트레일링_{int(trailing_pct*100)}%"

            # 손절
            if close <= stop_loss_price:
                return i, f"손절_{int(params['stop_loss']*100)}%"

        # 시간 초과
        return min(entry_idx + max_hold - 1, len(bars) - 1), "시간초과"

    def _calc_stats(self, trades: list, equity_curve: list, final_balance: float) -> dict:
        """성과 통계 계산"""
        if not trades:
            return {"error": "거래 없음"}

        df_trades = pd.DataFrame(trades)
        df_equity = pd.DataFrame(equity_curve)

        # 기본 통계
        total_trades = len(trades)
        wins = df_trades[df_trades["pnl"] > 0]
        losses = df_trades[df_trades["pnl"] <= 0]
        win_rate = len(wins) / total_trades * 100

        # 수익률
        total_return = (final_balance - self.initial_balance) / self.initial_balance * 100
        avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
        avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0

        # MDD (최대 낙폭)
        balances = df_equity["balance"].values
        peak = np.maximum.accumulate(balances)
        drawdown = (balances - peak) / peak * 100
        mdd = drawdown.min()

        # 샤프 비율
        daily_returns = df_equity["daily_return_pct"].values
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)
                  if daily_returns.std() > 0 else 0)

        # 케이스별 통계
        case_stats = {}
        for case_type in ["A", "B", "E"]:
            ct = df_trades[df_trades["case_type"] == case_type]
            if len(ct) > 0:
                case_stats[case_type] = {
                    "count": len(ct),
                    "win_rate": len(ct[ct["pnl"] > 0]) / len(ct) * 100,
                    "avg_pnl_pct": ct["pnl_pct"].mean(),
                    "total_pnl": ct["pnl"].sum()
                }

        # ✅ 총 거래비용 계산
        total_cost_paid = sum(
            t["cost"] * self.total_roundtrip_cost for t in trades
        )
        
        # ✅ 비용 제외 수익률 (gross) vs 비용 포함 수익률 (net) 비교
        gross_return = (final_balance + total_cost_paid - self.initial_balance) / self.initial_balance * 100
        net_return = total_return  # 이미 비용 반영됨

        result = {
            "period": f"{equity_curve[0]['date']} ~ {equity_curve[-1]['date']}",
            "initial_balance": self.initial_balance,
            "final_balance": final_balance,
            "total_return_pct": total_return,
            "gross_return_pct": gross_return,           # ✅ 비용 제외 수익률
            "net_return_pct": net_return,               # ✅ 비용 포함 순수익률
            "total_cost_paid": total_cost_paid,         # ✅ 총 납부 비용
            "cost_drag_pct": gross_return - net_return, # ✅ 비용으로 인한 수익 손실
            "roundtrip_cost_pct": self.total_roundtrip_cost * 100,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "profit_factor": abs(wins["pnl"].sum() / losses["pnl"].sum()) if len(losses) > 0 else 999,
            "mdd": mdd,
            "sharpe_ratio": sharpe,
            "case_stats": case_stats,
            "equity_curve": equity_curve,
            "trades": trades,
        }

        # 결과 출력
        logger.info(f"\n{'='*55}")
        logger.info(f"📊 백테스트 결과")
        logger.info(f"기간: {result['period']}")
        logger.info(f"초기 자본: {self.initial_balance:,.0f}원")
        logger.info(f"최종 자본: {final_balance:,.0f}원")
        logger.info(f"순수익률(비용포함): {net_return:+.2f}%")
        logger.info(f"총수익률(비용제외): {gross_return:+.2f}%")
        logger.info(f"총 납부 비용: {total_cost_paid:,.0f}원 ({gross_return-net_return:.2f}%p 손실)")
        logger.info(f"왕복 거래비용: {self.total_roundtrip_cost*100:.2f}%/건")
        logger.info(f"총 거래: {total_trades}건")
        logger.info(f"승률: {win_rate:.1f}%")
        logger.info(f"평균 수익: {avg_win:+.2f}%")
        logger.info(f"평균 손실: {avg_loss:+.2f}%")
        logger.info(f"MDD: {mdd:.2f}%")
        logger.info(f"샤프비율: {sharpe:.2f}")
        logger.info(f"{'='*55}")

        return result

    def optimize_params(self, data: dict, param_grid: dict = None) -> dict:
        """파라미터 최적화 (그리드 서치)"""
        param_grid = param_grid or {
            "trailing_stop_A": [0.03, 0.05, 0.07, 0.10],
            "trailing_stop_B": [0.02, 0.03, 0.05],
            "trailing_stop_E": [0.05, 0.07, 0.10],
            "stop_loss": [0.05, 0.07, 0.10],
            "max_hold_minutes": [30, 60, 90],
        }

        best_result = None
        best_params = None
        best_return = float("-inf")

        # 단순 그리드 서치 (조합 수 제한)
        import itertools
        keys = list(param_grid.keys())
        values = list(param_grid.values())

        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            result = self.run(data, params)
            if result.get("total_return_pct", float("-inf")) > best_return:
                best_return = result["total_return_pct"]
                best_result = result
                best_params = params

        logger.info(f"✅ 최적 파라미터: {best_params}")
        logger.info(f"✅ 최적 수익률: {best_return:+.2f}%")

        return {"best_params": best_params, "best_result": best_result}
