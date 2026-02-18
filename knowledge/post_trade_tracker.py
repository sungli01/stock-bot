"""
Post-Trade Tracker
- 매매 완료 종목 D+1~D+5 추적
- 일봉(OHLCV) 수집
- Polygon news API로 뉴스/급등 원인 수집
- ticker_details API로 섹터/산업 정보
- data/post_trade/ 디렉토리에 JSON 저장
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "post_trade")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


class PostTradeTracker:
    """매매 완료 종목 사후 추적"""

    def __init__(self):
        _ensure_dir()

    def record_trade(self, ticker: str, trade_date: str, trade_info: dict):
        """매매 완료 시 호출 — 초기 JSON 생성"""
        filename = f"{ticker}_{trade_date}.json"
        path = os.path.join(DATA_DIR, filename)

        record = {
            "ticker": ticker,
            "trade_date": trade_date,
            "trade_info": trade_info,
            "daily_bars": {},
            "news": [],
            "sector": None,
            "industry": None,
            "analysis": {"cause": "unknown", "tags": []},
            "tracking_status": "active",
            "created_at": datetime.utcnow().isoformat(),
            "last_updated": datetime.utcnow().isoformat(),
        }

        # 즉시 ticker details 조회
        details = self._fetch_ticker_details(ticker)
        if details:
            record["sector"] = details.get("sector")
            record["industry"] = details.get("industry")

        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)

        logger.info(f"📝 Post-trade 기록 생성: {filename}")

    def update_all(self):
        """모든 active 기록 업데이트 (장 마감 후 호출)"""
        _ensure_dir()
        for filename in os.listdir(DATA_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(DATA_DIR, filename)
            try:
                with open(path, "r") as f:
                    record = json.load(f)
            except Exception:
                continue

            if record.get("tracking_status") != "active":
                continue

            ticker = record["ticker"]
            trade_date = record["trade_date"]

            # D+5 지나면 완료 처리
            try:
                td = datetime.strptime(trade_date, "%Y-%m-%d")
            except ValueError:
                continue

            days_elapsed = (datetime.utcnow() - td).days
            if days_elapsed > 7:  # 주말 포함 여유
                record["tracking_status"] = "completed"
                self._analyze_cause(record)
                self._save(path, record)
                logger.info(f"✅ {filename} 추적 완료")
                continue

            # 일봉 수집
            self._update_daily_bars(record, ticker, trade_date)

            # 뉴스 수집
            self._update_news(record, ticker)

            record["last_updated"] = datetime.utcnow().isoformat()
            self._save(path, record)
            logger.info(f"📊 {filename} 업데이트 완료 (D+{days_elapsed})")

    def _update_daily_bars(self, record: dict, ticker: str, trade_date: str):
        """D+0~D+5 일봉 수집"""
        try:
            td = datetime.strptime(trade_date, "%Y-%m-%d")
            end = td + timedelta(days=7)
            url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{trade_date}/{end.strftime('%Y-%m-%d')}"
            resp = requests.get(url, params={"apiKey": POLYGON_API_KEY, "limit": 10}, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])

            bars = {}
            for bar in results:
                bar_date = datetime.utcfromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
                day_offset = (datetime.strptime(bar_date, "%Y-%m-%d") - td).days
                bars[f"D+{day_offset}"] = {
                    "date": bar_date,
                    "open": bar.get("o"),
                    "high": bar.get("h"),
                    "low": bar.get("l"),
                    "close": bar.get("c"),
                    "volume": bar.get("v"),
                }
            record["daily_bars"] = bars
        except Exception as e:
            logger.error(f"{ticker} 일봉 수집 실패: {e}")

    def _update_news(self, record: dict, ticker: str):
        """관련 뉴스 수집"""
        try:
            url = "https://api.polygon.io/v2/reference/news"
            resp = requests.get(url, params={
                "ticker": ticker,
                "limit": 10,
                "apiKey": POLYGON_API_KEY,
            }, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])

            news = []
            for article in results:
                news.append({
                    "title": article.get("title", ""),
                    "url": article.get("article_url", ""),
                    "published": article.get("published_utc", ""),
                    "source": article.get("publisher", {}).get("name", ""),
                })
            record["news"] = news
        except Exception as e:
            logger.error(f"{ticker} 뉴스 수집 실패: {e}")

    def _fetch_ticker_details(self, ticker: str) -> Optional[dict]:
        """종목 상세정보 (섹터/산업)"""
        try:
            url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
            resp = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=10)
            resp.raise_for_status()
            result = resp.json().get("results", {})
            return {
                "sector": result.get("sic_description", ""),
                "industry": result.get("type", ""),
                "name": result.get("name", ""),
                "market_cap": result.get("market_cap"),
            }
        except Exception as e:
            logger.error(f"{ticker} 상세정보 조회 실패: {e}")
            return None

    def _analyze_cause(self, record: dict):
        """급등 원인 분류"""
        news_titles = " ".join([n.get("title", "").lower() for n in record.get("news", [])])
        tags = []
        cause = "unknown"

        if any(kw in news_titles for kw in ["fda", "approval", "drug", "clinical", "trial"]):
            cause = "FDA"
            tags.append("biotech")
        elif any(kw in news_titles for kw in ["earnings", "revenue", "profit", "quarterly", "beat"]):
            cause = "earnings"
            tags.append("fundamental")
        elif any(kw in news_titles for kw in ["short squeeze", "short interest", "heavily shorted"]):
            cause = "short_squeeze"
            tags.append("squeeze")
        elif any(kw in news_titles for kw in ["reddit", "wallstreetbets", "meme", "viral", "social media"]):
            cause = "meme"
            tags.append("social")
        elif any(kw in news_titles for kw in ["contract", "partnership", "deal", "acquisition", "merger"]):
            cause = "catalyst"
            tags.append("corporate_action")

        record["analysis"] = {"cause": cause, "tags": tags}

    def _save(self, path: str, record: dict):
        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)
