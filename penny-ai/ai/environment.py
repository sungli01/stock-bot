"""
Penny Stock Trading Environment (Gymnasium)
- 수수료 + 슬리피지 정확히 반영
- 행동: 0=홀드, 1=매수, 2=매도
"""
import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

import os
import sys
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from processor.feature_engine import FEATURE_COLS


# 비용 상수
COMMISSION_RATE = 0.001       # 수수료 0.1% (왕복 0.2%)
SLIPPAGE_RATE = 0.0005        # 슬리피지 0.05%
TOTAL_COST_RATE = COMMISSION_RATE + SLIPPAGE_RATE  # 편도 0.15%

INITIAL_CASH = 10_000.0       # 초기 자본 $10,000
MAX_POSITION_RATIO = 0.95     # 최대 포지션 비율


class PennyStockEnv(gym.Env):
    """
    1분봉 기반 페니스탁 트레이딩 환경

    Observation: FEATURE_COLS + [포지션 보유 여부, 평균 매수가 비율, 수익률]
    Action: Discrete(3) — 0=홀드, 1=매수, 2=매도
    Reward: 실현 수익률 - 비용 (슬리피지 + 수수료)
    """

    metadata = {'render_modes': []}

    def __init__(self, df: pd.DataFrame, window_size: int = 30):
        super().__init__()

        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.n_steps = len(self.df)

        # 관측 공간: window_size × feature_cols + 포지션 정보 3개
        n_features = len(FEATURE_COLS)
        obs_dim = window_size * n_features + 3
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float32
        )

        # 행동 공간: 0=홀드, 1=매수, 2=매도
        self.action_space = spaces.Discrete(3)

        self._reset_state()

    def _reset_state(self):
        self.current_step = self.window_size
        self.cash = INITIAL_CASH
        self.shares = 0
        self.avg_buy_price = 0.0
        self.total_reward = 0.0
        self.trade_count = 0

    def _get_obs(self):
        window = self.df[FEATURE_COLS].iloc[
            self.current_step - self.window_size: self.current_step
        ].values.astype(np.float32)

        current_price = self.df['close'].iloc[self.current_step]
        position_flag = float(self.shares > 0)
        avg_price_ratio = self.avg_buy_price / (current_price + 1e-9) if self.shares > 0 else 1.0
        unrealized_pnl = (current_price - self.avg_buy_price) / (self.avg_buy_price + 1e-9) if self.shares > 0 else 0.0

        extra = np.array([position_flag, avg_price_ratio, unrealized_pnl], dtype=np.float32)
        return np.concatenate([window.flatten(), extra])

    def _portfolio_value(self):
        price = self.df['close'].iloc[self.current_step]
        return self.cash + self.shares * price

    def step(self, action: int):
        price = self.df['close'].iloc[self.current_step]
        reward = 0.0
        info = {}

        if action == 1:  # 매수
            if self.shares == 0 and self.cash > price:
                invest = self.cash * MAX_POSITION_RATIO
                cost = invest * TOTAL_COST_RATE
                shares_bought = (invest - cost) / price
                self.shares = shares_bought
                self.cash -= invest
                self.avg_buy_price = price
                self.trade_count += 1
                reward = -TOTAL_COST_RATE  # 매수 시 비용 페널티

        elif action == 2:  # 매도
            if self.shares > 0:
                proceeds = self.shares * price
                cost = proceeds * TOTAL_COST_RATE
                net_proceeds = proceeds - cost
                self.cash += net_proceeds

                # 실현 수익률 계산 (왕복 비용 포함)
                pnl_ratio = (price - self.avg_buy_price) / (self.avg_buy_price + 1e-9)
                reward = pnl_ratio - 2 * TOTAL_COST_RATE  # 왕복 비용 차감

                self.shares = 0
                self.avg_buy_price = 0.0
                self.trade_count += 1
                info['realized_pnl'] = reward

        # 홀드 시 미실현 수익 소폭 반영 (과매매 방지)
        elif action == 0 and self.shares > 0:
            unrealized = (price - self.avg_buy_price) / (self.avg_buy_price + 1e-9)
            reward = unrealized * 0.001  # 미실현 수익의 0.1%만 반영

        self.total_reward += reward
        self.current_step += 1

        terminated = self.current_step >= self.n_steps - 1
        truncated = False

        obs = self._get_obs()
        info['portfolio_value'] = self._portfolio_value()
        info['trade_count'] = self.trade_count

        return obs, reward, terminated, truncated, info

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self._reset_state()
        return self._get_obs(), {}

    def render(self):
        price = self.df['close'].iloc[self.current_step]
        pv = self._portfolio_value()
        print(f"Step {self.current_step} | Price: {price:.4f} | Portfolio: {pv:.2f} | Shares: {self.shares:.2f}")
