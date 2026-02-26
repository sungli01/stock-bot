"""
페니스탁 AI 학습 실행 스크립트
실제 S3 경로: raw/intraday/{date}/{TICKER}_{session}_1m.parquet
컬럼: ticker, datetime, session, open, high, low, close, volume, vwap, transactions
"""

import os
import sys
import io
import logging
import time
import warnings
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd
import boto3
import pyarrow.parquet as pq
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, random_split
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv

warnings.filterwarnings("ignore")

# ─── 환경 설정 ────────────────────────────────────────────────────────────────
AWS_KEY = os.environ.get("AWS_ACCESS_KEY_ID")  # 환경변수에서 로드
AWS_SECRET = os.environ.get("AWS_SECRET_ACCESS_KEY")  # 환경변수에서 로드
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2")
S3_BUCKET = os.environ.get("S3_BUCKET", "sungli-market-data")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5810895605")

# ─── 로깅 설정 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/home/ubuntu/.nanobot/workspace/penny-ai/train.log"),
    ],
)
logger = logging.getLogger(__name__)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
logger.info(f"디바이스: {DEVICE}")


# ─── 텔레그램 알림 ───────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        logger.info(f"[텔레그램] {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.warning(f"텔레그램 전송 실패: {e}")


# ─── S3 데이터 로더 ──────────────────────────────────────────────────────────
class DataLoader_S3:
    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_KEY,
            aws_secret_access_key=AWS_SECRET,
        )

    def list_dates(self, start: str, end: str) -> List[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        dates = []
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix="raw/intraday/", Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                d = cp["Prefix"].rstrip("/").split("/")[-1]
                if len(d) == 10 and start <= d <= end:
                    dates.append(d)
        return sorted(dates)

    def list_tickers(self, date: str) -> List[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        tickers = set()
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"raw/intraday/{date}/"):
            for obj in page.get("Contents", []):
                fn = obj["Key"].split("/")[-1]
                if fn.endswith("_reg_1m.parquet"):
                    tickers.add(fn.replace("_reg_1m.parquet", ""))
        return sorted(tickers)

    def read(self, date: str, ticker: str, session: str = "reg") -> pd.DataFrame:
        key = f"raw/intraday/{date}/{ticker}_{session}_1m.parquet"
        try:
            obj = self.s3.get_object(Bucket=S3_BUCKET, Key=key)
            buf = io.BytesIO(obj["Body"].read())
            table = pq.read_table(buf)
            df = table.to_pandas(timestamp_as_object=True)
            # datetime 컬럼 문자열로 변환
            if "datetime" in df.columns:
                df["datetime"] = df["datetime"].astype(str)
            return df
        except Exception:
            return pd.DataFrame()

    def save_model(self, data: bytes, name: str, version: str):
        key = f"penny-ai/models/{name}/{version}.pt"
        self.s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data)
        logger.info(f"모델 저장: s3://{S3_BUCKET}/{key}")
        return key


# ─── 피처 엔지니어링 ─────────────────────────────────────────────────────────
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """1분봉 데이터에서 피처 계산"""
    if df.empty or len(df) < 20:
        return pd.DataFrame()

    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 수익률
    df["returns"] = close.pct_change().fillna(0)
    df["mom_5"] = close.pct_change(5).fillna(0)
    df["mom_10"] = close.pct_change(10).fillna(0)

    # 볼린저 밴드
    bb_window = 20
    ma = close.rolling(bb_window, min_periods=1).mean()
    std = close.rolling(bb_window, min_periods=1).std().fillna(0)
    bb_upper = ma + 2 * std
    bb_lower = ma - 2 * std
    bb_width = (bb_upper - bb_lower) / (ma + 1e-9)
    df["bb_position"] = (close - bb_lower) / (bb_upper - bb_lower + 1e-9)
    df["bb_width"] = bb_width
    df["bb_breakout_upper"] = (close > bb_upper).astype(float)

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi_overbought"] = (df["rsi"] > 70).astype(float)
    df["rsi_oversold"] = (df["rsi"] < 30).astype(float)

    # VWAP
    if "vwap" in df.columns:
        df["vwap_ratio"] = close / (df["vwap"] + 1e-9)
        df["above_vwap"] = (close > df["vwap"]).astype(float)
    else:
        typical = (high + low + close) / 3
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical * volume).cumsum()
        vwap = cum_tp_vol / (cum_vol + 1e-9)
        df["vwap_ratio"] = close / (vwap + 1e-9)
        df["above_vwap"] = (close > vwap).astype(float)

    # 거래량 비율
    vol_ma = volume.rolling(20, min_periods=1).mean()
    df["volume_ratio"] = volume / (vol_ma + 1e-9)
    df["volume_spike"] = (df["volume_ratio"] > 3).astype(float)

    # OFI (Order Flow Imbalance)
    df["ofi"] = ((close - low) - (high - close)) / (high - low + 1e-9)

    # EMA
    ema5 = close.ewm(span=5, adjust=False).mean()
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    df["ema_5_above_10"] = (ema5 > ema10).astype(float)
    df["ema_10_above_20"] = (ema10 > ema20).astype(float)

    # 정규화
    for col in ["open", "high", "low", "close"]:
        price_mean = df[col].mean()
        price_std = df[col].std() + 1e-9
        df[f"{col}_norm"] = (df[col] - price_mean) / price_std

    vol_mean = volume.mean()
    vol_std = volume.std() + 1e-9
    df["volume_norm"] = (volume - vol_mean) / vol_std

    return df.fillna(0)


FEATURE_COLS = [
    "returns", "mom_5", "mom_10",
    "bb_position", "bb_width", "bb_breakout_upper",
    "rsi", "rsi_overbought", "rsi_oversold",
    "vwap_ratio", "above_vwap",
    "volume_ratio", "volume_spike",
    "ofi",
    "ema_5_above_10", "ema_10_above_20",
    "open_norm", "high_norm", "low_norm", "close_norm", "volume_norm",
]
N_FEATURES = len(FEATURE_COLS)  # 21


# ─── 강화학습 환경 ────────────────────────────────────────────────────────────
COMMISSION = 0.001   # 0.1%
SLIPPAGE = 0.002     # 0.2%
TOTAL_COST = (COMMISSION + SLIPPAGE) * 2  # 왕복 ~0.6%

class PennyEnv(gym.Env):
    def __init__(self, df: pd.DataFrame, window: int = 60):
        super().__init__()
        self.df = df.reset_index(drop=True)
        self.window = window
        self.n_feat = N_FEATURES

        self.action_space = spaces.Discrete(3)  # 0=HOLD, 1=BUY, 2=SELL
        obs_dim = window * self.n_feat + 3
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(obs_dim,), dtype=np.float32)
        self._reset_state()

    def _reset_state(self):
        self.step_idx = self.window
        self.balance = 1_000_000.0
        self.initial_balance = 1_000_000.0
        self.position = 0
        self.entry_price = 0.0
        self.hold_steps = 0
        self.idle_steps = 0
        self.total_profit = 0.0
        self.trades = []
        self.portfolio_values = [self.initial_balance]

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self._reset_state()
        return self._obs(), {}

    def step(self, action):
        price = self._price()
        reward = 0.0

        if action == 1:  # BUY
            reward = self._buy(price)
        elif action == 2:  # SELL
            reward = self._sell(price)
        else:  # HOLD
            reward = self._hold()

        self.step_idx += 1
        if self.position > 0:
            self.hold_steps += 1
            if self.hold_steps >= 60:  # 60분 강제 청산
                reward += self._sell(price, forced=True)
        else:
            self.idle_steps += 1

        pv = self.balance + self.position * price
        self.portfolio_values.append(pv)

        terminated = self.step_idx >= len(self.df) - 1
        return self._obs(), float(reward), terminated, False, {"portfolio_value": pv}

    def _buy(self, price):
        if self.position > 0:
            return 0.0
        exec_price = price * (1 + SLIPPAGE)
        invest = self.balance * 0.5
        shares = int(invest / exec_price)
        if shares <= 0:
            return 0.0
        cost = shares * exec_price * (1 + COMMISSION)
        if cost > self.balance:
            return 0.0
        self.balance -= cost
        self.position = shares
        self.entry_price = exec_price
        self.hold_steps = 0
        self.idle_steps = 0
        return 0.0

    def _sell(self, price, forced=False):
        if self.position <= 0:
            return 0.0
        exec_price = price * (1 - SLIPPAGE)
        proceeds = self.position * exec_price * (1 - COMMISSION)
        profit_pct = (exec_price - self.entry_price) / self.entry_price
        net_pct = profit_pct - TOTAL_COST
        self.balance += proceeds
        self.total_profit += proceeds - self.position * self.entry_price
        self.position = 0
        self.entry_price = 0.0
        self.hold_steps = 0
        return self._reward(net_pct, profit_pct)

    def _reward(self, net_pct, raw_pct):
        if net_pct >= 0.20:
            r = 5.0
        elif net_pct >= 0.10:
            r = 3.0
        elif net_pct >= 0.05:
            r = 2.0
        elif net_pct <= -0.07:
            r = -2.0
        else:
            r = net_pct / 0.05 * 2.0 if net_pct >= 0 else net_pct / 0.07 * 2.0
        # 비용 못 커버하는 매매 패널티
        if 0 < raw_pct < TOTAL_COST:
            r -= 0.5
        return r

    def _hold(self):
        if self.position <= 0:
            self.idle_steps += 1
            if self.idle_steps >= 20:
                self.idle_steps = 0
                return -0.5
        return 0.0

    def _obs(self):
        start = max(0, self.step_idx - self.window)
        end = self.step_idx
        window_df = self.df.iloc[start:end][[c for c in FEATURE_COLS if c in self.df.columns]]
        if len(window_df) < self.window:
            pad = pd.DataFrame(np.zeros((self.window - len(window_df), len(window_df.columns))), columns=window_df.columns)
            window_df = pd.concat([pad, window_df], ignore_index=True)
        arr = window_df.fillna(0).values.astype(np.float32).flatten()
        price = self._price()
        pos_info = np.array([
            float(self.position > 0),
            (price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0.0,
            self.balance / self.initial_balance,
        ], dtype=np.float32)
        return np.concatenate([arr, pos_info])

    def _price(self):
        idx = min(self.step_idx, len(self.df) - 1)
        return float(self.df["close"].iloc[idx])


# ─── 메인 학습 파이프라인 ─────────────────────────────────────────────────────
def main():
    start_time = time.time()
    version = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("=" * 60)
    logger.info("페니스탁 AI 학습 시작")
    logger.info(f"버전: {version} | 디바이스: {DEVICE}")
    logger.info("=" * 60)

    send_telegram(
        f"🤖 *페니스탁 AI 학습 시작*\n\n"
        f"📅 학습: 2025-01-01 ~ 2025-09-30\n"
        f"📊 검증: 2025-10-01 ~ 2025-12-31\n"
        f"💻 디바이스: {DEVICE}\n"
        f"🔧 버전: {version}"
    )

    loader = DataLoader_S3()

    # ── 1. 학습 데이터 로드 ──────────────────────────────────────────────────
    logger.info("Step 1: S3에서 학습 데이터 로드 (2025-01 ~ 2025-09)")
    train_dates = loader.list_dates("2025-01-01", "2025-09-30")
    logger.info(f"학습 날짜: {len(train_dates)}일")

    train_dfs = []
    for i, date in enumerate(train_dates):
        tickers = loader.list_tickers(date)
        for ticker in tickers:
            df = loader.read(date, ticker, "reg")
            if df.empty or len(df) < 70:
                continue
            df_feat = compute_features(df)
            if df_feat.empty:
                continue
            df_feat["date"] = date
            df_feat["ticker"] = ticker
            train_dfs.append(df_feat)
        if (i + 1) % 20 == 0:
            logger.info(f"  {i+1}/{len(train_dates)}일 로드 완료 ({len(train_dfs)}개 시퀀스)")

    logger.info(f"학습 데이터 로드 완료: {len(train_dfs)}개 시퀀스")

    if not train_dfs:
        logger.error("학습 데이터 없음!")
        send_telegram("❌ 학습 데이터 없음! 중단.")
        return

    # ── 2. 검증 데이터 로드 ──────────────────────────────────────────────────
    logger.info("Step 2: 검증 데이터 로드 (2025-10 ~ 2025-12)")
    val_dates = loader.list_dates("2025-10-01", "2025-12-31")
    val_dfs = []
    for date in val_dates:
        tickers = loader.list_tickers(date)
        for ticker in tickers:
            df = loader.read(date, ticker, "reg")
            if df.empty or len(df) < 70:
                continue
            df_feat = compute_features(df)
            if not df_feat.empty:
                val_dfs.append(df_feat)
    logger.info(f"검증 데이터: {len(val_dfs)}개 시퀀스")

    # ── 3. PPO 환경 구성 ─────────────────────────────────────────────────────
    logger.info("Step 3: PPO 학습 환경 구성")
    WINDOW = 60

    # 충분한 길이의 데이터만 사용
    valid_train = [df for df in train_dfs if len(df) >= WINDOW + 20]
    valid_val = [df for df in val_dfs if len(df) >= WINDOW + 20]
    logger.info(f"유효 학습 시퀀스: {len(valid_train)}개 | 유효 검증 시퀀스: {len(valid_val)}개")

    if not valid_train:
        logger.error("유효한 학습 시퀀스 없음!")
        send_telegram("❌ 유효한 학습 시퀀스 없음! 중단.")
        return

    # 환경 팩토리
    def make_env(df):
        def _init():
            return PennyEnv(df, window=WINDOW)
        return _init

    # 병렬 환경 (최대 8개)
    n_envs = min(8, len(valid_train))
    import random
    sample_dfs = random.sample(valid_train, n_envs)
    vec_env = DummyVecEnv([make_env(df) for df in sample_dfs])

    logger.info(f"PPO 환경: {n_envs}개 병렬")

    # ── 4. PPO 학습 ──────────────────────────────────────────────────────────
    logger.info("Step 4: PPO 학습 시작 (500,000 스텝)")
    send_telegram("🏋️ PPO 학습 시작 (500,000 스텝)...")

    model = PPO(
        "MlpPolicy",
        vec_env,
        verbose=1,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        device=DEVICE,
        tensorboard_log=None,
    )

    model.learn(total_timesteps=500_000, progress_bar=False)
    logger.info("PPO 학습 완료!")

    # ── 5. 검증 평가 ─────────────────────────────────────────────────────────
    logger.info("Step 5: 검증 데이터 평가")
    val_returns = []
    if valid_val:
        n_eval = min(20, len(valid_val))
        eval_sample = random.sample(valid_val, n_eval)
        for df in eval_sample:
            env = PennyEnv(df, window=WINDOW)
            obs, _ = env.reset()
            done = False
            while not done:
                action, _ = model.predict(obs, deterministic=True)
                obs, _, terminated, truncated, _ = env.step(action)
                done = terminated or truncated
            metrics = env.portfolio_values
            if metrics:
                ret = (metrics[-1] - metrics[0]) / metrics[0]
                val_returns.append(ret)

    avg_val_return = np.mean(val_returns) if val_returns else 0.0
    logger.info(f"검증 평균 수익률: {avg_val_return*100:.2f}%")

    # ── 6. 모델 저장 ─────────────────────────────────────────────────────────
    logger.info("Step 6: 모델 S3 저장")
    model_path = f"/tmp/penny_trader_{version}.zip"
    model.save(model_path)
    with open(model_path, "rb") as f:
        model_bytes = f.read()
    loader.save_model(model_bytes, "trader", version)

    # ── 7. 완료 보고 ─────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    logger.info(f"학습 완료! 소요시간: {elapsed/60:.1f}분")

    send_telegram(
        f"✅ *페니스탁 AI 학습 완료!*\n\n"
        f"🔧 버전: {version}\n"
        f"⏱️ 소요시간: {elapsed/60:.1f}분\n"
        f"📊 학습 시퀀스: {len(valid_train)}개\n"
        f"📈 검증 평균 수익률: {avg_val_return*100:.2f}%\n"
        f"💻 디바이스: {DEVICE}\n"
        f"💾 모델 저장: s3://{S3_BUCKET}/penny-ai/models/trader/{version}.pt\n\n"
        f"🎯 다음 단계: 검증 테스트 (2025-10 ~ 2025-12)"
    )

    logger.info("=" * 60)
    logger.info("학습 파이프라인 완료")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
