"""
LSTM + PPO 강화학습 에이전트
stable-baselines3 PPO + 커스텀 LSTM 정책 네트워크
"""

import io
import logging
import os
from typing import Optional, Tuple, Type
import numpy as np
import torch
import torch.nn as nn
from stable_baselines3 import PPO
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
import gymnasium as gym

logger = logging.getLogger(__name__)


class LSTMFeaturesExtractor(BaseFeaturesExtractor):
    """
    LSTM 기반 피처 추출기
    Feeder 출력 + 현재 포지션 + 잔고 비율을 처리
    """

    def __init__(
        self,
        observation_space: gym.spaces.Box,
        window_size: int = 60,
        n_features: int = 20,
        lstm_hidden: int = 256,
        lstm_layers: int = 2,
        dropout: float = 0.1,
    ):
        # LSTM 출력 차원
        features_dim = lstm_hidden + 3  # LSTM + 포지션 정보

        super().__init__(observation_space, features_dim=features_dim)

        self.window_size = window_size
        self.n_features = n_features
        self.lstm_hidden = lstm_hidden

        obs_dim = observation_space.shape[0]
        self.seq_dim = window_size * n_features
        self.extra_dim = obs_dim - self.seq_dim  # 포지션 정보 차원

        # LSTM
        self.lstm = nn.LSTM(
            input_size=n_features,
            hidden_size=lstm_hidden,
            num_layers=lstm_layers,
            batch_first=True,
            dropout=dropout if lstm_layers > 1 else 0,
        )

        # 포지션 정보 처리
        self.position_net = nn.Sequential(
            nn.Linear(max(self.extra_dim, 3), 16),
            nn.ReLU(),
        )

        # 최종 피처
        self._features_dim = lstm_hidden + 16

    @property
    def features_dim(self) -> int:
        return self._features_dim

    def forward(self, observations: torch.Tensor) -> torch.Tensor:
        batch_size = observations.shape[0]

        # 시퀀스 부분 분리
        seq_part = observations[:, :self.seq_dim]
        extra_part = observations[:, self.seq_dim:]

        # LSTM 입력 형태로 변환
        seq = seq_part.view(batch_size, self.window_size, self.n_features)

        # LSTM 처리
        lstm_out, _ = self.lstm(seq)
        lstm_feat = lstm_out[:, -1, :]  # 마지막 타임스텝

        # 포지션 정보 처리
        if extra_part.shape[1] > 0:
            # 차원 맞추기
            if extra_part.shape[1] < 3:
                pad = torch.zeros(batch_size, 3 - extra_part.shape[1], device=extra_part.device)
                extra_part = torch.cat([extra_part, pad], dim=-1)
            pos_feat = self.position_net(extra_part[:, :3])
        else:
            pos_feat = torch.zeros(batch_size, 16, device=observations.device)

        return torch.cat([lstm_feat, pos_feat], dim=-1)


class PennyTrader:
    """
    PPO 기반 페니스탁 트레이딩 에이전트
    """

    def __init__(
        self,
        env: gym.Env,
        window_size: int = 60,
        n_features: int = 20,
        lstm_hidden: int = 256,
        learning_rate: float = 3e-4,
        n_steps: int = 2048,
        batch_size: int = 64,
        n_epochs: int = 10,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
        clip_range: float = 0.2,
        ent_coef: float = 0.01,
        device: str = None,
    ):
        self.window_size = window_size
        self.n_features = n_features
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        # 환경 래핑
        if not isinstance(env, DummyVecEnv):
            self.vec_env = DummyVecEnv([lambda: env])
        else:
            self.vec_env = env

        self.vec_env = VecNormalize(self.vec_env, norm_obs=True, norm_reward=True)

        # 커스텀 정책 설정
        policy_kwargs = dict(
            features_extractor_class=LSTMFeaturesExtractor,
            features_extractor_kwargs=dict(
                window_size=window_size,
                n_features=n_features,
                lstm_hidden=lstm_hidden,
            ),
            net_arch=dict(
                pi=[256, 128],  # 정책 네트워크
                vf=[256, 128],  # 가치 네트워크
            ),
            activation_fn=nn.ReLU,
        )

        self.model = PPO(
            policy="MlpPolicy",
            env=self.vec_env,
            learning_rate=learning_rate,
            n_steps=n_steps,
            batch_size=batch_size,
            n_epochs=n_epochs,
            gamma=gamma,
            gae_lambda=gae_lambda,
            clip_range=clip_range,
            ent_coef=ent_coef,
            policy_kwargs=policy_kwargs,
            verbose=1,
            device=self.device,
        )

        logger.info(f"PennyTrader 초기화 완료 (device: {self.device})")

    def train(
        self,
        total_timesteps: int = 1_000_000,
        callback=None,
        progress_bar: bool = True,
    ):
        """PPO 학습"""
        logger.info(f"PPO 학습 시작 (total_timesteps: {total_timesteps:,})")
        self.model.learn(
            total_timesteps=total_timesteps,
            callback=callback,
            progress_bar=progress_bar,
        )
        logger.info("PPO 학습 완료")

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> Tuple[int, dict]:
        """
        행동 예측
        obs: 환경 관측값
        return: (action, info)
        """
        action, _states = self.model.predict(obs, deterministic=deterministic)
        action_names = {0: "HOLD", 1: "BUY", 2: "SELL"}
        return int(action), {"action_name": action_names.get(int(action), "UNKNOWN")}

    def predict_with_feeder(
        self,
        raw_features: np.ndarray,
        case_probs: np.ndarray,
        surge_prob: float,
        position_info: np.ndarray,
    ) -> Tuple[int, dict]:
        """
        Feeder 출력을 포함한 예측
        raw_features: (window_size, n_features)
        case_probs: (5,) - A/B/C/D/E 확률
        surge_prob: 2차 상승 확률
        position_info: [has_position, profit_pct, balance_ratio]
        """
        # 피처 결합
        n_features_with_case = self.n_features + 6  # 원래 피처 + 케이스5 + 서지1
        enhanced = np.zeros((self.window_size, n_features_with_case), dtype=np.float32)

        # 원래 피처 복사
        min_features = min(raw_features.shape[1], self.n_features)
        enhanced[:, :min_features] = raw_features[:, :min_features]

        # 마지막 타임스텝에 케이스 정보 추가
        enhanced[-1, self.n_features:self.n_features+5] = case_probs
        enhanced[-1, self.n_features+5] = surge_prob

        # 관측값 구성
        obs = np.concatenate([
            enhanced.flatten(),
            position_info.astype(np.float32),
        ])

        return self.predict(obs)

    def save(self, path: str):
        """모델 저장"""
        self.model.save(path)
        # VecNormalize 통계 저장
        norm_path = path.replace(".zip", "_vecnorm.pkl")
        self.vec_env.save(norm_path)
        logger.info(f"Trader 모델 저장: {path}")

    def save_bytes(self) -> bytes:
        """모델을 bytes로 반환 (S3 저장용)"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            tmp_path = f.name

        self.model.save(tmp_path)
        with open(tmp_path + ".zip", "rb") as f:
            data = f.read()
        os.unlink(tmp_path + ".zip") if os.path.exists(tmp_path + ".zip") else None
        os.unlink(tmp_path) if os.path.exists(tmp_path) else None
        return data

    @classmethod
    def load(cls, path: str, env: gym.Env, device: str = None) -> "PennyTrader":
        """모델 로드"""
        trader = cls.__new__(cls)
        trader.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        if not isinstance(env, DummyVecEnv):
            trader.vec_env = DummyVecEnv([lambda: env])
        else:
            trader.vec_env = env

        # VecNormalize 로드 시도
        norm_path = path.replace(".zip", "_vecnorm.pkl")
        if os.path.exists(norm_path):
            trader.vec_env = VecNormalize.load(norm_path, trader.vec_env)
        else:
            trader.vec_env = VecNormalize(trader.vec_env, norm_obs=True, norm_reward=False)
            trader.vec_env.training = False

        trader.model = PPO.load(path, env=trader.vec_env, device=trader.device)
        logger.info(f"Trader 모델 로드: {path}")
        return trader


class TrainingCallback:
    """학습 콜백 (로깅 및 체크포인트)"""

    def __init__(
        self,
        save_path: str,
        save_freq: int = 50000,
        verbose: int = 1,
    ):
        self.save_path = save_path
        self.save_freq = save_freq
        self.verbose = verbose
        self.n_calls = 0
        self.best_reward = float("-inf")

    def __call__(self, locals_: dict, globals_: dict) -> bool:
        self.n_calls += 1

        if self.n_calls % self.save_freq == 0:
            path = f"{self.save_path}_step_{self.n_calls}"
            logger.info(f"체크포인트 저장: {path}")

        return True  # 학습 계속
