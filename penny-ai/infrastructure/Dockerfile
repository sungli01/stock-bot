FROM python:3.11-slim

# 시스템 의존성
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 로그 디렉토리
RUN mkdir -p /app/logs

# 환경변수 기본값
ENV PAPER_MODE=true
ENV SEED_AMOUNT=1000000
ENV S3_REGION=ap-northeast-2
ENV S3_BUCKET=sungli-market-data

# 헬스체크
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import boto3; print('OK')" || exit 1

# 실행
CMD ["python", "main.py", "--mode", "trade"]
