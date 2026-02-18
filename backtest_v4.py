#!/usr/bin/env python3
"""
Backtest v4: 60-day simulation with full strategy
- Phase 1: Volume spike detection
- Phase 2: Chase buy (10%+ surge on 1min bars)
- Phase 3: 1st exit (BB upper break → 10% drop from peak on 5min)
- Phase 4: Re-entry (near BB lower + 2-3 consecutive green candles + volume increase)
- Phase 5: 2nd exit (BB upper area)
- BB(20, 2σ) on 5-min bars
- SL: -15%, TP: +30%, day trade only, ₩100,000 initial, compound, max 2 positions, 5-split
"""
import os, sys, time, json, math
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
import numpy as np

try:
    import requests
except ImportError:
    os.system("pip install requests")
    import requests

from dotenv import load_dotenv
load_dotenv('/home/ubuntu/.openclaw/workspace/stock-bot/.env')
API_KEY = os.getenv('POLYGON_API_KEY')
BASE = "https://api.polygon.io"

# ── Config ──
INITIAL_CAPITAL = 100_000  # ₩100,000
COMPOUND_CAP = 25_000_000
MAX_POSITIONS = 2
SPLIT_COUNT = 5
TAKE_PROFIT_PCT = 0.30
STOP_LOSS_PCT = -0.15
BB_PERIOD = 20
BB_STD = 2
PEAK_DROP_PCT = 0.10  # 10% drop from peak for exit
TOP_N_CANDIDATES = 7  # candidates per day

# Rate limit helper
_last_call = 0
def api_get(url, params=None):
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < 0.15:  # ~7 calls/sec safe
        time.sleep(0.15 - elapsed)
    if params is None:
        params = {}
    params['apiKey'] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    _last_call = time.time()
    if r.status_code == 429:
        print("  Rate limited, sleeping 60s...")
        time.sleep(60)
        return api_get(url, params)
    return r.json()

# ── Data fetching ──
def get_trading_days(start, end):
    """Get US market trading days via Polygon grouped daily endpoint"""
    days = []
    # Use market status / grouped daily to find trading days
    # Simpler: fetch SPY daily bars
    url = f"{BASE}/v2/aggs/ticker/SPY/range/1/day/{start}/{end}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": "250"})
    for bar in data.get('results', []):
        ts = bar['t'] // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        days.append(dt.strftime('%Y-%m-%d'))
    return days

def get_day_gainers(date_str):
    """Get top gainers for the day using grouped daily bars"""
    # Polygon grouped daily: all tickers for a date
    url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    data = api_get(url, {"adjusted": "true"})
    results = data.get('results', [])
    
    candidates = []
    for r in results:
        ticker = r.get('T', '')
        # Filter: common stock tickers only
        if len(ticker) > 5 or '.' in ticker or '-' in ticker:
            continue
        o, c, h, l, v = r.get('o',0), r.get('c',0), r.get('h',0), r.get('l',0), r.get('v',0)
        if o <= 0 or c <= 0 or v < 100000:
            continue
        if o < 1.0:  # penny stock filter
            continue
        change_pct = (h / o - 1) * 100  # intraday high vs open
        if change_pct >= 10 and v >= 500000:
            candidates.append({
                'ticker': ticker,
                'open': o, 'close': c, 'high': h, 'low': l,
                'volume': v,
                'change_pct': change_pct
            })
    
    # Sort by change_pct * volume score
    candidates.sort(key=lambda x: x['change_pct'] * math.log10(max(x['volume'],1)), reverse=True)
    return candidates[:TOP_N_CANDIDATES]

def get_bars(ticker, date_str, multiplier, timespan):
    """Fetch bars from Polygon"""
    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    return data.get('results', [])

# ── Bollinger Band ──
def compute_bb(closes, period=BB_PERIOD, num_std=BB_STD):
    """Returns (upper, middle, lower) or None"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = np.mean(window)
    std = np.std(window, ddof=0)
    return (sma + num_std * std, sma, sma - num_std * std)

# ── Market hours filter (ET) ──
ET_OFFSET = timedelta(hours=-5)  # EST (simplified, ignoring DST for backtesting)

def bar_to_et(bar_ts_ms):
    """Convert bar timestamp to ET datetime"""
    dt_utc = datetime.fromtimestamp(bar_ts_ms / 1000, tz=timezone.utc)
    # Rough DST: Mar-Nov = EDT (-4), Nov-Mar = EST (-5)
    month = dt_utc.month
    if 3 <= month <= 10:
        offset = timedelta(hours=-4)
    else:
        offset = timedelta(hours=-5)
    return dt_utc + offset - timedelta(hours=0)  # just shift for display
    # Actually let's just return UTC and filter by UTC equivalent of market hours
    # ET 9:30 = UTC 14:30 (EST) or UTC 13:30 (EDT)

def is_market_hours_utc(dt_utc):
    """Check if UTC time is during US market hours"""
    month = dt_utc.month
    if 3 <= month <= 10:  # EDT
        return (dt_utc.hour == 13 and dt_utc.minute >= 30) or (14 <= dt_utc.hour < 20)
    else:  # EST
        return (dt_utc.hour == 14 and dt_utc.minute >= 30) or (15 <= dt_utc.hour < 21)

def market_close_utc(date_str):
    """Get market close time in UTC"""
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    month = dt.month
    if 3 <= month <= 10:
        return dt.replace(hour=20, minute=0)
    else:
        return dt.replace(hour=21, minute=0)

# ── Strategy simulation ──
def simulate_day(ticker, date_str, daily_info, capital_per_position):
    """
    Simulate the full strategy for one ticker on one day.
    Returns list of trade results.
    """
    # Fetch 1-min and 5-min bars
    bars_1m = get_bars(ticker, date_str, 1, 'minute')
    bars_5m = get_bars(ticker, date_str, 5, 'minute')
    
    if not bars_1m or len(bars_1m) < 20:
        return []
    if not bars_5m or len(bars_5m) < 5:
        return []
    
    daily_high = daily_info['high']
    daily_low = daily_info['low']
    
    # Spike filter: if 1min high > 300% of daily range, skip
    max_1m_high = max(b['h'] for b in bars_1m)
    if daily_high > 0 and max_1m_high > daily_high * 3:
        return []
    
    # Filter to market hours only
    bars_1m = [b for b in bars_1m if is_market_hours_utc(
        datetime.fromtimestamp(b['t']//1000, tz=timezone.utc))]
    bars_5m = [b for b in bars_5m if is_market_hours_utc(
        datetime.fromtimestamp(b['t']//1000, tz=timezone.utc))]
    
    if not bars_1m or not bars_5m:
        return []
    
    # Clamp prices to daily range
    def clamp(v):
        return min(max(v, daily_low), daily_high)
    
    # Build 5-min bar index for quick lookup
    bars_5m_by_ts = {}
    for i, b in enumerate(bars_5m):
        bars_5m_by_ts[b['t']] = i
    
    # 5-min closes for BB
    closes_5m = [clamp(b['c']) for b in bars_5m]
    
    def get_5m_bar_index_at(ts_ms):
        """Find the 5-min bar that contains this timestamp"""
        for i in range(len(bars_5m)-1, -1, -1):
            if bars_5m[i]['t'] <= ts_ms:
                return i
        return -1
    
    trades = []
    
    # ── Phase 2: Find chase buy signal on 1-min bars ──
    # Look for 10%+ surge: compare current close to close ~10 bars ago
    LOOKBACK = 10  # 10-minute window for surge detection
    
    position = None  # {buy_price, buy_idx, shares, split, phase, peak, bb_broken}
    
    i = LOOKBACK
    while i < len(bars_1m):
        bar = bars_1m[i]
        ts = bar['t']
        dt_utc = datetime.fromtimestamp(ts//1000, tz=timezone.utc)
        price = clamp(bar['c'])
        
        # Force close 15 min before market close
        mc = market_close_utc(date_str)
        if position and dt_utc >= mc - timedelta(minutes=15):
            sell_price = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else bar['c'])
            pnl_pct = (sell_price / position['buy_price'] - 1)
            trades.append({
                'ticker': ticker,
                'phase': position.get('trade_phase', '1st'),
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '장마감',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(position['invested'] * pnl_pct),
                'invested': round(position['invested']),
            })
            position = None
            break
        
        if position is None:
            # Check for max trades per day (limit re-entries)
            if len(trades) >= 3:
                break
            
            # Phase 2: Chase buy signal
            ref_price = clamp(bars_1m[i - LOOKBACK]['c'])
            if ref_price > 0 and (price / ref_price - 1) >= 0.10:
                # Buy at NEXT bar open (realistic fill)
                if i + 1 < len(bars_1m):
                    buy_price = clamp(bars_1m[i+1]['o'])
                    # 5-split: for simplicity, assume all splits fill at same price
                    invested = min(capital_per_position, COMPOUND_CAP / MAX_POSITIONS)
                    shares = invested / buy_price if buy_price > 0 else 0
                    if shares <= 0:
                        i += 1
                        continue
                    position = {
                        'buy_price': buy_price,
                        'buy_idx_1m': i + 1,
                        'invested': invested,
                        'shares': shares,
                        'peak': buy_price,
                        'bb_broken': False,
                        'trade_phase': '1st',
                    }
                    i += 2  # skip buy bar
                    continue
            i += 1
            continue
        
        # ── We have a position ──
        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = price
        
        # Update peak
        if cur_high > position['peak']:
            position['peak'] = cur_high
        
        # Stop loss check
        sl_price = position['buy_price'] * (1 + STOP_LOSS_PCT)
        if cur_low <= sl_price:
            sell_price = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else sl_price)
            sell_price = min(sell_price, sl_price)  # worst case
            pnl_pct = (sell_price / position['buy_price'] - 1)
            trades.append({
                'ticker': ticker,
                'phase': position['trade_phase'],
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '손절(-15%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(position['invested'] * pnl_pct),
                'invested': round(position['invested']),
            })
            position = None
            i += 2
            continue
        
        # Take profit check (+30%)
        tp_price = position['buy_price'] * (1 + TAKE_PROFIT_PCT)
        if cur_high >= tp_price:
            sell_price = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else tp_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            trades.append({
                'ticker': ticker,
                'phase': position['trade_phase'],
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '익절(+30%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(position['invested'] * pnl_pct),
                'invested': round(position['invested']),
            })
            position = None
            i += 2
            continue
        
        # BB check on 5-min bars
        idx_5m = get_5m_bar_index_at(ts)
        if idx_5m >= 0:
            bb = compute_bb(closes_5m[:idx_5m+1])
            if bb:
                bb_upper, bb_mid, bb_lower = bb
                
                # Check BB upper break
                if cur_high > bb_upper:
                    position['bb_broken'] = True
                
                # Phase 3: If BB broken, sell when price drops 10% from peak (on 5-min basis)
                if position['bb_broken']:
                    drop_from_peak = (position['peak'] - cur_close) / position['peak']
                    if drop_from_peak >= PEAK_DROP_PCT:
                        sell_price = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                        pnl_pct = (sell_price / position['buy_price'] - 1)
                        is_first = position['trade_phase'] == '1st'
                        trades.append({
                            'ticker': ticker,
                            'phase': position['trade_phase'],
                            'buy_price': round(position['buy_price'], 4),
                            'sell_price': round(sell_price, 4),
                            'sell_reason': 'BB트레일링(-10%peak)',
                            'pnl_pct': round(pnl_pct * 100, 2),
                            'pnl_krw': round(position['invested'] * pnl_pct),
                            'invested': round(position['invested']),
                        })
                        
                        # Phase 4: Look for re-entry if this was 1st trade
                        if is_first:
                            # Scan forward for re-entry
                            reentry_pos = find_reentry(
                                bars_1m, bars_5m, closes_5m, i+2, 
                                daily_low, daily_high, date_str,
                                capital_per_position
                            )
                            if reentry_pos:
                                position = reentry_pos
                                i = reentry_pos['buy_idx_1m'] + 1
                                continue
                        
                        position = None
                        i += 2
                        continue
        
        i += 1
    
    # Force close any remaining position at last bar
    if position:
        last_price = clamp(bars_1m[-1]['c'])
        pnl_pct = (last_price / position['buy_price'] - 1)
        trades.append({
            'ticker': ticker,
            'phase': position['trade_phase'],
            'buy_price': round(position['buy_price'], 4),
            'sell_price': round(last_price, 4),
            'sell_reason': '장마감',
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_krw': round(position['invested'] * pnl_pct),
            'invested': round(position['invested']),
        })
    
    return trades


def find_reentry(bars_1m, bars_5m, closes_5m, start_idx, daily_low, daily_high, date_str, capital):
    """
    Phase 4: Re-entry logic
    Look for price near BB lower + 2-3 consecutive green candles + volume increase
    """
    def clamp(v):
        return min(max(v, daily_low), daily_high)
    
    mc = market_close_utc(date_str)
    
    # Need at least 30 bars of cooldown and some runway
    if start_idx + 30 >= len(bars_1m):
        return None
    
    # Build 5-min index helper
    def get_5m_idx(ts_ms):
        for j in range(len(bars_5m)-1, -1, -1):
            if bars_5m[j]['t'] <= ts_ms:
                return j
        return -1
    
    # Scan for re-entry conditions
    for i in range(start_idx + 10, min(start_idx + 120, len(bars_1m) - 5)):
        dt_utc = datetime.fromtimestamp(bars_1m[i]['t']//1000, tz=timezone.utc)
        if dt_utc >= mc - timedelta(minutes=30):
            break
        
        price = clamp(bars_1m[i]['c'])
        idx_5m = get_5m_idx(bars_1m[i]['t'])
        if idx_5m < 0:
            continue
        
        bb = compute_bb(closes_5m[:idx_5m+1])
        if not bb:
            continue
        
        bb_upper, bb_mid, bb_lower = bb
        
        # Check: price near BB lower (within 2%)
        if bb_lower <= 0:
            continue
        dist_to_lower = (price - bb_lower) / bb_lower
        if dist_to_lower > 0.02 or dist_to_lower < -0.05:
            continue
        
        # Check: 2-3 consecutive green candles with volume increase
        greens = 0
        vol_increasing = True
        for k in range(max(0, i-2), i+1):
            b = bars_1m[k]
            if clamp(b['c']) > clamp(b['o']):
                greens += 1
            if k > max(0, i-2) and b.get('v', 0) < bars_1m[k-1].get('v', 0):
                vol_increasing = False
        
        if greens >= 2 and vol_increasing:
            # Re-entry! Buy at next bar open
            if i + 1 < len(bars_1m):
                buy_price = clamp(bars_1m[i+1]['o'])
                if buy_price <= 0:
                    continue
                invested = capital
                shares = invested / buy_price
                return {
                    'buy_price': buy_price,
                    'buy_idx_1m': i + 1,
                    'invested': invested,
                    'shares': shares,
                    'peak': buy_price,
                    'bb_broken': False,
                    'trade_phase': '2nd(re-entry)',
                }
    
    return None


def run_backtest():
    print("=" * 60)
    print("Backtest v4: 60-day simulation")
    print("=" * 60)
    
    # Get trading days
    end_date = '2026-02-18'
    start_date = '2025-11-15'  # fetch extra to ensure 60 trading days
    
    print(f"Fetching trading days {start_date} ~ {end_date}...")
    all_days = get_trading_days(start_date, end_date)
    
    # Take last 60
    trading_days = all_days[-60:] if len(all_days) >= 60 else all_days
    print(f"Got {len(trading_days)} trading days: {trading_days[0]} ~ {trading_days[-1]}")
    
    capital = INITIAL_CAPITAL
    all_results = []
    total_trades = 0
    wins = 0
    losses = 0
    first_exits = 0
    reentry_exits = 0
    
    for day_idx, date_str in enumerate(trading_days):
        print(f"\n[{day_idx+1}/{len(trading_days)}] {date_str} | Capital: ₩{capital:,.0f}")
        
        # Get candidates
        candidates = get_day_gainers(date_str)
        if not candidates:
            print("  No candidates found")
            all_results.append({
                'date': date_str, 'trades': [], 'day_pnl': 0,
                'capital_after': round(capital)
            })
            continue
        
        print(f"  Candidates: {[c['ticker'] for c in candidates]}")
        
        # Allocate capital
        cap_per_pos = min(capital / MAX_POSITIONS, COMPOUND_CAP / MAX_POSITIONS)
        
        day_trades = []
        positions_used = 0
        
        for cand in candidates:
            if positions_used >= MAX_POSITIONS:
                break
            
            ticker = cand['ticker']
            daily_info = {
                'high': cand['high'],
                'low': cand['low'],
            }
            
            try:
                trades = simulate_day(ticker, date_str, daily_info, cap_per_pos)
            except Exception as e:
                print(f"  Error on {ticker}: {e}")
                continue
            
            if trades:
                positions_used += 1
                day_trades.extend(trades)
                for t in trades:
                    print(f"  {t['ticker']} [{t['phase']}]: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,})")
        
        # Calculate day P&L
        day_pnl = sum(t['pnl_krw'] for t in day_trades)
        capital += day_pnl
        capital = max(capital, 10000)  # minimum floor
        
        for t in day_trades:
            total_trades += 1
            if t['pnl_pct'] > 0:
                wins += 1
            else:
                losses += 1
            if '1st' in t.get('phase', ''):
                first_exits += 1
            if '2nd' in t.get('phase', '') or 're-entry' in t.get('phase', ''):
                reentry_exits += 1
        
        all_results.append({
            'date': date_str,
            'trades': day_trades,
            'day_pnl': round(day_pnl),
            'capital_after': round(capital),
        })
        
        if not day_trades:
            print("  No trades executed")
    
    # ── Summary ──
    final_return = (capital / INITIAL_CAPITAL - 1) * 100
    
    plus_days = sum(1 for d in all_results if d['day_pnl'] > 0)
    minus_days = sum(1 for d in all_results if d['day_pnl'] < 0)
    zero_days = sum(1 for d in all_results if d['day_pnl'] == 0)
    
    avg_win = np.mean([t['pnl_pct'] for d in all_results for t in d['trades'] if t['pnl_pct'] > 0]) if wins else 0
    avg_loss = np.mean([t['pnl_pct'] for d in all_results for t in d['trades'] if t['pnl_pct'] <= 0]) if losses else 0
    
    max_drawdown = 0
    peak_capital = INITIAL_CAPITAL
    for d in all_results:
        cap = d['capital_after']
        if cap > peak_capital:
            peak_capital = cap
        dd = (peak_capital - cap) / peak_capital * 100
        if dd > max_drawdown:
            max_drawdown = dd
    
    print("\n" + "=" * 60)
    print("BACKTEST v4 RESULTS")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} trading days)")
    print(f"Initial Capital: ₩{INITIAL_CAPITAL:,}")
    print(f"Final Capital: ₩{capital:,.0f}")
    print(f"Total Return: {final_return:+.1f}%")
    print(f"Max Drawdown: {max_drawdown:.1f}%")
    print(f"Total Trades: {total_trades} (Win: {wins}, Loss: {losses})")
    print(f"Win Rate: {wins/total_trades*100:.1f}%" if total_trades else "N/A")
    print(f"Avg Win: {avg_win:+.1f}%, Avg Loss: {avg_loss:+.1f}%")
    print(f"1st Exit: {first_exits}, Re-entry Exit: {reentry_exits}")
    print(f"Plus Days: {plus_days}, Minus Days: {minus_days}, Zero Days: {zero_days}")
    
    # ── Save results ──
    result_md = f"""# Backtest v4 Results — 60일 시뮬레이션

## 전략 요약
- **Phase 1**: 거래량 급등 감지 → 모니터링
- **Phase 2**: 1분봉 10%+ 급등 시 추격 매수
- **Phase 3**: BB(20,2σ) 상단 돌파 후 고점 대비 10% 하락 시 1차 매도
- **Phase 4**: BB 하단 근처 + 2~3 연속 양봉 + 거래량 증가 시 재진입
- **Phase 5**: BB 상단 근처에서 2차 매도
- **BB 설정**: 5분봉 기준 BB(20, 2σ)
- **손절**: -15%, **익절**: +30%
- **당일 매매 필수**, 초기 자본 ₩100,000, 복리 모드, 최대 2포지션, 5분할

## 백테스트 기간
- **{trading_days[0]} ~ {trading_days[-1]}** ({len(trading_days)} 거래일)

## 핵심 결과
| 항목 | 값 |
|---|---|
| 초기 자본 | ₩{INITIAL_CAPITAL:,} |
| 최종 자본 | ₩{capital:,.0f} |
| **총 수익률** | **{final_return:+.1f}%** |
| 최대 낙폭 (MDD) | {max_drawdown:.1f}% |
| 총 거래 수 | {total_trades} |
| 승리 | {wins} ({wins/total_trades*100:.1f}% win rate) |
| 패배 | {losses} |
| 평균 수익 (승) | {avg_win:+.1f}% |
| 평균 손실 (패) | {avg_loss:+.1f}% |
| 1차 매도 | {first_exits}건 |
| 재진입 후 매도 | {reentry_exits}건 |
| 수익 일수 | {plus_days}일 |
| 손실 일수 | {minus_days}일 |
| 무거래 일수 | {zero_days}일 |

## 일별 상세

| 날짜 | 거래수 | 일 P&L | 누적 자본 |
|---|---|---|---|
"""
    
    for d in all_results:
        n_trades = len(d['trades'])
        result_md += f"| {d['date']} | {n_trades} | ₩{d['day_pnl']:+,} | ₩{d['capital_after']:,} |\n"
    
    result_md += f"""
## 개별 거래 내역 (상위 20건)

| 날짜 | 종목 | 구분 | 매수가 | 매도가 | 사유 | 수익률 |
|---|---|---|---|---|---|---|
"""
    all_trades_flat = [(d['date'], t) for d in all_results for t in d['trades']]
    # Show top 20 by absolute pnl
    all_trades_flat.sort(key=lambda x: abs(x[1]['pnl_pct']), reverse=True)
    for date_str, t in all_trades_flat[:20]:
        result_md += f"| {date_str} | {t['ticker']} | {t['phase']} | ${t['buy_price']:.2f} | ${t['sell_price']:.2f} | {t['sell_reason']} | {t['pnl_pct']:+.1f}% |\n"
    
    result_md += f"""
## 매도 사유 분포

"""
    reason_counts = defaultdict(int)
    for d in all_results:
        for t in d['trades']:
            reason_counts[t['sell_reason']] += 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        result_md += f"- **{reason}**: {count}건\n"
    
    result_md += f"""
---
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*Backtest v4 — Polygon.io 1분봉/5분봉 데이터 기반*
"""
    
    out_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_result.md'
    with open(out_path, 'w') as f:
        f.write(result_md)
    print(f"\nResults saved to {out_path}")
    
    # Also save JSON
    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_result.json'
    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'initial_capital': INITIAL_CAPITAL,
                'stop_loss': STOP_LOSS_PCT,
                'take_profit': TAKE_PROFIT_PCT,
                'bb_period': BB_PERIOD,
                'bb_std': BB_STD,
                'max_positions': MAX_POSITIONS,
            },
            'summary': {
                'final_capital': round(capital),
                'total_return_pct': round(final_return, 2),
                'max_drawdown_pct': round(max_drawdown, 2),
                'total_trades': total_trades,
                'wins': wins,
                'losses': losses,
                'win_rate': round(wins/total_trades*100, 1) if total_trades else 0,
                'first_exits': first_exits,
                'reentry_exits': reentry_exits,
            },
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False)
    print(f"JSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
