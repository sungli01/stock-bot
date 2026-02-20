#!/usr/bin/env python3
"""
백테스트 데이터 수집기 (에이전트 1)
bars_cache 1m 파일 → 날짜별 시뮬레이션 이벤트 스트림 생성

출력: backtest_sim/processed/YYYY-MM-DD.json
포맷:
{
  "date": "2026-01-15",
  "tickers": {
    "ABCD": {
      "bars_1m": [...],         # 원본 1m 봉
      "bars_3m": [...],         # 3분봉 (3개 1m봉 합산)
      "daily_volume": 1234567,  # 해당일 총 거래량
      "daily_open": 2.50,       # 시가
      "daily_high": 4.80,       # 일중 최고가
      "daily_low": 2.30,        # 일중 최저가
      "daily_close": 3.90,      # 종가
      "daily_change_pct": 56.0, # 일일 변화율 %
      "events": [               # 볼륨스파이크/가격급등 이벤트 리스트
        {
          "time_ms": 1737...,
          "bar_idx": 42,
          "bar_open": 2.5,
          "bar_close": 3.1,
          "price_change_pct_from_open": 24.0,  # 시가 대비 현재가
          "vol_3min_ratio_pct": 350.0,          # 직전 3분봉 대비 현재 3분봉 거래량 비율
          "vol_3min_abs": 45000,
          "vol_3min_prev": 12000,
          "is_vol_spike": True,                 # vol_3min_ratio_pct >= 200
          "is_candidate": True,                 # price 변화 >= 1% (수정된 임계값)
          "is_premarket": True,                 # 미국 기준 프리마켓 여부
        }
      ]
    }
  }
}
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 경로 설정
BARS_CACHE_DIR = Path("/home/ubuntu/.openclaw/workspace/stock-bot/data/bars_cache")
OUTPUT_DIR = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/processed")
READY_FLAG = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/READY.flag")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 알고리즘 파라미터 (v8.5 기준 + 버그수정 반영)
CONFIG = {
    "min_price": 0.70,
    "max_price": 30.0,
    "candidate_change_pct": 1.0,       # 버그#6 수정: 5% → 1%
    "vol_3min_ratio_pct": 200.0,       # 3분봉 거래량 200%+ 증가
    "min_daily_volume": 300000,        # $10 미만 종목
    "min_daily_volume_highprice": 50000,  # $10 이상 종목
    "highprice_threshold": 10.0,
    "price_change_pct": 20.0,          # 큐 기준 20%+ 상승 시 매수
    "max_pct_from_queue": 40.0,        # 버그#3 수정: 큐 대비 최대 40%까지만 허용
}

# 미국 동부시간 기준 (UTC 오프셋)
# 프리마켓: UTC 14:00 ~ UTC 18:30 (EST: 09:30 이전)
# 정규장: UTC 18:30 ~ UTC 01:00 (다음날)
# 애프터마켓: UTC 01:00 ~ UTC 04:00
PREMARKET_START_UTC_MS = 9 * 3600 * 1000   # UTC 09:00 (EST 04:00)
REGULAR_START_UTC_MS = 18 * 3600 * 1000 + 30 * 60 * 1000  # UTC 18:30
MARKET_CLOSE_UTC_MS = (24 + 1) * 3600 * 1000  # UTC 01:00 다음날


def get_all_dates():
    """bars_cache에서 모든 날짜 추출"""
    dates = set()
    for f in BARS_CACHE_DIR.glob("*_1m.json"):
        parts = f.stem.split("_")
        # TICKER_YYYY-MM-DD_1m 형식 — 날짜는 항상 마지막에서 두 번째 파트
        date_str = parts[-2]  # YYYY-MM-DD
        if len(date_str) == 10:
            dates.add(date_str)
    return sorted(dates)


def get_tickers_for_date(date_str):
    """특정 날짜의 모든 ticker 목록"""
    tickers = []
    for f in BARS_CACHE_DIR.glob(f"*_{date_str}_1m.json"):
        ticker = f.stem.replace(f"_{date_str}_1m", "")
        tickers.append(ticker)
    return sorted(tickers)


def compute_3min_bars(bars_1m):
    """1분봉 → 3분봉 집계"""
    bars_3m = []
    for i in range(2, len(bars_1m)):
        window = bars_1m[i-2:i+1]  # 3개 봉 윈도우
        vol = sum(b.get("v", 0) for b in window)
        open_p = window[0].get("o", window[0].get("vw", 0))
        close_p = window[-1].get("c", window[-1].get("vw", 0))
        high_p = max(b.get("h", b.get("vw", 0)) for b in window)
        low_p = min(b.get("l", b.get("vw", float("inf"))) for b in window)
        bars_3m.append({
            "t": window[-1]["t"],    # 마지막 봉의 타임스탬프
            "o": open_p,
            "c": close_p,
            "h": high_p,
            "l": low_p,
            "v": vol,
        })
    return bars_3m


def compute_events(bars_1m, bars_3m):
    """볼륨스파이크 / 가격 이벤트 추출"""
    if not bars_1m:
        return []

    daily_open = bars_1m[0].get("o", bars_1m[0].get("vw", 0))
    events = []

    for idx, bar3 in enumerate(bars_3m):
        if idx < 1:
            continue  # 직전 3분봉 필요

        prev_bar3 = bars_3m[idx - 1]
        cur_vol = bar3.get("v", 0)
        prev_vol = prev_bar3.get("v", 0)

        if prev_vol <= 0:
            continue

        vol_ratio_pct = (cur_vol / prev_vol - 1) * 100
        cur_price = bar3.get("c", bar3.get("vw", 0))
        price_change_from_open = ((cur_price / daily_open) - 1) * 100 if daily_open > 0 else 0

        # 타임스탬프 기준 시장 구분
        t_ms = bar3["t"]
        t_sec_of_day_utc = (t_ms // 1000) % 86400
        t_utc_ms_of_day = t_sec_of_day_utc * 1000
        is_premarket = t_utc_ms_of_day < REGULAR_START_UTC_MS
        is_afterhours = t_utc_ms_of_day >= MARKET_CLOSE_UTC_MS

        is_vol_spike = vol_ratio_pct >= CONFIG["vol_3min_ratio_pct"]
        is_candidate = abs(price_change_from_open) >= CONFIG["candidate_change_pct"]

        if is_vol_spike or is_candidate:
            events.append({
                "time_ms": t_ms,
                "bar3_idx": idx,
                "bar_open": bar3.get("o", 0),
                "bar_close": cur_price,
                "bar_vol": cur_vol,
                "price_change_pct_from_open": round(price_change_from_open, 2),
                "vol_3min_ratio_pct": round(vol_ratio_pct, 1),
                "vol_3min_abs": cur_vol,
                "vol_3min_prev": prev_vol,
                "is_vol_spike": is_vol_spike,
                "is_candidate": is_candidate,
                "is_premarket": is_premarket,
                "is_afterhours": is_afterhours,
            })

    return events


def process_ticker(ticker, date_str):
    """단일 ticker-date 처리"""
    fpath = BARS_CACHE_DIR / f"{ticker}_{date_str}_1m.json"
    if not fpath.exists():
        return None

    with open(fpath) as f:
        bars_1m = json.load(f)

    if not bars_1m or len(bars_1m) < 3:
        return None

    # 가격 필터
    prices = [b.get("c", b.get("vw", 0)) for b in bars_1m if b.get("c", b.get("vw", 0)) > 0]
    if not prices:
        return None
    avg_price = sum(prices) / len(prices)
    if avg_price < CONFIG["min_price"] or avg_price > CONFIG["max_price"]:
        return None

    # 워런트 필터 (버그#5 수정)
    if ticker.endswith(".WS") or ticker.endswith("-WS"):
        return None

    daily_open = bars_1m[0].get("o", bars_1m[0].get("vw", 0))
    daily_close = bars_1m[-1].get("c", bars_1m[-1].get("vw", 0))
    daily_high = max(b.get("h", b.get("vw", 0)) for b in bars_1m)
    daily_low = min(b.get("l", b.get("vw", float("inf"))) for b in bars_1m)
    daily_volume = sum(b.get("v", 0) for b in bars_1m)
    daily_change_pct = ((daily_close / daily_open) - 1) * 100 if daily_open > 0 else 0

    bars_3m = compute_3min_bars(bars_1m)
    events = compute_events(bars_1m, bars_3m)

    return {
        "ticker": ticker,
        "date": date_str,
        "daily_volume": daily_volume,
        "daily_open": round(daily_open, 4),
        "daily_high": round(daily_high, 4),
        "daily_low": round(daily_low, 4),
        "daily_close": round(daily_close, 4),
        "daily_change_pct": round(daily_change_pct, 2),
        "bars_1m_count": len(bars_1m),
        "bars_3m": bars_3m,
        "bars_1m": bars_1m,
        "events": events,
    }


def process_date(date_str):
    """날짜 전체 처리"""
    tickers = get_tickers_for_date(date_str)
    result = {
        "date": date_str,
        "tickers": {}
    }

    for ticker in tickers:
        data = process_ticker(ticker, date_str)
        if data:
            result["tickers"][ticker] = data

    return result


def main():
    dates = get_all_dates()
    print(f"[DataCollector] 총 {len(dates)}거래일 처리 시작")

    for i, date_str in enumerate(dates):
        out_path = OUTPUT_DIR / f"{date_str}.json"
        if out_path.exists():
            print(f"  [{i+1}/{len(dates)}] {date_str} — 스킵 (이미 존재)")
            continue

        data = process_date(date_str)
        ticker_count = len(data["tickers"])

        with open(out_path, "w") as f:
            json.dump(data, f)

        # 이벤트 통계
        total_events = sum(len(v["events"]) for v in data["tickers"].values())
        vol_spikes = sum(
            sum(1 for e in v["events"] if e["is_vol_spike"])
            for v in data["tickers"].values()
        )
        print(f"  [{i+1}/{len(dates)}] {date_str} — {ticker_count}종목, {total_events}이벤트, {vol_spikes}볼스파이크")

    # 완료 플래그
    with open(READY_FLAG, "w") as f:
        f.write(f"READY\n{len(dates)} dates processed\n{datetime.now().isoformat()}\n")

    print(f"\n[DataCollector] 완료. READY 플래그 생성: {READY_FLAG}")


if __name__ == "__main__":
    main()
