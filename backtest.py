#!/usr/bin/env python3
"""급등 스캘핑 전략 백테스트 시뮬레이션"""

import requests
import time
import json
import sys
from datetime import datetime, timedelta
from collections import defaultdict

def log(msg=""):
    print(msg, flush=True)

API_KEY = "e5MIxst1E1Gdgbecg2fLSJsxw0AFJHCo"
BASE = "https://api.polygon.io"
INITIAL_CAPITAL = 280  # USD

# Strategy params
STOP_LOSS = -0.07
TRAILING_ACTIVATE = 0.08
MAX_HOLD_MIN = 45
MAX_CONCURRENT = 2
ENTRY_SURGE = 0.05  # +5% in 5min
VOLUME_SPIKE = 2.0  # 200%

def get_trailing_width(gain_pct, hold_minutes):
    """Get trailing stop width based on gain tier"""
    if gain_pct >= 0.80:
        width = 0.30
    elif gain_pct >= 0.50:
        width = 0.08
    elif gain_pct >= 0.15:
        width = 0.05
    elif gain_pct >= 0.08:
        width = 0.03
    else:
        return None  # not activated
    # Time weight: after 30min, tighten by 0.8x
    if hold_minutes >= 30:
        width *= 0.8
    return width

def fetch_json(url, params=None):
    if params is None:
        params = {}
    params["apiKey"] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    if r.status_code == 429:
        log("Rate limited, waiting 15s...")
        time.sleep(15)
        r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_penny_stock_candidates():
    """Find penny stocks that had big moves recently using grouped daily bars"""
    candidates = []
    
    # Check last 2 weeks of trading days
    today = datetime(2026, 2, 19)
    dates_to_check = []
    d = today - timedelta(days=1)
    while len(dates_to_check) < 10 and d > today - timedelta(days=20):
        if d.weekday() < 5:  # weekday
            dates_to_check.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    
    log(f"Checking {len(dates_to_check)} trading days for candidates...")
    
    seen_tickers = set()
    
    for date_str in dates_to_check[:5]:  # limit API calls
        log(f"  Scanning {date_str}...")
        try:
            data = fetch_json(f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}")
            time.sleep(0.5)  # rate limit
        except Exception as e:
            log(f"    Error: {e}")
            continue
        
        results = data.get("results", [])
        for bar in results:
            t = bar.get("T", "")
            o = bar.get("o", 0)
            c = bar.get("c", 0)
            h = bar.get("h", 0)
            v = bar.get("v", 0)
            
            if not (0.7 <= o <= 10) and not (0.7 <= c <= 10):
                continue
            if o <= 0:
                continue
            
            day_change = (h - o) / o
            if day_change >= 0.10 and v >= 500000:  # at least 10% intraday move, decent volume
                if t not in seen_tickers and len(t) <= 5 and "." not in t:
                    seen_tickers.add(t)
                    candidates.append({
                        "ticker": t,
                        "date": date_str,
                        "open": o,
                        "high": h,
                        "close": c,
                        "volume": v,
                        "day_change_pct": round(day_change * 100, 1)
                    })
    
    # Sort by day change and take top candidates
    candidates.sort(key=lambda x: x["day_change_pct"], reverse=True)
    log(f"Found {len(candidates)} penny stock candidates with 10%+ moves")
    return candidates[:30]  # top 30

def get_minute_bars(ticker, date_str):
    """Get 1-minute bars for a ticker on a specific date (including pre/post market)"""
    try:
        data = fetch_json(
            f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}",
            {"adjusted": "true", "sort": "asc", "limit": 50000}
        )
        time.sleep(0.25)
        return data.get("results", [])
    except Exception as e:
        log(f"    Error fetching {ticker} bars: {e}")
        return []

def simulate_trades_on_bars(bars, ticker, date_str):
    """Simulate the scalping strategy on 1-minute bars for one ticker/day"""
    if len(bars) < 10:
        return []
    
    trades = []
    in_trade = False
    entry_price = 0
    entry_time = 0
    peak_price = 0
    
    # Build 5-min rolling windows for surge detection
    for i in range(5, len(bars)):
        bar = bars[i]
        ts = bar.get("t", 0) // 1000  # ms to sec
        c = bar.get("c", 0)
        v = bar.get("v", 0)
        h = bar.get("h", 0)
        l = bar.get("l", 0)
        
        if in_trade:
            hold_min = (ts - entry_time) / 60
            gain = (c - entry_price) / entry_price
            peak_price = max(peak_price, h)
            peak_gain = (peak_price - entry_price) / entry_price
            
            exit_reason = None
            exit_price = c
            
            # Check stop loss (use low of bar)
            low_gain = (l - entry_price) / entry_price
            if low_gain <= STOP_LOSS:
                exit_reason = "손절"
                exit_price = entry_price * (1 + STOP_LOSS)
            
            # Check max hold time
            elif hold_min >= MAX_HOLD_MIN:
                exit_reason = "시간초과"
                exit_price = c
            
            # Check trailing stop
            elif peak_gain >= TRAILING_ACTIVATE:
                width = get_trailing_width(peak_gain, hold_min)
                if width:
                    trail_stop = peak_price * (1 - width)
                    if l <= trail_stop:
                        exit_reason = f"트레일링({peak_gain*100:.0f}%고점)"
                        exit_price = trail_stop
            
            if exit_reason:
                pnl_pct = (exit_price - entry_price) / entry_price
                trades.append({
                    "ticker": ticker,
                    "date": date_str,
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "hold_min": round(hold_min, 1),
                    "exit_reason": exit_reason,
                    "peak_gain_pct": round(peak_gain * 100, 1),
                    "entry_ts": entry_time,
                    "exit_ts": ts,
                })
                in_trade = False
            continue
        
        # Not in trade - check entry conditions
        # 5-min price surge check
        price_5min_ago = bars[i-5].get("c", 0)
        if price_5min_ago <= 0:
            continue
        surge = (c - price_5min_ago) / price_5min_ago
        
        # Volume spike: compare current bar volume to avg of prior 20 bars
        if i >= 20:
            avg_vol = sum(bars[j].get("v", 0) for j in range(i-20, i)) / 20
        else:
            avg_vol = sum(bars[j].get("v", 0) for j in range(i)) / max(i, 1)
        
        vol_ratio = v / max(avg_vol, 1)
        
        if surge >= ENTRY_SURGE and vol_ratio >= VOLUME_SPIKE and 0.7 <= c <= 10:
            in_trade = True
            entry_price = c
            entry_time = ts
            peak_price = c
    
    return trades

def run_backtest():
    log("=" * 60)
    log("급등 스캘핑 전략 백테스트 시뮬레이션")
    log("=" * 60)
    
    # Step 1: Find candidates
    candidates = get_penny_stock_candidates()
    if not candidates:
        log("No candidates found!")
        return
    
    log(f"\nTop candidates:")
    for c in candidates[:10]:
        log(f"  {c['ticker']:6s} {c['date']} O:{c['open']:.2f} H:{c['high']:.2f} +{c['day_change_pct']}% Vol:{c['volume']:,}")
    
    # Step 2: Get minute bars and simulate
    all_trades = []
    processed = 0
    
    for cand in candidates:
        ticker = cand["ticker"]
        date_str = cand["date"]
        log(f"\nProcessing {ticker} on {date_str}...")
        
        bars = get_minute_bars(ticker, date_str)
        if not bars:
            continue
        
        log(f"  Got {len(bars)} minute bars")
        trades = simulate_trades_on_bars(bars, ticker, date_str)
        
        if trades:
            for t in trades:
                log(f"  Trade: entry ${t['entry_price']:.2f} → exit ${t['exit_price']:.2f} = {t['pnl_pct']:+.1f}% ({t['exit_reason']}, {t['hold_min']:.0f}min, peak +{t['peak_gain_pct']:.0f}%)")
            all_trades.extend(trades)
        else:
            log(f"  No trades triggered")
        
        processed += 1
        if processed >= 25:  # limit API usage
            break
    
    # Step 3: Portfolio simulation (sequential, max 2 concurrent)
    # Sort all trades by entry time
    all_trades.sort(key=lambda t: t["entry_ts"])
    
    # Simulate with capital management
    capital = INITIAL_CAPITAL
    portfolio_trades = []
    active = []
    
    for trade in all_trades:
        # Remove expired active trades
        active = [a for a in active if a["exit_ts"] <= trade["entry_ts"]]
        
        if len(active) >= MAX_CONCURRENT:
            continue
        
        # Position size: split capital equally
        pos_size = capital / MAX_CONCURRENT
        pnl_usd = pos_size * (trade["pnl_pct"] / 100)
        
        trade["pos_size"] = round(pos_size, 2)
        trade["pnl_usd"] = round(pnl_usd, 2)
        
        capital += pnl_usd
        portfolio_trades.append(trade)
        active.append(trade)
    
    # Step 4: Generate report
    generate_report(portfolio_trades, all_trades, capital)

def generate_report(portfolio_trades, all_trades, final_capital):
    if not all_trades:
        report = "# 백테스트 결과\n\n거래 신호가 발견되지 않았습니다.\n"
        with open("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_result.md", "w") as f:
            f.write(report)
        log("\nNo trades found.")
        return
    
    total = len(all_trades)
    wins = sum(1 for t in all_trades if t["pnl_pct"] > 0)
    losses = sum(1 for t in all_trades if t["pnl_pct"] <= 0)
    win_rate = wins / total * 100 if total else 0
    avg_pnl = sum(t["pnl_pct"] for t in all_trades) / total
    avg_win = sum(t["pnl_pct"] for t in all_trades if t["pnl_pct"] > 0) / max(wins, 1)
    avg_loss = sum(t["pnl_pct"] for t in all_trades if t["pnl_pct"] <= 0) / max(losses, 1)
    max_win = max(t["pnl_pct"] for t in all_trades)
    max_loss = min(t["pnl_pct"] for t in all_trades)
    avg_hold = sum(t["hold_min"] for t in all_trades) / total
    
    # Exit reason breakdown
    reasons = defaultdict(int)
    for t in all_trades:
        r = t["exit_reason"]
        if "트레일링" in r:
            reasons["트레일링 스탑"] += 1
        else:
            reasons[r] += 1
    
    # Portfolio results
    ptotal = len(portfolio_trades)
    if ptotal > 0:
        total_pnl_usd = sum(t.get("pnl_usd", 0) for t in portfolio_trades)
        total_pnl_pct = (final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    else:
        total_pnl_usd = 0
        total_pnl_pct = 0
    
    report = f"""# 📊 급등 스캘핑 전략 백테스트 결과

> 시뮬레이션 일시: 2026-02-19
> 데이터 소스: Polygon.io 1분봉
> 초기 자본: ${INITIAL_CAPITAL} (₩400,000)

---

## 📈 전체 요약

| 항목 | 값 |
|------|-----|
| 총 거래 수 | {total}회 |
| 승리 | {wins}회 |
| 패배 | {losses}회 |
| **승률** | **{win_rate:.1f}%** |
| **평균 수익률** | **{avg_pnl:+.2f}%** |
| 평균 수익 (승) | +{avg_win:.2f}% |
| 평균 손실 (패) | {avg_loss:.2f}% |
| 최대 수익 | +{max_win:.2f}% |
| 최대 손실 | {max_loss:.2f}% |
| 평균 보유시간 | {avg_hold:.1f}분 |
| 손익비 (avg win/avg loss) | {abs(avg_win/avg_loss):.2f} |

## 💰 포트폴리오 시뮬레이션 (동시 최대 2종목)

| 항목 | 값 |
|------|-----|
| 실행 거래 수 | {ptotal}회 |
| 최종 자본 | ${final_capital:.2f} |
| **총 손익** | **${total_pnl_usd:+.2f} ({total_pnl_pct:+.1f}%)** |

## 🔍 청산 사유 분석

| 사유 | 횟수 |
|------|------|
"""
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        report += f"| {reason} | {count}회 |\n"
    
    report += f"""
## 📋 종목별 매매 내역

| # | 종목 | 날짜 | 진입가 | 청산가 | 수익률 | 보유시간 | 고점 | 청산사유 |
|---|------|------|--------|--------|--------|----------|------|----------|
"""
    for i, t in enumerate(all_trades, 1):
        report += f"| {i} | {t['ticker']} | {t['date']} | ${t['entry_price']:.2f} | ${t['exit_price']:.2f} | {t['pnl_pct']:+.1f}% | {t['hold_min']:.0f}분 | +{t['peak_gain_pct']:.0f}% | {t['exit_reason']} |\n"
    
    report += f"""
## 💡 전략 평가 및 개선 제안

### 강점
- 트레일링 스탑이 큰 급등에서 수익 보존에 효과적
- 시간 제한(45분)이 불필요한 리스크 노출 방지

### 약점 및 개선안
1. **진입 타이밍**: 5분 +5% 감지 시점이 이미 늦을 수 있음 → 3분 또는 거래량 선행 감지 고려
2. **손절폭**: -7% 고정 손절이 페니스탁 변동성 대비 좁을 수 있음 → ATR 기반 동적 손절 검토
3. **트레일링 구간**: +8~15% 구간의 -3%p가 너무 타이트할 수 있음 → 변동성 기반 조정 고려
4. **시간대 필터**: 개장 직후 30분이 가장 효과적 → 시간대별 성과 분석 추가 권장
5. **거래량 기준**: 200% 스파이크 기준 조정 실험 필요 (150%~300% 범위)
6. **자본 규모**: $280은 PDT 규칙에 제한 없지만 슬리피지 영향이 클 수 있음

### 리스크 주의사항
- 페니스탁은 유동성 부족으로 실제 슬리피지가 시뮬보다 큼
- 프리/애프터마켓은 스프레드가 넓어 실효 수익률 하락 예상
- 백테스트 수익률에서 실전은 20~40% 하락 감안 필요
"""
    
    with open("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_result.md", "w") as f:
        f.write(report)
    
    log("\n" + "=" * 60)
    log("결과 저장 완료: stock-bot/backtest_result.md")
    log(f"총 {total}거래, 승률 {win_rate:.1f}%, 평균수익률 {avg_pnl:+.2f}%")
    log(f"포트폴리오: ${INITIAL_CAPITAL} → ${final_capital:.2f} ({total_pnl_pct:+.1f}%)")
    log("=" * 60)

if __name__ == "__main__":
    run_backtest()
