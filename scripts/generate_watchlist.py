"""
Polygon API로 소형주 워치리스트 생성
- reference tickers → US CS 목록
- snapshot prevDay.c로 가격 필터 ($0.5~$20)
- 목표: 500~800개
"""
import os
import sys
import json
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY = os.getenv("POLYGON_API_KEY", "")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")


def fetch_all_tickers():
    all_tickers = []
    url = "https://api.polygon.io/v3/reference/tickers"
    params = {"market": "stocks", "active": "true", "type": "CS", "limit": 1000, "sort": "ticker", "order": "asc", "apiKey": API_KEY}
    page = 0
    while url:
        page += 1
        print(f"  페이지 {page}... ({len(all_tickers)}개)")
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_tickers.extend(data.get("results", []))
        next_url = data.get("next_url")
        if next_url:
            url = next_url
            params = {"apiKey": API_KEY}
        else:
            break
        time.sleep(0.15)
    return all_tickers


def fetch_snapshot_prices():
    """Snapshot에서 prevDay.c (전일종가) 사용"""
    print("📊 Snapshot 조회...")
    resp = requests.get(
        "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
        params={"apiKey": API_KEY}, timeout=30,
    )
    resp.raise_for_status()
    price_map = {}
    for t in resp.json().get("tickers", []):
        ticker = t.get("ticker", "")
        prev = t.get("prevDay", {})
        day = t.get("day", {})
        # 현재가 or 전일종가
        price = day.get("c", 0) or prev.get("c", 0) or 0
        prev_vol = prev.get("v", 0) or 0
        if price > 0:
            price_map[ticker] = {"price": price, "prev_vol": prev_vol}
    print(f"  {len(price_map)}개 종목")
    return price_map


def main():
    print("🔍 전체 보통주(CS) 조회...")
    all_tickers = fetch_all_tickers()
    valid_exchanges = {"XNAS", "XNYS", "XASE"}
    us_map = {t["ticker"]: t for t in all_tickers if t.get("primary_exchange") in valid_exchanges}
    print(f"  미국 CS: {len(us_map)}개")

    prices = fetch_snapshot_prices()

    watchlist = []
    for ticker, info in us_map.items():
        p = prices.get(ticker)
        if not p:
            continue
        price = p["price"]
        if not (0.5 <= price <= 20.0):
            continue
        # 극단적 저유동성 제외 (전일 거래량 1000주 미만)
        if p["prev_vol"] < 1000:
            continue
        watchlist.append({
            "ticker": ticker,
            "name": info.get("name", ""),
            "exchange": info.get("primary_exchange", ""),
            "price": round(price, 2),
            "market_cap": 0,
        })

    print(f"✅ 워치리스트: {len(watchlist)}개")

    if len(watchlist) > 1000:
        watchlist = [w for w in watchlist if 1.0 <= w["price"] <= 15.0]
        print(f"  → 축소: {len(watchlist)}개 ($1~$15)")

    watchlist.sort(key=lambda x: x["ticker"])
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(watchlist, f, indent=2)
    print(f"💾 {OUTPUT_PATH} ({len(watchlist)}개)")

    by_ex = {}
    for w in watchlist:
        by_ex[w["exchange"]] = by_ex.get(w["exchange"], 0) + 1
    for ex, cnt in sorted(by_ex.items()):
        print(f"  {ex}: {cnt}개")


if __name__ == "__main__":
    main()
