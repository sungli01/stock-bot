"""
Polygon.io 시장 데이터 수집 모듈
- REST API로 종목 리스트 조회
- 5분봉/1분봉 데이터 조회
- 시총, 가격 필터링
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Stub 모드: API 키 없으면 모의 데이터 반환
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
USE_STUB = not POLYGON_API_KEY or POLYGON_API_KEY == "your_polygon_api_key_here"


class MarketDataClient:
    """Polygon.io REST API 클라이언트"""

    def __init__(self):
        if not USE_STUB:
            from polygon import RESTClient
            self.client = RESTClient(api_key=POLYGON_API_KEY)
        else:
            self.client = None
            logger.warning("⚠️ Polygon API 키 없음 — stub 모드로 실행")

    def get_all_tickers(self, min_price: float = 1.0, min_market_cap: float = 50_000_000) -> list[dict]:
        """
        전종목 조회 + 기본 필터링 (가격, 시총)
        Returns: [{"ticker": "AAPL", "name": "Apple Inc", "market_cap": ..., "price": ...}, ...]
        """
        if USE_STUB:
            return self._stub_tickers()

        tickers = []
        try:
            for t in self.client.list_tickers(
                market="stocks",
                active=True,
                limit=1000,
            ):
                # 가격/시총 필터는 snapshot에서 처리
                tickers.append({
                    "ticker": t.ticker,
                    "name": t.name or "",
                    "market_cap": getattr(t, "market_cap", 0) or 0,
                })
        except Exception as e:
            logger.error(f"종목 리스트 조회 실패: {e}")
        return tickers

    def get_bars(self, ticker: str, timeframe: str = "5min", limit: int = 50) -> pd.DataFrame:
        """
        분봉 데이터 조회
        timeframe: "1min" or "5min"
        Returns: DataFrame with columns [open, high, low, close, volume, timestamp]
        """
        if USE_STUB:
            return self._stub_bars(ticker, limit)

        multiplier = 5 if timeframe == "5min" else 1
        timespan = "minute"
        end = datetime.utcnow()
        start = end - timedelta(hours=12)

        try:
            aggs = self.client.get_aggs(
                ticker=ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_=start.strftime("%Y-%m-%d"),
                to=end.strftime("%Y-%m-%d"),
                limit=limit,
            )
            if not aggs:
                return pd.DataFrame()

            rows = [{
                "open": a.open,
                "high": a.high,
                "low": a.low,
                "close": a.close,
                "volume": a.volume,
                "timestamp": pd.Timestamp(a.timestamp, unit="ms"),
            } for a in aggs]
            return pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"{ticker} 분봉 조회 실패: {e}")
            return pd.DataFrame()

    def get_snapshot(self, ticker: str) -> Optional[dict]:
        """종목 스냅샷 (현재가, 거래량, 변동률 등)"""
        if USE_STUB:
            return self._stub_snapshot(ticker)

        try:
            snap = self.client.get_snapshot_ticker("stocks", ticker)
            return {
                "ticker": ticker,
                "price": snap.day.close if snap.day else 0,
                "volume": snap.day.volume if snap.day else 0,
                "prev_close": snap.prev_day.close if snap.prev_day else 0,
                "change_pct": snap.todays_change_percent or 0,
                "market_cap": getattr(snap, "market_cap", 0) or 0,
            }
        except Exception as e:
            logger.error(f"{ticker} 스냅샷 조회 실패: {e}")
            return None

    # ─── Stub 메서드 (모의 데이터) ──────────────────────────────
    def _stub_tickers(self) -> list[dict]:
        """테스트용 모의 종목 리스트"""
        import random
        stubs = [
            {"ticker": "NVDA", "name": "NVIDIA Corp", "market_cap": 3_500_000_000_000},
            {"ticker": "AAPL", "name": "Apple Inc", "market_cap": 2_800_000_000_000},
            {"ticker": "TSLA", "name": "Tesla Inc", "market_cap": 800_000_000_000},
            {"ticker": "AMD", "name": "Advanced Micro Devices", "market_cap": 250_000_000_000},
            {"ticker": "PLTR", "name": "Palantir Technologies", "market_cap": 60_000_000_000},
            {"ticker": "SOFI", "name": "SoFi Technologies", "market_cap": 12_000_000_000},
            {"ticker": "MARA", "name": "Marathon Digital", "market_cap": 5_000_000_000},
            {"ticker": "RIOT", "name": "Riot Platforms", "market_cap": 3_000_000_000},
            {"ticker": "SOUN", "name": "SoundHound AI", "market_cap": 2_000_000_000},
            {"ticker": "SMCI", "name": "Super Micro Computer", "market_cap": 15_000_000_000},
        ]
        return stubs

    def _stub_bars(self, ticker: str, limit: int) -> pd.DataFrame:
        """테스트용 모의 분봉 데이터"""
        import numpy as np
        np.random.seed(hash(ticker) % 2**31)
        base_price = {"NVDA": 142, "AAPL": 185, "TSLA": 250}.get(ticker, 50)
        prices = base_price + np.cumsum(np.random.randn(limit) * 0.5)
        volumes = np.random.randint(5000, 50000, size=limit)
        now = datetime.utcnow()
        timestamps = [now - timedelta(minutes=5 * (limit - i)) for i in range(limit)]
        return pd.DataFrame({
            "open": prices - np.random.rand(limit) * 0.3,
            "high": prices + np.random.rand(limit) * 0.5,
            "low": prices - np.random.rand(limit) * 0.5,
            "close": prices,
            "volume": volumes,
            "timestamp": timestamps,
        })

    def _stub_snapshot(self, ticker: str) -> dict:
        """테스트용 모의 스냅샷"""
        import random
        base = {"NVDA": 142.5, "AAPL": 185.3, "TSLA": 250.0}.get(ticker, 50.0)
        change = random.uniform(-3, 8)
        return {
            "ticker": ticker,
            "price": round(base * (1 + change / 100), 2),
            "volume": random.randint(10000, 500000),
            "prev_close": base,
            "change_pct": round(change, 2),
            "market_cap": random.randint(100_000_000, 3_000_000_000_000),
        }
