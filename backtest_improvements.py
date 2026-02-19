#!/usr/bin/env python3
"""백테스트 진입 전략 개선 3가지 비교"""

import requests
import time
import json
from datetime import datetime, timedelta
from collections import defaultdict

API_KEY = "e5MIxst1E1Gdgbecg2fLSJsxw0AFJHCo"
DATES = ["2026-02-11", "2026-02-12", "2026-02-13", "2026-02-17", "2026-02-18"]
BASE = "https://api.polygon.io"
INITIAL_CAPITAL = 280.0
MAX_CONCURRENT = 2

def api_get(url, params=None):
    if params is None: params = {}
    params["apiKey"] = API_KEY
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                print(f"  Rate limited, waiting 15s...")
                time.sleep(15)
                continue
            if r.status_code == 200:
                return r.json()
            else:
                print(f"  HTTP {r.status_code} for {url}")
                return None
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(2)
    return None

def get_candidates(date):
    """grouped bars에서 후보 종목 추출"""
    print(f"\n[{date}] 후보 종목 추출...")
    data = api_get(f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date}")
    if not data or "results" not in data:
        print(f"  No grouped data for {date}")
        return []
    
    candidates = []
    for bar in data["results"]:
        ticker = bar.get("T", "")
        o, h, l, c, v = bar.get("o",0), bar.get("h",0), bar.get("l",0), bar.get("c",0), bar.get("v",0)
        if o <= 0 or v < 500000: continue
        if c < 0.7 or c > 10: continue
        if (h - o) / o < 0.10: continue  # 고가/시가 > +10%
        # Skip weird tickers
        if len(ticker) > 5 or "." in ticker or "/" in ticker: continue
        gain = (h - o) / o
        candidates.append((ticker, gain, v, o, c))
    
    candidates.sort(key=lambda x: -x[1])
    candidates = candidates[:15]
    print(f"  {len(candidates)} 후보: {[c[0] for c in candidates]}")
    return candidates

def get_minute_bars(ticker, date):
    """1분봉 데이터 수집"""
    data = api_get(f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}", 
                   {"limit": 50000, "sort": "asc"})
    if not data or "results" not in data:
        return []
    return data["results"]

def build_5min_bars(minute_bars):
    """1분봉 → 5분봉 변환"""
    if not minute_bars: return []
    bars_5m = []
    buf = []
    for bar in minute_bars:
        ts = bar["t"]
        # 5분 단위로 그룹
        bucket = (ts // 300000) * 300000
        if buf and (buf[0]["t"] // 300000) * 300000 != bucket:
            # flush
            bars_5m.append({
                "t": (buf[0]["t"] // 300000) * 300000,
                "o": buf[0]["o"],
                "h": max(b["h"] for b in buf),
                "l": min(b["l"] for b in buf),
                "c": buf[-1]["c"],
                "v": sum(b["v"] for b in buf),
            })
            buf = []
        buf.append(bar)
    if buf:
        bars_5m.append({
            "t": (buf[0]["t"] // 300000) * 300000,
            "o": buf[0]["o"],
            "h": max(b["h"] for b in buf),
            "l": min(b["l"] for b in buf),
            "c": buf[-1]["c"],
            "v": sum(b["v"] for b in buf),
        })
    return bars_5m

def calc_rsi(closes, period=14):
    """RSI 계산, returns list same length as closes (None for first period entries)"""
    if len(closes) < period + 1:
        return [None] * len(closes)
    
    rsi_values = [None] * period
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100 - 100/(1+rs))
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100 - 100/(1+rs))
    
    return rsi_values

def simulate_exit(minute_bars, entry_idx, entry_price):
    """매도 전략 v7 시뮬레이션"""
    stop_loss = -0.07
    trail_activated = False
    peak_gain = 0.0
    entry_time = minute_bars[entry_idx]["t"]
    
    for i in range(entry_idx + 1, len(minute_bars)):
        bar = minute_bars[i]
        current_price = bar["c"]
        high_price = bar["h"]
        low_price = bar["l"]
        elapsed_min = (bar["t"] - entry_time) / 60000
        
        gain_from_entry = (current_price - entry_price) / entry_price
        low_gain = (low_price - entry_price) / entry_price
        high_gain = (high_price - entry_price) / entry_price
        
        # Update peak
        if high_gain > peak_gain:
            peak_gain = high_gain
        
        # 손절 체크 (low 기준)
        if low_gain <= stop_loss:
            return stop_loss, elapsed_min
        
        # 시간가중
        time_mult = 0.8 if elapsed_min >= 30 else 1.0
        
        # 45분 최대 보유
        if elapsed_min >= 45:
            return gain_from_entry, elapsed_min
        
        # 트레일링 체크
        if peak_gain >= 0.08:
            trail_activated = True
        
        if trail_activated:
            # 동적 트레일링
            if peak_gain >= 0.80:
                trail_pct = 0.30
            elif peak_gain >= 0.50:
                trail_pct = 0.08
            elif peak_gain >= 0.15:
                trail_pct = 0.05
            else:
                trail_pct = 0.03
            
            trail_pct *= time_mult
            
            drawdown = peak_gain - gain_from_entry
            if drawdown >= trail_pct:
                return gain_from_entry, elapsed_min
    
    # End of data
    if len(minute_bars) > entry_idx:
        final = (minute_bars[-1]["c"] - entry_price) / entry_price
        elapsed = (minute_bars[-1]["t"] - entry_time) / 60000
        return final, elapsed
    return 0, 0

def find_signals_baseline(bars_5m, minute_bars, vol_spike=2.0, daily_vol=0, rsi_limit=None):
    """기존 전략: 5분봉 +5% 급등 + 거래량 스파이크"""
    return find_signals(bars_5m, minute_bars, threshold=0.05, vol_spike=vol_spike, 
                       daily_vol=daily_vol, rsi_limit=rsi_limit)

def find_signals(bars_5m, minute_bars, threshold=0.05, vol_spike=2.0, daily_vol=0, rsi_limit=None):
    """시그널 탐지"""
    if len(bars_5m) < 3: return []
    
    # RSI 계산 (1분봉 기반)
    minute_closes = [b["c"] for b in minute_bars]
    rsi_values = calc_rsi(minute_closes) if rsi_limit else None
    
    # 분봉 타임스탬프 → 인덱스 매핑
    minute_ts_map = {}
    for idx, b in enumerate(minute_bars):
        minute_ts_map[b["t"]] = idx
    
    # 평균 거래량 (20봉 이동평균)
    signals = []
    for i in range(2, len(bars_5m)):
        bar = bars_5m[i]
        change = (bar["c"] - bar["o"]) / bar["o"] if bar["o"] > 0 else 0
        
        if change < threshold:
            continue
        
        # 거래량 스파이크 체크
        lookback = bars_5m[max(0, i-20):i]
        if len(lookback) < 3: continue
        avg_vol = sum(b["v"] for b in lookback) / len(lookback)
        if avg_vol <= 0: continue
        if bar["v"] / avg_vol < vol_spike: continue
        
        # RSI 필터
        if rsi_limit and rsi_values:
            # 5분봉 끝 시점의 1분봉 RSI
            bar_end_ts = bar["t"] + 300000  # 5분봉 끝
            # 가장 가까운 1분봉 찾기
            best_idx = None
            for ts_offset in range(0, 300000, 60000):
                check_ts = bar["t"] + ts_offset
                if check_ts in minute_ts_map:
                    best_idx = minute_ts_map[check_ts]
            if best_idx and best_idx < len(rsi_values) and rsi_values[best_idx] is not None:
                if rsi_values[best_idx] > rsi_limit:
                    continue  # 과매수 → 스킵
        
        # 진입 시점: 5분봉 완성 직후의 1분봉
        entry_ts = bar["t"] + 300000
        entry_idx = None
        for j in range(len(minute_bars)):
            if minute_bars[j]["t"] >= entry_ts:
                entry_idx = j
                break
        
        if entry_idx is None: continue
        entry_price = minute_bars[entry_idx]["o"]
        if entry_price <= 0: continue
        
        signals.append({
            "entry_idx": entry_idx,
            "entry_price": entry_price,
            "entry_time": minute_bars[entry_idx]["t"],
            "signal_bar": bar,
        })
    
    return signals

def run_simulation(all_signals_by_date, initial_capital=280.0, max_concurrent=2):
    """포트폴리오 시뮬레이션"""
    trades = []
    for date, ticker_signals in all_signals_by_date.items():
        # Flatten and sort by time
        all_sigs = []
        for ticker, minute_bars, sigs in ticker_signals:
            for sig in sigs:
                all_sigs.append((ticker, minute_bars, sig))
        all_sigs.sort(key=lambda x: x[2]["entry_time"])
        
        active = []  # (exit_time,)
        for ticker, minute_bars, sig in all_sigs:
            entry_time = sig["entry_time"]
            # Remove finished trades
            active = [a for a in active if a > entry_time]
            if len(active) >= max_concurrent:
                continue
            
            ret, elapsed = simulate_exit(minute_bars, sig["entry_idx"], sig["entry_price"])
            exit_time = entry_time + elapsed * 60000
            active.append(exit_time)
            
            trades.append({
                "date": date,
                "ticker": ticker,
                "entry_price": sig["entry_price"],
                "return": ret,
                "elapsed_min": elapsed,
            })
    
    return trades

def calc_stats(trades):
    if not trades:
        return {"count": 0, "win_rate": 0, "avg_return": 0, "profit_factor": 0, "total_return": 0}
    
    wins = [t for t in trades if t["return"] > 0]
    losses = [t for t in trades if t["return"] <= 0]
    
    total_gain = sum(t["return"] for t in wins) if wins else 0
    total_loss = abs(sum(t["return"] for t in losses)) if losses else 0.001
    
    avg_ret = sum(t["return"] for t in trades) / len(trades)
    
    # Compounding
    capital = INITIAL_CAPITAL
    for t in trades:
        pos_size = capital / MAX_CONCURRENT
        capital += pos_size * t["return"]
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL
    
    return {
        "count": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_return": avg_ret * 100,
        "profit_factor": total_gain / total_loss if total_loss > 0 else 999,
        "total_return": total_return * 100,
        "final_capital": capital,
    }

# ========== MAIN ==========
print("=" * 60)
print("진입 전략 개선 백테스트 시작")
print("=" * 60)

# Step 1: 각 날짜별 후보 종목 + 1분봉 수집
all_data = {}  # date -> [(ticker, daily_vol, minute_bars, bars_5m)]

for date in DATES:
    candidates = get_candidates(date)
    time.sleep(0.5)
    
    date_data = []
    for ticker, gain, vol, open_p, close_p in candidates:
        print(f"  [{date}] {ticker} 1분봉 수집...")
        minute_bars = get_minute_bars(ticker, date)
        time.sleep(0.25)
        if len(minute_bars) < 30:
            print(f"    → {len(minute_bars)}개 봉, 스킵")
            continue
        bars_5m = build_5min_bars(minute_bars)
        date_data.append((ticker, vol, minute_bars, bars_5m))
        print(f"    → {len(minute_bars)}개 1분봉, {len(bars_5m)}개 5분봉")
    
    all_data[date] = date_data

print("\n" + "=" * 60)
print("데이터 수집 완료. 전략별 시뮬레이션 시작...")
print("=" * 60)

# Define strategies
strategies = {
    "Baseline (+5%, no filter)": {"threshold": 0.05, "rsi_limit": None, "daily_vol": 0},
    "A1: +2% 진입": {"threshold": 0.02, "rsi_limit": None, "daily_vol": 0},
    "A2: +3% 진입": {"threshold": 0.03, "rsi_limit": None, "daily_vol": 0},
    "B1: RSI<70 필터": {"threshold": 0.05, "rsi_limit": 70, "daily_vol": 0},
    "B2: RSI<80 필터": {"threshold": 0.05, "rsi_limit": 80, "daily_vol": 0},
    "C1: 일거래량>1M": {"threshold": 0.05, "rsi_limit": None, "daily_vol": 1_000_000},
    "C2: 일거래량>2M": {"threshold": 0.05, "rsi_limit": None, "daily_vol": 2_000_000},
    "A2+B2: +3%,RSI<80": {"threshold": 0.03, "rsi_limit": 80, "daily_vol": 0},
    "A2+B1: +3%,RSI<70": {"threshold": 0.03, "rsi_limit": 70, "daily_vol": 0},
    "A2+B2+C1: +3%,RSI<80,Vol>1M": {"threshold": 0.03, "rsi_limit": 80, "daily_vol": 1_000_000},
    "A1+B2+C1: +2%,RSI<80,Vol>1M": {"threshold": 0.02, "rsi_limit": 80, "daily_vol": 1_000_000},
    "A2+B1+C1: +3%,RSI<70,Vol>1M": {"threshold": 0.03, "rsi_limit": 70, "daily_vol": 1_000_000},
}

results = {}

for name, params in strategies.items():
    print(f"\n전략: {name}")
    all_signals = {}
    
    for date, date_data in all_data.items():
        ticker_signals = []
        for ticker, daily_vol, minute_bars, bars_5m in date_data:
            # 일거래량 필터
            if params["daily_vol"] > 0 and daily_vol < params["daily_vol"]:
                continue
            
            sigs = find_signals(bars_5m, minute_bars, 
                              threshold=params["threshold"],
                              vol_spike=2.0,
                              rsi_limit=params["rsi_limit"])
            if sigs:
                ticker_signals.append((ticker, minute_bars, sigs))
        all_signals[date] = ticker_signals
    
    trades = run_simulation(all_signals)
    stats = calc_stats(trades)
    results[name] = stats
    print(f"  거래수={stats['count']}, 승률={stats['win_rate']:.1f}%, "
          f"평균수익={stats['avg_return']:.2f}%, 손익비={stats['profit_factor']:.2f}, "
          f"총수익={stats['total_return']:.2f}%")

# ========== 결과 파일 생성 ==========
print("\n\n결과 파일 생성 중...")

baseline = results.get("Baseline (+5%, no filter)", {})

md = []
md.append("# 진입 전략 개선 백테스트 결과")
md.append("")
md.append(f"**테스트 기간:** {', '.join(DATES)}")
md.append(f"**초기 자본:** ${INITIAL_CAPITAL}")
md.append(f"**동시 보유:** {MAX_CONCURRENT}종목")
md.append(f"**매도 전략:** v7 (-7% 손절, +8% 트레일링, 동적 트레일링, 45분 최대보유)")
md.append("")

md.append("## 전략별 비교 테이블")
md.append("")
md.append("| 전략 | 거래수 | 승률(%) | 평균수익(%) | 손익비 | 총수익(%) | 최종자본($) |")
md.append("|------|--------|---------|-------------|--------|-----------|-------------|")

for name, stats in results.items():
    md.append(f"| {name} | {stats['count']} | {stats['win_rate']:.1f} | "
              f"{stats['avg_return']:.2f} | {stats['profit_factor']:.2f} | "
              f"{stats['total_return']:.2f} | {stats.get('final_capital', INITIAL_CAPITAL):.2f} |")

md.append("")
md.append("## Baseline 대비 개선폭")
md.append("")

if baseline and baseline["count"] > 0:
    md.append("| 전략 | 승률 변화 | 평균수익 변화 | 총수익 변화 |")
    md.append("|------|-----------|---------------|-------------|")
    for name, stats in results.items():
        if name == "Baseline (+5%, no filter)": continue
        wr_diff = stats["win_rate"] - baseline["win_rate"]
        ar_diff = stats["avg_return"] - baseline["avg_return"]
        tr_diff = stats["total_return"] - baseline["total_return"]
        md.append(f"| {name} | {wr_diff:+.1f}%p | {ar_diff:+.2f}%p | {tr_diff:+.2f}%p |")
else:
    md.append("Baseline 거래 없음 - 비교 불가")

md.append("")
md.append("## 분석 및 추천")
md.append("")

# Find best strategy
best_name = max(results.keys(), key=lambda k: results[k]["total_return"]) if results else "N/A"
best = results.get(best_name, {})

md.append(f"### 최적 전략: **{best_name}**")
md.append(f"- 총수익률: {best.get('total_return', 0):.2f}%")
md.append(f"- 승률: {best.get('win_rate', 0):.1f}%")
md.append(f"- 손익비: {best.get('profit_factor', 0):.2f}")
md.append(f"- 거래수: {best.get('count', 0)}")
md.append("")

# Sort by total return
sorted_strats = sorted(results.items(), key=lambda x: -x[1]["total_return"])
md.append("### 전략 순위 (총수익 기준)")
md.append("")
for i, (name, stats) in enumerate(sorted_strats, 1):
    emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
    md.append(f"{emoji} **{name}**: {stats['total_return']:.2f}% (승률 {stats['win_rate']:.1f}%, {stats['count']}거래)")

md.append("")
md.append("### 개선안 요약")
md.append("")
md.append("- **A (조기 진입):** 급등 꼭대기 대신 초기에 진입하여 더 좋은 가격 확보")
md.append("- **B (RSI 필터):** 이미 과열된 종목 진입 방지")
md.append("- **C (거래량 필터):** 유동성 높은 종목만 선별하여 슬리피지 감소")
md.append("")
md.append(f"*생성일: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")

output = "\n".join(md)
with open("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_improvements.md", "w") as f:
    f.write(output)

print("\n" + "=" * 60)
print("완료! 결과 저장: stock-bot/backtest_improvements.md")
print("=" * 60)
print("\n" + output)
