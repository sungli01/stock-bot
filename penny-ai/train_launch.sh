#!/bin/bash
# EC2 GPU 인스턴스 자동 학습 스크립트
set -e

LOG=/home/ubuntu/train.log
exec > >(tee -a $LOG) 2>&1

echo "=== 페니스탁 AI 학습 시작 $(date) ==="

# 1. 환경 설정
cd /home/ubuntu
export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}  # 환경변수에서 로드
export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}  # 환경변수에서 로드
export AWS_DEFAULT_REGION=ap-northeast-2
export TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
export TELEGRAM_CHAT_ID=5810895605
export S3_BUCKET=sungli-market-data

# 2. GitHub에서 코드 클론
echo "=== 코드 클론 ==="
git clone https://github.com/sungli01/penny-ai.git || (cd penny-ai && git pull)
cd penny-ai

# 3. 패키지 설치
echo "=== 패키지 설치 ==="
pip install -q -r requirements.txt

# 4. GPU 확인
python3 -c "import torch; print(f'CUDA: {torch.cuda.is_available()}, GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}')"

# 5. 학습 실행
echo "=== AI 학습 실행 ==="
python3 -c "
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

from ai.trainer import AITrainer
trainer = AITrainer()
results = trainer.run(
    start_date='2025-01-01',
    end_date='2025-09-30',
    val_start='2025-10-01',
    val_end='2025-12-31',
    test_start='2026-01-01',
    feeder_epochs=100,
    trader_timesteps=500000,
)
print('학습 결과:', results)
"

echo "=== 학습 완료 $(date) ==="

# 6. 완료 후 인스턴스 자동 종료
shutdown -h now
