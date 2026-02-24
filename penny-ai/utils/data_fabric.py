"""
MarketDataFabric - AI 모델 불가지론적 데이터 인터페이스
어떤 AI 모델도 이 인터페이스로 바로 S3 데이터 접근 가능
"""

import os
import json
import logging
from io import BytesIO
from typing import Optional, List, Dict
import boto3
import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


class MarketDataFabric:
    """
    S3 기반 마켓 데이터 패브릭
    - get_timeseries(): 시계열 데이터 조회
    - get_events(): 이벤트 데이터 조회
    - get_case(): 케이스 분류 조회
    - query(): SQL 스타일 쿼리
    - to_prompt(): LLM용 텍스트 변환
    - export(): 다양한 포맷으로 내보내기
    """

    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("S3_REGION", "ap-northeast-2"),
        )
        self.bucket = os.environ.get("S3_BUCKET", "sungli-market-data")
        self._index_cache = None

    # ─────────────────────────────────────────
    # 핵심 인터페이스
    # ─────────────────────────────────────────

    def get_timeseries(
        self,
        ticker: str,
        date: str,
        session: str = "both",   # "pre" | "reg" | "both"
        interval: str = "1m",    # "1m" | "3m" | "5m"
    ) -> Optional[pd.DataFrame]:
        """
        시계열 1분봉 데이터 반환

        Args:
            ticker: 종목 코드 (예: "ABCD")
            date: 날짜 (예: "2025-01-15")
            session: "pre"(프리마켓) | "reg"(본장) | "both"(전체)
            interval: "1m" | "3m" | "5m"

        Returns:
            pd.DataFrame with columns: [datetime, open, high, low, close, volume, vwap, session]
        """
        dfs = []

        if session in ("pre", "both"):
            df_pre = self._load_parquet(f"raw/intraday/{date}/{ticker}_pre_1m.parquet")
            if df_pre is not None:
                df_pre["session"] = "premarket"
                dfs.append(df_pre)

        if session in ("reg", "both"):
            df_reg = self._load_parquet(f"raw/intraday/{date}/{ticker}_reg_1m.parquet")
            if df_reg is not None:
                df_reg["session"] = "regular"
                dfs.append(df_reg)

        if not dfs:
            return None

        df = pd.concat(dfs, ignore_index=True)

        # 시간 정렬
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)

        # 집계 (3m, 5m)
        if interval != "1m" and "timestamp" in df.columns:
            df = self._resample(df, interval)

        return df

    def get_daily(self, ticker: str, date: str) -> Optional[dict]:
        """일봉 데이터 반환"""
        key = f"raw/daily/{date}/{ticker}.json"
        return self._load_json(key)

    def get_top10(self, date: str) -> Optional[list]:
        """당일 상위 10종목 반환"""
        key = f"raw/daily/{date}/top10.json"
        return self._load_json(key)

    def get_events(self, ticker: str, date: str) -> Optional[dict]:
        """이벤트 데이터 반환 (1차/2차 상승, 쿨링, BB돌파)"""
        key = f"ontology/events/{date}/{ticker}_events.json"
        return self._load_json(key)

    def get_case(self, ticker: str, date: str) -> Optional[dict]:
        """케이스 분류 반환 (A/B/C/D/E형)"""
        key = f"ontology/cases/{ticker}_{date}_case.json"
        return self._load_json(key)

    def get_news(self, ticker: str, date: str) -> Optional[list]:
        """뉴스 데이터 반환"""
        key = f"news/{date}/{ticker}_news.json"
        return self._load_json(key)

    def get_features(self, ticker: str, date: str) -> Optional[pd.DataFrame]:
        """피처 데이터 반환 (BB, RSI, VWAP 등)"""
        key = f"derived/features/{date}/{ticker}_features.parquet"
        return self._load_parquet(key)

    def query(self, filters: dict) -> List[dict]:
        """
        시계열 인덱스 쿼리

        Args:
            filters: {
                "date_from": "2025-01-01",
                "date_to": "2025-12-31",
                "case_type": "A",
                "min_change_pct": 20.0,
                "max_rank": 5,
            }

        Returns:
            조건에 맞는 종목-일 목록
        """
        index = self._get_index()
        results = []

        for item in index:
            date = item.get("date", "")
            change_pct = item.get("change_pct", 0)
            rank = item.get("rank", 99)

            if filters.get("date_from") and date < filters["date_from"]:
                continue
            if filters.get("date_to") and date > filters["date_to"]:
                continue
            if filters.get("min_change_pct") and change_pct < filters["min_change_pct"]:
                continue
            if filters.get("max_rank") and rank > filters["max_rank"]:
                continue

            # 케이스 필터 (케이스 데이터 로드 필요)
            if filters.get("case_type"):
                case = self.get_case(item["ticker"], date)
                if not case or case.get("type") != filters["case_type"]:
                    continue

            results.append(item)

        return results

    def to_prompt(self, ticker: str, date: str, max_bars: int = 30) -> str:
        """
        LLM용 텍스트 변환
        어떤 AI 모델도 바로 사용 가능한 형식

        Returns:
            str: 구조화된 텍스트
        """
        lines = [f"# 종목 분석: {ticker} ({date})\n"]

        # 일봉
        daily = self.get_daily(ticker, date)
        if daily:
            lines.append(f"## 일봉 데이터")
            lines.append(f"- 시작가: ${daily.get('o', 'N/A'):.4f}")
            lines.append(f"- 최고가: ${daily.get('h', 'N/A'):.4f}")
            lines.append(f"- 최저가: ${daily.get('l', 'N/A'):.4f}")
            lines.append(f"- 종가: ${daily.get('c', 'N/A'):.4f}")
            lines.append(f"- 거래량: {daily.get('v', 0):,}")
            lines.append(f"- 상승률: {daily.get('change_pct', 0):+.2f}%\n")

        # 케이스
        case = self.get_case(ticker, date)
        if case:
            lines.append(f"## 케이스 분류")
            lines.append(f"- 유형: {case.get('type', 'N/A')}형")
            lines.append(f"- 2차 상승: {'확인' if case.get('second_surge') else '없음'}")
            lines.append(f"- BB 돌파: {'확인' if case.get('bb_break') else '없음'}\n")

        # 이벤트
        events = self.get_events(ticker, date)
        if events:
            lines.append(f"## 주요 이벤트")
            for evt in events.get("events", [])[:5]:
                lines.append(f"- [{evt.get('time', '')}] {evt.get('type', '')} @ ${evt.get('price', 0):.4f}")
            lines.append("")

        # 1분봉 (최근 N개)
        bars = self.get_timeseries(ticker, date, session="reg")
        if bars is not None and len(bars) > 0:
            lines.append(f"## 본장 1분봉 (최근 {min(max_bars, len(bars))}봉)")
            lines.append("시간 | 시가 | 고가 | 저가 | 종가 | 거래량")
            lines.append("-" * 60)
            for _, row in bars.tail(max_bars).iterrows():
                t = str(row.get("timestamp", ""))[-8:][:5]
                lines.append(
                    f"{t} | ${row.get('open',0):.3f} | ${row.get('high',0):.3f} | "
                    f"${row.get('low',0):.3f} | ${row.get('close',0):.3f} | {int(row.get('volume',0)):,}"
                )
            lines.append("")

        # 뉴스
        news = self.get_news(ticker, date)
        if news:
            lines.append(f"## 뉴스 ({len(news)}건)")
            for n in news[:3]:
                lines.append(f"- {n.get('published_utc', '')[:10]} {n.get('title', '')}")

        return "\n".join(lines)

    def export(self, ticker: str, date: str, format: str = "parquet") -> bytes:
        """
        다양한 포맷으로 내보내기

        Args:
            format: "parquet" | "csv" | "json" | "arrow"
        """
        df = self.get_timeseries(ticker, date)
        if df is None:
            return b""

        buf = BytesIO()
        if format == "parquet":
            df.to_parquet(buf, index=False)
        elif format == "csv":
            df.to_csv(buf, index=False)
            return buf.getvalue()
        elif format == "json":
            return df.to_json(orient="records").encode()
        elif format == "arrow":
            import pyarrow as pa
            table = pa.Table.from_pandas(df)
            import pyarrow.ipc as ipc
            writer = ipc.new_stream(buf, table.schema)
            writer.write_table(table)
            writer.close()

        buf.seek(0)
        return buf.read()

    def list_dates(self, ticker: str = None) -> List[str]:
        """수집된 날짜 목록 반환"""
        index = self._get_index()
        if ticker:
            dates = [item["date"] for item in index if item.get("ticker") == ticker]
        else:
            dates = list(set(item["date"] for item in index))
        return sorted(dates)

    def list_tickers(self, date: str = None) -> List[str]:
        """수집된 종목 목록 반환"""
        index = self._get_index()
        if date:
            tickers = [item["ticker"] for item in index if item.get("date") == date]
        else:
            tickers = list(set(item["ticker"] for item in index))
        return sorted(tickers)

    def get_stats(self) -> dict:
        """전체 데이터 통계"""
        return self._load_json("catalog/stats.json") or {}

    # ─────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────

    def _load_parquet(self, key: str) -> Optional[pd.DataFrame]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            buf = BytesIO(obj["Body"].read())
            return pd.read_parquet(buf)
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception as e:
            logger.debug(f"parquet 로드 실패 ({key}): {e}")
            return None

    def _load_json(self, key: str) -> Optional[dict]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception:
            return None

    def _get_index(self) -> list:
        if self._index_cache is None:
            self._index_cache = self._load_json("ontology/timeseries_index.json") or []
        return self._index_cache

    def _resample(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """1분봉을 N분봉으로 집계"""
        minutes = int(interval.replace("m", ""))
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        df = df.resample(f"{minutes}min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "vwap": "mean",
        }).dropna()
        return df.reset_index()
