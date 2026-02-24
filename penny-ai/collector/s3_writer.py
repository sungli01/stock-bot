"""
S3 저장 모듈
온톨로지 구조: s3://sungli-market-data/penny-ai/{year}/{month}/{date}/{ticker}/
"""

import os
import io
import json
import logging
from datetime import datetime
from typing import Optional
import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


class S3Writer:
    def __init__(
        self,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        prefix: str = "penny-ai",
    ):
        self.bucket = bucket or os.environ.get("S3_BUCKET", "sungli-market-data")
        self.region = region or os.environ.get("S3_REGION", "ap-northeast-2")
        self.prefix = prefix

        self.s3 = boto3.client(
            "s3",
            region_name=self.region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )

    def _build_key(self, trade_date: str, ticker: str, data_type: str, ext: str = "parquet") -> str:
        """
        S3 키 생성
        예: penny-ai/2024/01/2024-01-15/AAPL/minute_bars.parquet
        """
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        year = dt.strftime("%Y")
        month = dt.strftime("%m")
        return f"{self.prefix}/{year}/{month}/{trade_date}/{ticker}/{data_type}.{ext}"

    def write_dataframe(self, df: pd.DataFrame, trade_date: str, ticker: str, data_type: str) -> str:
        """DataFrame을 Parquet으로 S3에 저장"""
        if df.empty:
            logger.warning(f"{ticker}/{data_type}: 빈 DataFrame, 저장 스킵")
            return ""

        key = self._build_key(trade_date, ticker, data_type, "parquet")

        # timestamp 컬럼이 있으면 문자열로 변환 (pyarrow 호환)
        df_save = df.copy()
        for col in df_save.columns:
            if pd.api.types.is_datetime64_any_dtype(df_save[col]):
                df_save[col] = df_save[col].astype(str)

        buffer = io.BytesIO()
        table = pa.Table.from_pandas(df_save, preserve_index=False)
        pq.write_table(table, buffer, compression="snappy")
        buffer.seek(0)

        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=buffer.getvalue(),
            ContentType="application/octet-stream",
        )
        logger.info(f"저장 완료: s3://{self.bucket}/{key} ({len(df)} rows)")
        return f"s3://{self.bucket}/{key}"

    def write_json(self, data: dict | list, trade_date: str, ticker: str, data_type: str) -> str:
        """JSON 데이터를 S3에 저장"""
        key = self._build_key(trade_date, ticker, data_type, "json")
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        logger.info(f"저장 완료: s3://{self.bucket}/{key}")
        return f"s3://{self.bucket}/{key}"

    def write_metadata(self, metadata: dict, trade_date: str) -> str:
        """날짜별 메타데이터 저장"""
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        key = f"{self.prefix}/{dt.year}/{dt.month:02d}/{trade_date}/metadata.json"
        body = json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        logger.info(f"메타데이터 저장: s3://{self.bucket}/{key}")
        return f"s3://{self.bucket}/{key}"

    def read_dataframe(self, trade_date: str, ticker: str, data_type: str) -> pd.DataFrame:
        """S3에서 Parquet DataFrame 읽기"""
        key = self._build_key(trade_date, ticker, data_type, "parquet")
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            buffer = io.BytesIO(obj["Body"].read())
            return pd.read_parquet(buffer)
        except self.s3.exceptions.NoSuchKey:
            logger.warning(f"S3 키 없음: {key}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"S3 읽기 실패 {key}: {e}")
            return pd.DataFrame()

    def read_json(self, trade_date: str, ticker: str, data_type: str) -> dict | list:
        """S3에서 JSON 읽기"""
        key = self._build_key(trade_date, ticker, data_type, "json")
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except self.s3.exceptions.NoSuchKey:
            logger.warning(f"S3 키 없음: {key}")
            return {}
        except Exception as e:
            logger.error(f"S3 읽기 실패 {key}: {e}")
            return {}

    def list_dates(self, year: str = None, month: str = None) -> list[str]:
        """수집된 날짜 목록 반환"""
        prefix = self.prefix
        if year:
            prefix += f"/{year}"
            if month:
                prefix += f"/{month:02d}"

        paginator = self.s3.get_paginator("list_objects_v2")
        dates = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                parts = cp["Prefix"].rstrip("/").split("/")
                # penny-ai/year/month/date/ 구조에서 date 추출
                if len(parts) >= 4:
                    date_str = parts[3]
                    if len(date_str) == 10 and date_str[4] == "-":
                        dates.add(date_str)
        return sorted(dates)

    def list_tickers(self, trade_date: str) -> list[str]:
        """특정 날짜의 수집된 종목 목록 반환"""
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
        prefix = f"{self.prefix}/{dt.year}/{dt.month:02d}/{trade_date}/"
        paginator = self.s3.get_paginator("list_objects_v2")
        tickers = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                parts = cp["Prefix"].rstrip("/").split("/")
                if len(parts) >= 5:
                    tickers.add(parts[4])
        return sorted(tickers)

    def save_model(self, model_bytes: bytes, model_name: str, version: str) -> str:
        """AI 모델 저장"""
        key = f"{self.prefix}/models/{model_name}/{version}.pt"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=model_bytes,
            ContentType="application/octet-stream",
        )
        logger.info(f"모델 저장: s3://{self.bucket}/{key}")
        return f"s3://{self.bucket}/{key}"

    def load_model(self, model_name: str, version: str = "latest") -> bytes:
        """AI 모델 로드"""
        if version == "latest":
            # 최신 버전 찾기
            prefix = f"{self.prefix}/models/{model_name}/"
            paginator = self.s3.get_paginator("list_objects_v2")
            keys = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            if not keys:
                raise FileNotFoundError(f"모델 없음: {model_name}")
            key = sorted(keys)[-1]  # 최신 파일
        else:
            key = f"{self.prefix}/models/{model_name}/{version}.pt"

        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()
