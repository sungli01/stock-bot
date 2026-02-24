"""
Polygon API 클라이언트
일봉, 1분봉(프리마켓+본장), 뉴스 수집
"""

import os
import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional
import requests
import pandas as pd

logger = logging.getLogger(__name__)


class PolygonClient:
    BASE_URL = "https://api.polygon.io"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("POLYGON_API_KEY")
        if not self.api_key:
            raise ValueError("POLYGON_API_KEY가 설정되지 않았습니다.")
        self.session = requests.Session()
        self.session.params = {"apiKey": self.api_key}

    def _get(self, endpoint: str, params: dict = None, retries: int = 3) -> dict:
        url = f"{self.BASE_URL}{endpoint}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params or {}, timeout=30)
                if resp.status_code == 429:
                    wait = 60
                    logger.warning(f"Rate limit 초과. {wait}초 대기...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"API 요청 실패 (시도 {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"API 요청 최종 실패: {endpoint}")

    def _get_paginated(self, endpoint: str, params: dict = None) -> list:
        """페이지네이션 처리하여 전체 결과 반환"""
        results = []
        params = params or {}
        url = f"{self.BASE_URL}{endpoint}"

        while url:
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                logger.warning("Rate limit. 60초 대기...")
                time.sleep(60)
                continue
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            next_url = data.get("next_url")
            if next_url:
                url = next_url
                params = {}  # next_url에 이미 파라미터 포함
            else:
                break
            time.sleep(0.2)

        return results

    def get_top_gainers(self, trade_date: str, min_price: float = 0.5, max_price: float = 50.0,
                        min_volume: int = 500000, top_n: int = 10) -> list[dict]:
        """
        특정 날짜의 상승률 상위 N개 종목 반환
        trade_date: 'YYYY-MM-DD'
        """
        # Polygon Grouped Daily endpoint
        endpoint = f"/v2/aggs/grouped/locale/us/market/stocks/{trade_date}"
        params = {"adjusted": "false"}
        data = self._get(endpoint, params)
        results = data.get("results", [])

        if not results:
            logger.warning(f"{trade_date}: 데이터 없음")
            return []

        df = pd.DataFrame(results)
        # 컬럼: T(ticker), o(open), h(high), l(low), c(close), v(volume), vw(vwap), n(transactions)
        df = df.rename(columns={"T": "ticker", "o": "open", "h": "high",
                                "l": "low", "c": "close", "v": "volume",
                                "vw": "vwap", "n": "transactions"})
        # 가격 필터
        df = df[(df["open"] >= min_price) & (df["open"] <= max_price)]
        # 거래량 필터
        df = df[df["volume"] >= min_volume]
        # 상승률 계산
        df["change_pct"] = (df["close"] - df["open"]) / df["open"] * 100
        # 상승 종목만
        df = df[df["change_pct"] > 0]
        # 상위 N개
        df = df.nlargest(top_n, "change_pct")

        return df.to_dict("records")

    def get_minute_bars(self, ticker: str, from_date: str, to_date: str,
                        multiplier: int = 1, timespan: str = "minute",
                        extended_hours: bool = True) -> pd.DataFrame:
        """
        1분봉 데이터 수집 (프리마켓 포함)
        from_date, to_date: 'YYYY-MM-DD'
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        params = {
            "adjusted": "false",
            "sort": "asc",
            "limit": 50000,
            "extended_hours": "true" if extended_hours else "false",
        }
        results = self._get_paginated(endpoint, params)

        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        df = df.rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume",
            "vw": "vwap", "n": "transactions"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York")
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["ticker"] = ticker
        return df

    def get_premarket_bars(self, ticker: str, trade_date: str) -> pd.DataFrame:
        """프리마켓(04:00~09:30 ET) 1분봉"""
        df = self.get_minute_bars(ticker, trade_date, trade_date, extended_hours=True)
        if df.empty:
            return df
        # 프리마켓 필터: 04:00 ~ 09:30
        df = df[
            (df["timestamp"].dt.hour >= 4) &
            (df["timestamp"].dt.hour < 9) |
            ((df["timestamp"].dt.hour == 9) & (df["timestamp"].dt.minute < 30))
        ]
        df["session"] = "premarket"
        return df.reset_index(drop=True)

    def get_regular_session_bars(self, ticker: str, trade_date: str) -> pd.DataFrame:
        """본장(09:30~16:00 ET) 1분봉"""
        df = self.get_minute_bars(ticker, trade_date, trade_date, extended_hours=True)
        if df.empty:
            return df
        # 본장 필터: 09:30 ~ 16:00
        mask = (
            ((df["timestamp"].dt.hour == 9) & (df["timestamp"].dt.minute >= 30)) |
            ((df["timestamp"].dt.hour > 9) & (df["timestamp"].dt.hour < 16))
        )
        df = df[mask]
        df["session"] = "regular"
        return df.reset_index(drop=True)

    def get_all_session_bars(self, ticker: str, trade_date: str) -> pd.DataFrame:
        """프리마켓 + 본장 전체 1분봉"""
        df = self.get_minute_bars(ticker, trade_date, trade_date, extended_hours=True)
        if df.empty:
            return df

        # 04:00 ~ 16:00 ET
        mask = (
            (df["timestamp"].dt.hour >= 4) & (df["timestamp"].dt.hour < 16)
        )
        df = df[mask]

        def classify_session(row):
            h, m = row["timestamp"].hour, row["timestamp"].minute
            if h < 9 or (h == 9 and m < 30):
                return "premarket"
            return "regular"

        df["session"] = df.apply(classify_session, axis=1)
        return df.reset_index(drop=True)

    def get_news(self, ticker: str, published_utc_gte: str, published_utc_lte: str,
                 limit: int = 50) -> list[dict]:
        """
        종목 뉴스 수집
        published_utc_gte/lte: 'YYYY-MM-DD'
        """
        endpoint = "/v2/reference/news"
        params = {
            "ticker": ticker,
            "published_utc.gte": published_utc_gte,
            "published_utc.lte": published_utc_lte,
            "limit": limit,
            "sort": "published_utc",
            "order": "desc",
        }
        results = self._get_paginated(endpoint, params)
        return results

    def get_ticker_details(self, ticker: str) -> dict:
        """종목 기본 정보"""
        endpoint = f"/v3/reference/tickers/{ticker}"
        data = self._get(endpoint)
        return data.get("results", {})

    def get_snapshot_all_tickers(self, tickers: list[str] = None) -> dict:
        """실시간 스냅샷 (당일 현재 데이터)"""
        endpoint = "/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {}
        if tickers:
            params["tickers"] = ",".join(tickers)
        data = self._get(endpoint, params)
        return {item["ticker"]: item for item in data.get("tickers", [])}

    def get_last_trade(self, ticker: str) -> dict:
        """최근 체결 데이터"""
        endpoint = f"/v2/last/trade/{ticker}"
        data = self._get(endpoint)
        return data.get("results", {})
