#!/usr/bin/env python3
"""
sim/feeder.py — 하루치 데이터 피더 (서브에이전트 1)

사용법: python3 sim/feeder.py 2025-11-19
출력: sim/stream/YYYY-MM-DD.json  (시간순 1분봉 스트림)

스트림 포맷:
[
  {
    "time_ms": 1737...,
    "time_kst": "2025-11-20 00:31",   ← 한국시간
    "ticker": "ABCD",
    "o": 2.50, "h": 2.60, "l": 2.45, "c": 2.55, "v": 12000,
    "bar_idx": 5,                      ← 해당 종목의 몇 번째 봉
    "daily_open": 2.10,               ← 해당 종목 시가
    "daily_volume_so_far": 45000,     ← 현재까지 누적 거래량
  },
  ...
]
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

BARS_CACHE = Path(__file__).parent.parent / "data" / "bars_cache"
STREAM_DIR = Path(__file__).parent / "stream"
STREAM_DIR.mkdir(exist_ok=True)


def get_kst(ts_ms: int) -> str:
    """UTC ms → KST 문자열"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) + timedelta(hours=9)
    return dt.strftime("%Y-%m-%d %H:%M")


def feed_day(date_str: str) -> dict:
    """특정 날짜의 모든 종목 1m 봉 → 시간순 스트림 생성"""
    stream_path = STREAM_DIR / f"{date_str}.json"

    # 이미 생성됐으면 스킵
    if stream_path.exists():
        print(f"[Feeder] {date_str} 스트림 이미 존재 — 재사용")
        with open(stream_path) as f:
            data = json.load(f)
        return {"date": date_str, "bar_count": len(data["stream"]), "tickers": data["tickers"]}

    # 해당 날짜 1m 파일 목록
    files = list(BARS_CACHE.glob(f"*_{date_str}_1m.json"))
    if not files:
        print(f"[Feeder] {date_str} 데이터 없음")
        return {}

    tickers_data = {}

    # 각 종목 로드
    for f in files:
        ticker = f.stem.replace(f"_{date_str}_1m", "")

        # 워런트 제외
        if ticker.endswith(".WS") or ticker.endswith("-WS"):
            continue

        with open(f) as fp:
            bars = json.load(fp)

        if not bars or len(bars) < 5:
            continue

        daily_open = bars[0].get("o", bars[0].get("vw", 0))
        if daily_open <= 0:
            continue

        # 가격 범위 필터 (시가 기준)
        if daily_open < 0.70 or daily_open > 30.0:
            continue

        tickers_data[ticker] = {
            "bars": bars,
            "daily_open": daily_open,
            "ticker": ticker,
        }

    if not tickers_data:
        print(f"[Feeder] {date_str} 유효 종목 없음")
        return {}

    # 시간순 스트림 생성
    all_events = []
    for ticker, tdata in tickers_data.items():
        bars = tdata["bars"]
        daily_open = tdata["daily_open"]
        cumulative_vol = 0

        for idx, bar in enumerate(bars):
            ts = bar.get("t", 0)
            if ts == 0:
                continue
            v = bar.get("v", 0)
            cumulative_vol += v

            all_events.append({
                "time_ms": ts,
                "time_kst": get_kst(ts),
                "ticker": ticker,
                "o": bar.get("o", bar.get("vw", 0)),
                "h": bar.get("h", bar.get("vw", 0)),
                "l": bar.get("l", bar.get("vw", 0)),
                "c": bar.get("c", bar.get("vw", 0)),
                "v": v,
                "bar_idx": idx,
                "daily_open": daily_open,
                "daily_volume_so_far": cumulative_vol,
            })

    # 시간순 정렬
    all_events.sort(key=lambda x: (x["time_ms"], x["ticker"]))

    output = {
        "date": date_str,
        "tickers": list(tickers_data.keys()),
        "ticker_count": len(tickers_data),
        "bar_count": len(all_events),
        "stream": all_events,
    }

    with open(stream_path, "w") as f:
        json.dump(output, f)

    print(f"[Feeder] {date_str} 완료 — {len(tickers_data)}종목, {len(all_events)}봉")
    return {"date": date_str, "bar_count": len(all_events), "tickers": list(tickers_data.keys())}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 sim/feeder.py YYYY-MM-DD")
        sys.exit(1)
    date_str = sys.argv[1]
    result = feed_day(date_str)
    print(json.dumps(result, ensure_ascii=False))
