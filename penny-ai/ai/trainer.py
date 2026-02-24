"""
Penny Stock AI Trainer
- PPO 알고리즘 (stable-baselines3)
- 학습/검증 기간 분리 (과적합 방지)
- S3에서 데이터 로드 → 학습 → S3에 모델 저장
"""
import os
import sys
import io
import logging
import boto3
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
from stable_baselines3.common.callbacks import EvalCallback, CheckpointCallback
from stable_baselines3.common.monitor import Monitor

# 경로 설정 (Colab 환경 대응)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from processor.feature_engine import build_features, FEATURE_COLS
from ai.environment import PennyStockEnv

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# S3 설정
S3_BUCKET = 'sungli-market-data'
S3_INTRADAY_PREFIX = 'raw/intraday/'
S3_MODEL_PREFIX = 'penny-ai/models/'
REGION = 'ap-northeast-2'

# 학습/검증 기간 분리
TRAIN_START = '2025-01-01'
TRAIN_END = '2025-09-30'
VALID_START = '2025-10-01'
VALID_END = '2025-12-31'

# 텔레그램 알림
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '5810895605')

# 학습 대상 종목
TICKERS = ['SOXL', 'TQQQ', 'SPXL', 'FNGU', 'LABU',
           'SOXS', 'SQQQ', 'SPXS', 'FNGD', 'LABD']


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        logger.info(f"[Telegram] {msg}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg}, timeout=10)
    except Exception as e:
        logger.warning(f"텔레그램 전송 실패: {e}")


def load_s3_data(s3_client, ticker: str, start: str, end: str, session_type: str = 'reg') -> pd.DataFrame:
    """S3에서 특정 티커의 1분봉 데이터 로드"""
    frames = []
    paginator = s3_client.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_INTRADAY_PREFIX):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # 날짜 파싱
            parts = key.split('/')
            if len(parts) < 4:
                continue
            date_str = parts[2]  # raw/intraday/2025-01-02/
            if date_str < start or date_str > end:
                continue
            filename = parts[-1]
            if not filename.startswith(ticker) or f'_{session_type}_1m.parquet' not in filename:
                continue

            try:
                obj_data = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
                df = pd.read_parquet(io.BytesIO(obj_data['Body'].read()))
                df['date'] = date_str
                frames.append(df)
            except Exception as e:
                logger.warning(f"파일 로드 실패 {key}: {e}")

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined.sort_index(inplace=True)
    return combined


def prepare_env(df: pd.DataFrame, window_size: int = 30):
    df_feat = build_features(df)
    env = PennyStockEnv(df_feat, window_size=window_size)
    env = Monitor(env)
    return DummyVecEnv([lambda: env])


def train():
    logger.info("🚀 페니스탁 AI 학습 시작!")
    send_telegram("🚀 페니스탁 AI 학습 시작!\n학습 기간: 2025-01-01 ~ 2025-09-30")

    s3 = boto3.client('s3', region_name=REGION)

    all_train_frames = []
    all_valid_frames = []

    for ticker in TICKERS:
        logger.info(f"📥 {ticker} 데이터 로드 중...")
        train_df = load_s3_data(s3, ticker, TRAIN_START, TRAIN_END, 'reg')
        valid_df = load_s3_data(s3, ticker, VALID_START, VALID_END, 'reg')

        if not train_df.empty:
            train_df['ticker'] = ticker
            all_train_frames.append(train_df)
            logger.info(f"  {ticker} 학습 데이터: {len(train_df)}행")
        if not valid_df.empty:
            valid_df['ticker'] = ticker
            all_valid_frames.append(valid_df)
            logger.info(f"  {ticker} 검증 데이터: {len(valid_df)}행")

    if not all_train_frames:
        logger.error("❌ 학습 데이터 없음!")
        send_telegram("❌ 학습 데이터 없음! 수집 상태 확인 필요")
        return

    train_df = pd.concat(all_train_frames, ignore_index=True)
    valid_df = pd.concat(all_valid_frames, ignore_index=True) if all_valid_frames else None

    logger.info(f"✅ 학습 데이터 총 {len(train_df)}행 로드 완료")

    # 환경 생성
    train_env = prepare_env(train_df)
    train_env = VecNormalize(train_env, norm_obs=True, norm_reward=True)

    # PPO 모델 생성
    model = PPO(
        'MlpPolicy',
        train_env,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        verbose=1,
        tensorboard_log='/tmp/penny_ai_tb/'
    )

    # 콜백
    callbacks = []

    if valid_df is not None and not valid_df.empty:
        valid_env = prepare_env(valid_df)
        valid_env = VecNormalize(valid_env, norm_obs=True, norm_reward=False, training=False)
        eval_cb = EvalCallback(
            valid_env,
            best_model_save_path='/tmp/penny_ai_best/',
            log_path='/tmp/penny_ai_eval/',
            eval_freq=10000,
            deterministic=True,
            verbose=1
        )
        callbacks.append(eval_cb)

    checkpoint_cb = CheckpointCallback(
        save_freq=50000,
        save_path='/tmp/penny_ai_checkpoints/',
        name_prefix='ppo_penny'
    )
    callbacks.append(checkpoint_cb)

    # 학습 실행
    total_timesteps = 500_000
    logger.info(f"🤖 PPO 학습 시작 (총 {total_timesteps:,} 스텝)")
    send_telegram(f"🤖 PPO 학습 중...\n총 {total_timesteps:,} 스텝\n예상 완료: 약 20~30분")

    model.learn(
        total_timesteps=total_timesteps,
        callback=callbacks,
        progress_bar=True
    )

    # 모델 S3 저장
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    model_path = f'/tmp/ppo_penny_{timestamp}'
    model.save(model_path)
    train_env.save(f'{model_path}_vecnorm.pkl')

    # S3 업로드
    model_key = f"{S3_MODEL_PREFIX}ppo_penny_{timestamp}.zip"
    s3.upload_file(f'{model_path}.zip', S3_BUCKET, model_key)
    logger.info(f"✅ 모델 S3 저장 완료: {model_key}")

    send_telegram(
        f"✅ 페니스탁 AI 학습 완료!\n"
        f"모델: {model_key}\n"
        f"학습 기간: {TRAIN_START} ~ {TRAIN_END}\n"
        f"총 스텝: {total_timesteps:,}\n"
        f"→ 검증 테스트 시작 준비 완료!"
    )

    logger.info("🎉 학습 완료!")


if __name__ == '__main__':
    train()
