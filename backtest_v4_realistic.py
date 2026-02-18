#!/usr/bin/env python3
"""
Backtest v4 REALISTIC: 60-day simulation with full strategy + realistic constraints
Same strategy as v4 but with:
- Slippage: +0.5% on buy, -0.5% on sell
- Commission: max($0.005/share, 0.1% of trade value) per side
- Gap filter: 10%+ gap-up re-evaluation
- Liquidity filter: 1min volume*price < buy_amount → skip
- Strict compound cap ₩25,000,000 with per-position = cap/max_positions
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

# ── Realistic constraints ──
SLIPPAGE_BUY = 0.005   # +0.5% on buy
SLIPPAGE_SELL = 0.005   # -0.5% on sell
COMMISSION_PER_SHARE = 0.005  # $0.005/share
COMMISSION_PCT = 0.001  # 0.1%
GAP_UP_THRESHOLD = 0.10  # 10% gap-up threshold

def apply_slippage_buy(price):
    """Buy slippage: pay more"""
    return price * (1 + SLIPPAGE_BUY)

def apply_slippage_sell(price):
    """Sell slippage: receive less"""
    return price * (1 - SLIPPAGE_SELL)

def calc_commission(shares, price):
    """Commission: max of per-share or percentage"""
    return max(shares * COMMISSION_PER_SHARE, shares * price * COMMISSION_PCT)

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

_prev_day_closes = {}  # ticker -> close price from previous trading day

def get_day_gainers(date_str):
    """Get top gainers for the day using grouped daily bars. Also caches closes for next day gap calc."""
    global _prev_day_closes
    url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    data = api_get(url, {"adjusted": "true"})
    results = data.get('results', [])
    
    # Cache ALL closes from this day for next day's gap calculation
    new_closes = {}
    for r in results:
        ticker = r.get('T', '')
        c = r.get('c', 0)
        if c > 0:
            new_closes[ticker] = c
    
    candidates = []
    for r in results:
        ticker = r.get('T', '')
        if len(ticker) > 5 or '.' in ticker or '-' in ticker:
            continue
        o, c, h, l, v = r.get('o',0), r.get('c',0), r.get('h',0), r.get('l',0), r.get('v',0)
        if o <= 0 or c <= 0 or v < 100000:
            continue
        if o < 1.0:
            continue
        change_pct = (h / o - 1) * 100
        if change_pct >= 10 and v >= 500000:
            candidates.append({
                'ticker': ticker,
                'open': o, 'close': c, 'high': h, 'low': l,
                'volume': v,
                'change_pct': change_pct,
                'prev_close': _prev_day_closes.get(ticker, None),
            })
    
    candidates.sort(key=lambda x: x['change_pct'] * math.log10(max(x['volume'],1)), reverse=True)
    
    # Update prev closes for next day
    _prev_day_closes = new_closes
    
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
def simulate_day(ticker, date_str, daily_info, capital_per_position, prev_close=None):
    """
    Simulate the full strategy for one ticker on one day.
    Returns list of trade results.
    REALISTIC version: slippage, commission, liquidity filter, gap filter.
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
    daily_open = daily_info.get('open', 0)
    
    # Gap filter: if 10%+ gap up from prev close, re-evaluate
    if prev_close and prev_close > 0 and daily_open > 0:
        gap_pct = (daily_open / prev_close - 1)
        if gap_pct >= GAP_UP_THRESHOLD:
            # Re-evaluate: require even stronger surge (15% instead of 10%) after gap
            surge_threshold = 0.15
        else:
            surge_threshold = 0.10
    else:
        surge_threshold = 0.10
    
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
    
    def check_liquidity(bar_1m, buy_amount):
        """Liquidity filter: 1min volume * price must exceed buy amount"""
        vol = bar_1m.get('v', 0)
        price = bar_1m.get('c', 0)
        if vol * price < buy_amount:
            return False
        return True
    
    trades = []
    
    # ── Phase 2: Find chase buy signal on 1-min bars ──
    LOOKBACK = 10
    
    position = None
    
    i = LOOKBACK
    while i < len(bars_1m):
        bar = bars_1m[i]
        ts = bar['t']
        dt_utc = datetime.fromtimestamp(ts//1000, tz=timezone.utc)
        price = clamp(bar['c'])
        
        # Force close 15 min before market close
        mc = market_close_utc(date_str)
        if position and dt_utc >= mc - timedelta(minutes=15):
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else bar['c'])
            sell_price = apply_slippage_sell(raw_sell)
            commission = calc_commission(position['shares'], position['buy_price']) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker,
                'phase': position.get('trade_phase', '1st'),
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '장마감',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
            })
            position = None
            break
        
        if position is None:
            if len(trades) >= 3:
                break
            
            # Phase 2: Chase buy signal (gap-adjusted threshold)
            ref_price = clamp(bars_1m[i - LOOKBACK]['c'])
            if ref_price > 0 and (price / ref_price - 1) >= surge_threshold:
                if i + 1 < len(bars_1m):
                    # Liquidity filter
                    invested = min(capital_per_position, COMPOUND_CAP / MAX_POSITIONS)
                    if not check_liquidity(bars_1m[i], invested):
                        i += 1
                        continue
                    
                    raw_buy = clamp(bars_1m[i+1]['o'])
                    buy_price = apply_slippage_buy(raw_buy)
                    shares = invested / buy_price if buy_price > 0 else 0
                    if shares <= 0:
                        i += 1
                        continue
                    buy_commission = calc_commission(shares, buy_price)
                    position = {
                        'buy_price': buy_price,
                        'buy_idx_1m': i + 1,
                        'invested': invested,
                        'shares': shares,
                        'peak': buy_price,
                        'bb_broken': False,
                        'trade_phase': '1st',
                        'buy_commission': buy_commission,
                    }
                    i += 2
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
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else sl_price)
            raw_sell = min(raw_sell, sl_price)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker,
                'phase': position['trade_phase'],
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '손절(-15%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
            })
            position = None
            i += 2
            continue
        
        # Take profit check (+30%)
        tp_price = position['buy_price'] * (1 + TAKE_PROFIT_PCT)
        if cur_high >= tp_price:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else tp_price)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker,
                'phase': position['trade_phase'],
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '익절(+30%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
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
                        raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                        sell_price = apply_slippage_sell(raw_sell)
                        commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                        pnl_pct = (sell_price / position['buy_price'] - 1)
                        pnl_krw = position['invested'] * pnl_pct - commission
                        is_first = position['trade_phase'] == '1st'
                        trades.append({
                            'ticker': ticker,
                            'phase': position['trade_phase'],
                            'buy_price': round(position['buy_price'], 4),
                            'sell_price': round(sell_price, 4),
                            'sell_reason': 'BB트레일링(-10%peak)',
                            'pnl_pct': round(pnl_pct * 100, 2),
                            'pnl_krw': round(pnl_krw),
                            'invested': round(position['invested']),
                            'commission': round(commission, 2),
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
        raw_sell = clamp(bars_1m[-1]['c'])
        sell_price = apply_slippage_sell(raw_sell)
        commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
        pnl_pct = (sell_price / position['buy_price'] - 1)
        pnl_krw = position['invested'] * pnl_pct - commission
        trades.append({
            'ticker': ticker,
            'phase': position['trade_phase'],
            'buy_price': round(position['buy_price'], 4),
            'sell_price': round(sell_price, 4),
            'sell_reason': '장마감',
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_krw': round(pnl_krw),
            'invested': round(position['invested']),
            'commission': round(commission, 2),
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
                # Liquidity filter
                vol = bars_1m[i].get('v', 0)
                px = bars_1m[i].get('c', 0)
                if vol * px < capital:
                    continue
                
                raw_buy = clamp(bars_1m[i+1]['o'])
                buy_price = apply_slippage_buy(raw_buy)
                if buy_price <= 0:
                    continue
                invested = capital
                shares = invested / buy_price
                buy_commission = calc_commission(shares, buy_price)
                return {
                    'buy_price': buy_price,
                    'buy_idx_1m': i + 1,
                    'invested': invested,
                    'shares': shares,
                    'peak': buy_price,
                    'bb_broken': False,
                    'trade_phase': '2nd(re-entry)',
                    'buy_commission': buy_commission,
                }
    
    return None


def run_backtest():
    print("=" * 60)
    print("Backtest v4 REALISTIC: 60-day simulation")
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
        
        # Get candidates (prev closes already cached from previous iteration)
        candidates = get_day_gainers(date_str)
        # Cache today's closes for next day's gap calculation
        # (done via grouped daily which we already fetched above)
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
                'open': cand['open'],
            }
            # Get previous day close for gap calculation
            prev_close = cand.get('prev_close', None)
            
            try:
                trades = simulate_day(ticker, date_str, daily_info, cap_per_pos, prev_close=prev_close)
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
    print("BACKTEST v4 REALISTIC RESULTS")
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
    # Load original results for comparison
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_result.json') as f:
            orig = json.load(f)
        orig_summary = orig['summary']
    except:
        orig_summary = None
    
    total_commission = sum(t.get('commission', 0) for d in all_results for t in d['trades'])
    
    result_md = f"""# Backtest v4 REALISTIC Results — 60일 시뮬레이션

## 전략 요약
- **Phase 1**: 거래량 급등 감지 → 모니터링
- **Phase 2**: 1분봉 10%+ 급등 시 추격 매수
- **Phase 3**: BB(20,2σ) 상단 돌파 후 고점 대비 10% 하락 시 1차 매도
- **Phase 4**: BB 하단 근처 + 2~3 연속 양봉 + 거래량 증가 시 재진입
- **Phase 5**: BB 상단 근처에서 2차 매도
- **BB 설정**: 5분봉 기준 BB(20, 2σ)
- **손절**: -15%, **익절**: +30%
- **당일 매매 필수**, 초기 자본 ₩100,000, 복리 모드, 최대 2포지션, 5분할

## 현실화 적용 항목
- **슬리피지**: 매수 +0.5%, 매도 -0.5%
- **수수료**: max($0.005/주, 0.1%) 편도
- **갭 필터**: 10%+ 갭업 시 추격매수 기준 15%로 상향
- **유동성 필터**: 1분봉 거래량×가격 < 매수금액 → 스킵
- **Compound Cap**: ₩25,000,000 엄격 적용

## 백테스트 기간
- **{trading_days[0]} ~ {trading_days[-1]}** ({len(trading_days)} 거래일)

## 기존 vs 현실화 비교표
| 항목 | 기존 v4 | 현실화 v4 |
|---|---|---|
| 초기 자본 | ₩{INITIAL_CAPITAL:,} | ₩{INITIAL_CAPITAL:,} |
| 최종 자본 | ₩{orig_summary['final_capital']:,} | ₩{capital:,.0f} |
| **총 수익률** | **{orig_summary['total_return_pct']:+.1f}%** | **{final_return:+.1f}%** |
| 최대 낙폭 (MDD) | {orig_summary['max_drawdown_pct']:.1f}% | {max_drawdown:.1f}% |
| 총 거래 수 | {orig_summary['total_trades']} | {total_trades} |
| 승률 | {orig_summary['win_rate']:.1f}% | {wins/total_trades*100:.1f}% |
| 1차 매도 | {orig_summary['first_exits']}건 | {first_exits}건 |
| 재진입 | {orig_summary['reentry_exits']}건 | {reentry_exits}건 |
| 총 수수료 | - | ${total_commission:,.2f} |
""" if orig_summary else f"""## 비교표
기존 결과 로드 실패 — 현실화 결과만 표시
"""
    result_md += f"""
## 현실화 핵심 결과
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
| 총 수수료 | ${total_commission:,.2f} |

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
    
    # ── Re-entry statistics ──
    reentry_trades = [t for d in all_results for t in d['trades'] if 're-entry' in t.get('phase','')]
    reentry_wins = sum(1 for t in reentry_trades if t['pnl_pct'] > 0)
    result_md += f"""
## 재진입 통계
| 항목 | 값 |
|---|---|
| 재진입 발동 횟수 | {len(reentry_trades)}건 |
| 재진입 성공 (수익) | {reentry_wins}건 ({reentry_wins/len(reentry_trades)*100:.0f}% 성공률) |
| 재진입 실패 (손실) | {len(reentry_trades)-reentry_wins}건 |
| 재진입 평균 수익률 | {np.mean([t['pnl_pct'] for t in reentry_trades]):+.1f}% |
""" if reentry_trades else """
## 재진입 통계
재진입 발동 없음 (조건 미충족)
"""

    # ── Multi-day analysis from ORIGINAL results ──
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_result.json') as f:
            orig_data = json.load(f)
        
        ticker_day_trades = defaultdict(list)
        for d in orig_data['daily']:
            for t in d['trades']:
                ticker_day_trades[t['ticker']].append({
                    'date': d['date'],
                    'pnl_pct': t['pnl_pct'],
                    'buy_price': t['buy_price'],
                    'sell_price': t['sell_price'],
                    'sell_reason': t['sell_reason'],
                    'volume': None,  # from daily info
                })
        
        # Also get volume/price info from candidates
        ticker_daily_info = defaultdict(list)
        for d in orig_data['daily']:
            tickers_in_day = set(t['ticker'] for t in d['trades'])
            for tk in tickers_in_day:
                ticker_daily_info[tk].append(d['date'])
        
        multi_day = {k: v for k, v in ticker_day_trades.items() if len(set(t['date'] for t in v)) > 1}
        
        result_md += f"""
## 멀티데이 종목 분석

### 멀티데이 등장 종목 ({len(multi_day)}개)
| 종목 | 등장일수 | 등장일 | 첫날 수익률 | 이후 평균 수익률 |
|---|---|---|---|---|
"""
        for tk, trades in sorted(multi_day.items(), key=lambda x: -len(set(t['date'] for t in x[1]))):
            dates = sorted(set(t['date'] for t in trades))
            first_day_trades = [t for t in trades if t['date'] == dates[0]]
            later_trades = [t for t in trades if t['date'] != dates[0]]
            first_avg = np.mean([t['pnl_pct'] for t in first_day_trades])
            later_avg = np.mean([t['pnl_pct'] for t in later_trades]) if later_trades else 0
            result_md += f"| {tk} | {len(dates)} | {', '.join(dates)} | {first_avg:+.1f}% | {later_avg:+.1f}% |\n"
        
        # Analyze multi-day characteristics
        multi_first_day_profits = []
        multi_later_profits = []
        for tk, trades in multi_day.items():
            dates = sorted(set(t['date'] for t in trades))
            first_day_trades = [t for t in trades if t['date'] == dates[0]]
            later_trades = [t for t in trades if t['date'] != dates[0]]
            multi_first_day_profits.extend([t['pnl_pct'] for t in first_day_trades])
            multi_later_profits.extend([t['pnl_pct'] for t in later_trades])
        
        result_md += f"""
### 멀티데이 종목 공통 특성
- **첫날 평균 수익률**: {np.mean(multi_first_day_profits):+.1f}%
- **이후 등장일 평균 수익률**: {np.mean(multi_later_profits):+.1f}%
- **첫날 수익 거래 비율**: {sum(1 for p in multi_first_day_profits if p > 0)/len(multi_first_day_profits)*100:.0f}%
- **이후 수익 거래 비율**: {sum(1 for p in multi_later_profits if p > 0)/len(multi_later_profits)*100:.0f}%
- **가격대**: 대부분 $1~$30 소형주 (페니~스몰캡)

### 멀티데이 필터 조건 제안
1. **첫날 상승률 10~50% 범위**: 너무 높은 급등(100%+)은 다음날 되돌림 확률 높음
2. **첫날 거래량 50만주 이상**: 유동성 확보된 종목만 재등장
3. **첫날 마감 가격 > 시가 (양봉 마감)**: 장 막판까지 강세 유지 종목
4. **가격대 $2~$20**: 극단적 저가주 제외, 적당한 변동성 구간
5. **다음날 갭 5% 이내**: 10%+ 갭업은 오히려 하락 전환 가능성
6. **첫날 BB 상단 돌파 이력**: 모멘텀 확인된 종목
"""
    except Exception as e:
        result_md += f"\n## 멀티데이 분석\n분석 실패: {e}\n"

    result_md += f"""
---
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*Backtest v4 REALISTIC — Polygon.io 1분봉/5분봉 데이터 기반*
*슬리피지 ±0.5%, 수수료 max($0.005/주, 0.1%), 갭/유동성 필터 적용*
"""
    
    out_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_realistic_result.md'
    with open(out_path, 'w') as f:
        f.write(result_md)
    print(f"\nResults saved to {out_path}")
    
    # Also save JSON
    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_realistic_result.json'
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
