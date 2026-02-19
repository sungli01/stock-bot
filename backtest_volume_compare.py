#!/usr/bin/env python3
"""거래량 스파이크 기준별 백테스트 비교 시뮬레이션"""

import requests
import time
import json
from datetime import datetime, timedelta
from collections import defaultdict

API_KEY = "e5MIxst1E1Gdgbecg2fLSJsxw0AFJHCo"
BASE = "https://api.polygon.io"
DATES = ["2025-02-12","2025-02-13","2025-02-14","2025-02-18"]  # 5 trading days (no 2/17 Presidents Day)
# Actually let me include 2/11 to get 5 days if 2/17 is holiday
DATES = ["2026-02-11","2026-02-12","2026-02-13","2026-02-16","2026-02-17","2026-02-18"]

VOLUME_THRESHOLDS = [200, 300, 400, 500, 700, 1000]
STOP_LOSS = -0.07
TRAILING_ACTIVATE = 0.08
MAX_HOLD_MIN = 45
MAX_POSITIONS = 2
INITIAL_CAPITAL = 280

def get_gainers(date):
    """Get penny stocks that had significant moves on a given date"""
    url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date}"
    r = requests.get(url, params={"apiKey": API_KEY, "adjusted": "false"})
    time.sleep(0.25)
    if r.status_code != 200:
        print(f"  Error fetching grouped bars for {date}: {r.status_code}")
        return []
    data = r.json()
    candidates = []
    for t in data.get("results", []):
        sym = t.get("T", "")
        if sym.startswith("X:") or sym.startswith("O:") or sym.startswith("I:"):
            continue
        if len(sym) > 5:  # skip warrants etc
            continue
        c = t.get("c", 0)
        o = t.get("o", 0)
        v = t.get("v", 0)
        h = t.get("h", 0)
        # Tighter filter: need intraday range >10% and good volume for penny stocks
        if 0.7 <= c <= 10 and v > 500000:
            if o > 0 and (h - o) / o > 0.10:
                candidates.append(sym)
    print(f"  {date}: {len(candidates)} candidates from daily gainers")
    return candidates

def get_1min_bars(ticker, date):
    """Fetch 1-min bars including pre/after market"""
    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}"
    r = requests.get(url, params={"apiKey": API_KEY, "adjusted": "false", "sort": "asc", "limit": 50000})
    time.sleep(0.15)
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get("results", []) or []

def compute_5min_bars(bars_1min):
    """Aggregate 1-min bars into 5-min bars"""
    if not bars_1min:
        return []
    bars_5 = []
    chunk = []
    for b in bars_1min:
        chunk.append(b)
        if len(chunk) == 5:
            bars_5.append({
                "t": chunk[0]["t"],
                "o": chunk[0]["o"],
                "h": max(x["h"] for x in chunk),
                "l": min(x["l"] for x in chunk),
                "c": chunk[-1]["c"],
                "v": sum(x["v"] for x in chunk),
            })
            chunk = []
    if chunk:
        bars_5.append({
            "t": chunk[0]["t"],
            "o": chunk[0]["o"],
            "h": max(x["h"] for x in chunk),
            "l": min(x["l"] for x in chunk),
            "c": chunk[-1]["c"],
            "v": sum(x["v"] for x in chunk),
        })
    return bars_5

def find_signals(bars_1min, bars_5min, volume_threshold_pct):
    """Find entry signals: 5min +5% move with volume spike"""
    signals = []
    if len(bars_5min) < 6:
        return signals
    
    for i in range(5, len(bars_5min)):
        b5 = bars_5min[i]
        price_change = (b5["c"] - b5["o"]) / b5["o"] if b5["o"] > 0 else 0
        if price_change < 0.05:
            continue
        if b5["c"] < 0.7 or b5["c"] > 10:
            continue
        if b5["v"] < 10000:
            continue
        
        # Volume spike: compare to avg of previous 5 bars
        prev_vols = [bars_5min[j]["v"] for j in range(i-5, i)]
        avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 1
        if avg_vol < 1:
            avg_vol = 1
        spike_pct = (b5["v"] / avg_vol) * 100
        
        if spike_pct >= volume_threshold_pct:
            # Entry at close of this 5-min bar
            entry_time = b5["t"] + 5 * 60 * 1000  # ms, next minute
            signals.append({
                "entry_time_ms": b5["t"] + 4 * 60 * 1000,
                "entry_price": b5["c"],
                "spike_pct": spike_pct,
                "volume": b5["v"],
            })
    return signals

def simulate_trade(bars_1min, signal):
    """Simulate a single trade with v7 exit strategy"""
    entry_price = signal["entry_price"]
    entry_time = signal["entry_time_ms"]
    
    # Find bars after entry
    trade_bars = [b for b in bars_1min if b["t"] > entry_time]
    if not trade_bars:
        return None
    
    peak = entry_price
    exit_price = None
    exit_reason = None
    exit_time = None
    
    for b in trade_bars:
        elapsed_min = (b["t"] - entry_time) / 60000
        if elapsed_min > MAX_HOLD_MIN:
            exit_price = b["o"]
            exit_reason = "timeout"
            exit_time = b["t"]
            break
        
        current = b["h"]
        if current > peak:
            peak = current
        
        pnl_pct = (b["l"] - entry_price) / entry_price
        peak_pct = (peak - entry_price) / entry_price
        
        # Time weight
        time_weight = 0.8 if elapsed_min >= 30 else 1.0
        adjusted_stop = STOP_LOSS * time_weight
        
        # Stop loss check
        if pnl_pct <= adjusted_stop:
            exit_price = entry_price * (1 + adjusted_stop)
            exit_reason = "stoploss"
            exit_time = b["t"]
            break
        
        # Trailing stop
        if peak_pct >= TRAILING_ACTIVATE:
            if peak_pct >= 0.80:
                trail = 0.30
            elif peak_pct >= 0.50:
                trail = 0.08
            elif peak_pct >= 0.15:
                trail = 0.05
            else:
                trail = 0.03
            
            trail *= time_weight
            trail_price = peak * (1 - trail)
            if b["l"] <= trail_price:
                exit_price = trail_price
                exit_reason = "trailing"
                exit_time = b["t"]
                break
    
    if exit_price is None:
        # End of day
        exit_price = trade_bars[-1]["c"]
        exit_reason = "timeout"
        exit_time = trade_bars[-1]["t"]
    
    pnl_pct = (exit_price - entry_price) / entry_price
    hold_min = (exit_time - entry_time) / 60000
    
    return {
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "hold_min": hold_min,
    }

def run_backtest(all_data, volume_threshold):
    """Run backtest for a specific volume threshold across all dates/tickers"""
    trades = []
    
    for (date, ticker), (bars_1min, bars_5min) in all_data.items():
        signals = find_signals(bars_1min, bars_5min, volume_threshold)
        
        # Simulate with max 2 concurrent positions
        active_trades = []
        for sig in signals:
            # Remove expired active trades
            active_trades = [t for t in active_trades if t["exit_time_ms"] > sig["entry_time_ms"]]
            if len(active_trades) >= MAX_POSITIONS:
                continue
            
            result = simulate_trade(bars_1min, sig)
            if result:
                result["ticker"] = ticker
                result["date"] = date
                trades.append(result)
                active_trades.append({"exit_time_ms": sig["entry_time_ms"] + result["hold_min"] * 60000})
    
    return trades

def compute_stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / n * 100
    avg_pnl = sum(t["pnl_pct"] for t in trades) / n * 100
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) * 100 if wins else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses) * 100) if losses else 0.01
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')
    max_gain = max(t["pnl_pct"] for t in trades) * 100
    avg_hold = sum(t["hold_min"] for t in trades) / n
    
    sl = sum(1 for t in trades if t["exit_reason"] == "stoploss")
    tr = sum(1 for t in trades if t["exit_reason"] == "trailing")
    to = sum(1 for t in trades if t["exit_reason"] == "timeout")
    
    # Simulate cumulative P&L
    capital = INITIAL_CAPITAL
    for t in trades:
        position_size = capital / MAX_POSITIONS
        capital += position_size * t["pnl_pct"]
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    return {
        "trades": n,
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "profit_factor": profit_factor,
        "max_gain": max_gain,
        "avg_hold": avg_hold,
        "sl_pct": sl/n*100,
        "tr_pct": tr/n*100,
        "to_pct": to/n*100,
        "total_return": total_return,
        "final_capital": capital,
    }

def main():
    print("=" * 60)
    print("거래량 스파이크 기준별 백테스트 비교")
    print("=" * 60)
    
    # Step 1: Get candidates for each date
    all_candidates = {}
    for date in DATES:
        print(f"\n📅 {date} 후보 종목 검색...")
        candidates = get_gainers(date)
        all_candidates[date] = candidates
    
    # Step 2: Fetch 1-min bars for all candidates
    all_data = {}
    total = sum(len(v) for v in all_candidates.values())
    count = 0
    for date, tickers in all_candidates.items():
        for ticker in tickers:
            count += 1
            if count % 20 == 0:
                print(f"  데이터 수집 중... {count}/{total}")
            bars = get_1min_bars(ticker, date)
            if len(bars) < 30:
                continue
            bars_5 = compute_5min_bars(bars)
            all_data[(date, ticker)] = (bars, bars_5)
    
    print(f"\n✅ 총 {len(all_data)}개 종목/일 데이터 수집 완료")
    
    # Step 3: Run backtests for each threshold
    results = {}
    for thresh in VOLUME_THRESHOLDS:
        print(f"\n🔍 거래량 스파이크 {thresh}% 백테스트...")
        trades = run_backtest(all_data, thresh)
        stats = compute_stats(trades)
        results[thresh] = stats
        if stats:
            print(f"   거래 {stats['trades']}건, 승률 {stats['win_rate']:.1f}%, 평균수익 {stats['avg_pnl']:.2f}%")
        else:
            print(f"   거래 없음")
    
    # Step 4: Generate report
    report = generate_report(results)
    
    with open("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_volume_compare.md", "w") as f:
        f.write(report)
    
    print(f"\n📄 결과 저장: backtest_volume_compare.md")
    print(report)

def generate_report(results):
    lines = []
    lines.append("# 거래량 스파이크 기준별 백테스트 비교 결과")
    lines.append("")
    lines.append(f"**기간:** 2025-02-11 ~ 2025-02-18 (5거래일)")
    lines.append(f"**전략:** v7 매도전략 | 초기자본 $280 | 동시보유 2종목")
    lines.append(f"**대상:** $0.7~$10 페니스탁, 5분봉 +5% 급등, 최소거래량 10,000주")
    lines.append("")
    
    # Comparison table
    lines.append("## 📊 비교 테이블")
    lines.append("")
    lines.append("| 스파이크 기준 | 거래수 | 승률 | 평균수익률 | 손익비 | 최대수익 | 평균보유 | 총수익률 | 최종자본 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    
    for thresh in VOLUME_THRESHOLDS:
        s = results.get(thresh)
        if s:
            lines.append(f"| **{thresh}%** | {s['trades']} | {s['win_rate']:.1f}% | {s['avg_pnl']:.2f}% | {s['profit_factor']:.2f} | +{s['max_gain']:.1f}% | {s['avg_hold']:.0f}분 | {s['total_return']:.1f}% | ${s['final_capital']:.0f} |")
        else:
            lines.append(f"| **{thresh}%** | 0 | - | - | - | - | - | - | $280 |")
    
    lines.append("")
    
    # Exit reason breakdown
    lines.append("## 🔄 청산 사유 비율")
    lines.append("")
    lines.append("| 스파이크 기준 | 손절 | 트레일링 | 시간초과 |")
    lines.append("|:---:|:---:|:---:|:---:|")
    
    for thresh in VOLUME_THRESHOLDS:
        s = results.get(thresh)
        if s:
            lines.append(f"| **{thresh}%** | {s['sl_pct']:.0f}% | {s['tr_pct']:.0f}% | {s['to_pct']:.0f}% |")
        else:
            lines.append(f"| **{thresh}%** | - | - | - |")
    
    lines.append("")
    
    # Individual details
    for thresh in VOLUME_THRESHOLDS:
        s = results.get(thresh)
        lines.append(f"### 스파이크 {thresh}%")
        if s:
            lines.append(f"- 거래 수: {s['trades']}건")
            lines.append(f"- 승률: {s['win_rate']:.1f}%")
            lines.append(f"- 평균 수익률: {s['avg_pnl']:.2f}%")
            lines.append(f"- 손익비: {s['profit_factor']:.2f}")
            lines.append(f"- 최대 수익: +{s['max_gain']:.1f}%")
            lines.append(f"- 평균 보유시간: {s['avg_hold']:.0f}분")
            lines.append(f"- 총 수익률: {s['total_return']:.1f}%")
        else:
            lines.append("- 해당 기준 충족 거래 없음")
        lines.append("")
    
    # Recommendation
    lines.append("## 💡 추천")
    lines.append("")
    
    valid = {k: v for k, v in results.items() if v and v["trades"] >= 3}
    if valid:
        # Best by total return
        best_return = max(valid.items(), key=lambda x: x[1]["total_return"])
        # Best by win rate (with decent trades)
        best_wr = max(valid.items(), key=lambda x: x[1]["win_rate"])
        # Best by profit factor
        best_pf = max(valid.items(), key=lambda x: x[1]["profit_factor"])
        
        lines.append(f"- **최고 총수익률:** {best_return[0]}% 스파이크 → {best_return[1]['total_return']:.1f}%")
        lines.append(f"- **최고 승률:** {best_wr[0]}% 스파이크 → {best_wr[1]['win_rate']:.1f}%")
        lines.append(f"- **최고 손익비:** {best_pf[0]}% 스파이크 → {best_pf[1]['profit_factor']:.2f}")
        lines.append("")
        
        # Overall recommendation
        # Score: normalize and weight
        best_overall = max(valid.items(), key=lambda x: (
            x[1]["total_return"] * 0.4 + 
            x[1]["win_rate"] * 0.3 + 
            x[1]["profit_factor"] * 10 * 0.3
        ))
        lines.append(f"### 🏆 종합 추천: **{best_overall[0]}% 스파이크 기준**")
        lines.append(f"- 거래 {best_overall[1]['trades']}건, 승률 {best_overall[1]['win_rate']:.1f}%, 총수익 {best_overall[1]['total_return']:.1f}%")
        lines.append("")
        
        # Analysis
        if best_overall[0] >= 500:
            lines.append("> 높은 스파이크 기준이 효과적 → **\"되는 종목만 골라 크게 먹는\" 전략 유효**")
        else:
            lines.append("> 중간 수준의 스파이크 기준이 최적 → 거래 빈도와 품질의 균형점")
    else:
        lines.append("충분한 거래 데이터가 없어 추천 불가")
    
    lines.append("")
    lines.append(f"---")
    lines.append(f"*생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    
    return "\n".join(lines)

if __name__ == "__main__":
    main()
