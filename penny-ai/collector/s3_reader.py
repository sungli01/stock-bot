"""
S3 실제 데이터 경로 읽기 모듈
실제 저장 구조: s3://sungli-market-data/raw/intraday/{date}/{TICKER}_{type}_1m.parquet
"""

import os
import io
import logging
from datetime import datetime, timedelta
from typing import Optional, List
import boto3
import pandas as pd

logger = logging.getLogger(__name__)


class S3Reader:
    """실제 S3 데이터 경로에 맞는 리더"""

    def __init__(self):
        self.bucket = os.environ.get("S3_BUCKET", "sungli-market-data")
        self.region = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2")
        self.s3 = boto3.client(
            "s3",
            region_name=self.region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )

    def list_dates(self, start_date: str, end_date: str) -> List[str]:
        """수집된 날짜 목록 반환 (start_date ~ end_date 범위)"""
        paginator = self.s3.get_paginator("list_objects_v2")
        dates = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix="raw/intraday/", Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                date_str = cp["Prefix"].rstrip("/").split("/")[-1]
                if len(date_str) == 10 and start_date <= date_str <= end_date:
                    dates.add(date_str)
        return sorted(dates)

    def list_tickers(self, trade_date: str) -> List[str]:
        """특정 날짜의 종목 목록 반환"""
        prefix = f"raw/intraday/{trade_date}/"
        paginator = self.s3.get_paginator("list_objects_v2")
        tickers = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                filename = obj["Key"].split("/")[-1]
                # TICKER_reg_1m.parquet 또는 TICKER_pre_1m.parquet
                if filename.endswith("_reg_1m.parquet"):
                    ticker = filename.replace("_reg_1m.parquet", "")
                    tickers.add(ticker)
        return sorted(tickers)

    def read_minute_bars(self, trade_date: str, ticker: str, session: str = "reg") -> pd.DataFrame:
        """
        1분봉 데이터 읽기
        session: 'reg' (본장) 또는 'pre' (프리마켓)
        """
        key = f"raw/intraday/{trade_date}/{ticker}_{session}_1m.parquet"
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            buffer = io.BytesIO(obj["Body"].read())
            df = pd.read_parquet(buffer)
            return df
        except self.s3.exceptions.NoSuchKey:
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"읽기 실패 {key}: {e}")
            return pd.DataFrame()

    def read_combined(self, trade_date: str, ticker: str) -> pd.DataFrame:
        """프리마켓 + 본장 합쳐서 반환"""
        pre = self.read_minute_bars(trade_date, ticker, "pre")
        reg = self.read_minute_bars(trade_date, ticker, "reg")

        frames = [df for df in [pre, reg] if not df.empty]
        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)

        # timestamp 정렬
        if "timestamp" in combined.columns:
            combined = combined.sort_values("timestamp").reset_index(drop=True)
        elif "t" in combined.columns:
            combined = combined.sort_values("t").reset_index(drop=True)

        return combined

    def save_model(self, model_bytes: bytes, model_name: str, version: str) -> str:
        """AI 모델 저장"""
        key = f"penny-ai/models/{model_name}/{version}.pt"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=model_bytes,
            ContentType="application/octet-stream",
        )
        logger.info(f"모델 저장: s3://{self.bucket}/{key}")
        return f"s3://{self.bucket}/{key}"
