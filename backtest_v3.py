#!/usr/bin/env python3
"""
Backtest v3: Bollinger Band trailing stop + daily bar clamping + compound mode
Rules from 형님:
- If price breaks above BB upper: trailing stop at peak -10%
- If price doesn't break BB upper: sell at +35% (take profit)
- Stop loss: -15% (unchanged)
- Daily bar clamping: 1-min prices capped to daily H/L range
- Compound mode: cap ₩10,000,000
"""
import json, os, glob, math
from datetime import datetime, timezone, timedelta
import numpy as np

DATA_DIR = "data/backtest"

def load_all_trades():
    """Load all backtest day files and return sorted by date"""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))
    days = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
            days.append(data)
    return days

def get_daily_bar(ticker, date_str, api_key):
    """Fetch daily bar to get real H/L range"""
    import requests
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{date_str}/{date_str}?adjusted=true&apiKey={api_key}"
    r = requests.get(url, timeout=10)
    data = r.json()
    if data.get('results') and len(data['results']) > 0:
        b = data['results'][0]
        return b['h'], b['l']
    return None, None

def get_1min_bars(ticker, date_str, api_key):
    """Fetch 1-min bars for a ticker on a date"""
    import requests
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
    r = requests.get(url, timeout=15)
    data = r.json()
    return data.get('results', [])

def compute_bollinger(closes, period=20, num_std=2):
    """Compute Bollinger Band upper for the last value"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = np.mean(window)
    std = np.std(window)
    upper = sma + num_std * std
    return upper

def simulate_trade_with_bb(bars, buy_time_utc_str, buy_price, daily_high, daily_low, stop_loss_pct=-0.15, bb_trail_pct=-0.10, no_bb_tp_pct=0.35):
    """
    Simulate a single trade with BB trailing stop logic.
    - Clamp all prices to [daily_low, daily_high]
    - Track BB upper (20-period, 2std on closes)
    - If price breaks above BB upper → trailing stop (peak * (1 + bb_trail_pct))
    - If never breaks BB upper → sell at no_bb_tp_pct (+35%)
    - Stop loss at stop_loss_pct (-15%)
    Returns: (sell_price, sell_reason, sell_time_str)
    """
    if not bars:
        return buy_price, "no_data", ""
    
    # Parse buy time
    buy_h, buy_m = int(buy_time_utc_str.split(":")[0]), int(buy_time_utc_str.split(":")[1])
    
    # Filter bars after buy time, clamp prices
    trade_bars = []
    all_closes_before = []  # for BB computation pre-buy
    
    for b in bars:
        ts = b['t'] // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        
        # Clamp to daily range
        c = min(max(b['c'], daily_low), daily_high)
        h = min(max(b['h'], daily_low), daily_high)
        l = min(max(b['l'], daily_low), daily_high)
        
        if dt.hour < buy_h or (dt.hour == buy_h and dt.minute < buy_m):
            all_closes_before.append(c)
        else:
            trade_bars.append({'t': ts, 'c': c, 'h': h, 'l': l, 'dt': dt})
    
    if not trade_bars:
        return buy_price, "no_bars_after_buy", ""
    
    # Initialize BB with pre-buy closes
    closes_window = list(all_closes_before)
    
    peak_price = buy_price
    bb_broken = False
    trailing_active = False
    trailing_stop_price = 0
    
    tp_price = buy_price * (1 + no_bb_tp_pct)
    sl_price = buy_price * (1 + stop_loss_pct)
    
    for bar in trade_bars:
        c = bar['c']
        h = bar['h']
        l = bar['l']
        dt = bar['dt']
        
        closes_window.append(c)
        
        # Compute BB upper
        bb_upper = compute_bollinger(closes_window, period=20, num_std=2)
        
        # Check stop loss first (using low)
        if l <= sl_price:
            return sl_price, f"손절({stop_loss_pct*100:+.0f}%)", dt.strftime("%H:%M")
        
        # Track peak
        if h > peak_price:
            peak_price = h
        
        # Check if BB upper broken
        if bb_upper and h > bb_upper and not bb_broken:
            bb_broken = True
            trailing_active = True
        
        if trailing_active:
            # Trailing stop: peak * (1 + bb_trail_pct)
            trailing_stop_price = peak_price * (1 + bb_trail_pct)
            if l <= trailing_stop_price:
                pnl = (trailing_stop_price / buy_price - 1) * 100
                return trailing_stop_price, f"BB트레일링({pnl:+.1f}%)", dt.strftime("%H:%M")
        else:
            # No BB break yet - check +35% TP
            if h >= tp_price:
                pnl = (tp_price / buy_price - 1) * 100
                return tp_price, f"익절({pnl:+.1f}%)", dt.strftime("%H:%M")
    
    # End of day - force close at last bar close
    last_c = trade_bars[-1]['c']
    pnl = (last_c / buy_price - 1) * 100
    return last_c, f"장마감({pnl:+.1f}%)", trade_bars[-1]['dt'].strftime("%H:%M")


def run_backtest():
    import requests
    
    # Load API key
    with open('.env') as f:
        env = {}
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                env[k] = v
    api_key = env.get('POLYGON_API_KEY', '')
    
    days = load_all_trades()
    
    # Compound mode
    capital = 1_000_000  # ₩1M start
    cap = 10_000_000     # ₩10M cap
    
    total_pnl = 0
    all_results = []
    
    for day_data in days:
        date_str = day_data.get('date', '')
        trades = day_data.get('trades', [])
        if not trades:
            continue
        
        day_capital = min(capital, cap)
        day_pnl = 0
        day_trades = []
        
        for trade in trades:
            ticker = trade['ticker']
            alloc_pct = trade['allocation_pct'] / 100.0
            buy_price = trade['buy_price']
            buy_time = trade.get('buy_time_utc', trade.get('buy_time_kst', '10:00'))
            
            invested = day_capital * alloc_pct
            shares = int(invested / buy_price) if buy_price > 0 else 0
            if shares == 0:
                continue
            
            # Fetch daily bar for clamping
            daily_h, daily_l = get_daily_bar(ticker, date_str, api_key)
            if daily_h is None:
                # Fallback: use original trade result
                sell_price = trade['sell_price']
                sell_reason = trade.get('sell_reason', 'original')
                sell_time = trade.get('sell_time_kst', '')
            else:
                # Fetch 1-min bars
                bars = get_1min_bars(ticker, date_str, api_key)
                if not bars:
                    sell_price = min(max(trade['sell_price'], daily_l), daily_h)
                    sell_reason = trade.get('sell_reason', 'clamped')
                    sell_time = trade.get('sell_time_kst', '')
                else:
                    # Clamp buy price too
                    clamped_buy = min(max(buy_price, daily_l), daily_h)
                    sell_price, sell_reason, sell_time_utc = simulate_trade_with_bb(
                        bars, buy_time, clamped_buy, daily_h, daily_l
                    )
                    buy_price = clamped_buy
                    sell_time = sell_time_utc  # UTC for now
            
            actual_invested = shares * buy_price
            pnl = shares * (sell_price - buy_price)
            pnl_pct = (sell_price / buy_price - 1) * 100 if buy_price > 0 else 0
            
            day_trades.append({
                'ticker': ticker,
                'buy_price': round(buy_price, 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': sell_reason,
                'shares': shares,
                'invested': round(actual_invested),
                'pnl': round(pnl),
                'pnl_pct': round(pnl_pct, 1),
                'daily_range': f"{daily_l}-{daily_h}" if daily_h else "N/A"
            })
            day_pnl += pnl
        
        capital += day_pnl
        total_pnl += day_pnl
        
        all_results.append({
            'date': date_str,
            'trades': day_trades,
            'day_pnl': round(day_pnl),
            'capital_after': round(capital),
            'cumulative_pnl': round(total_pnl)
        })
        
        print(f"{date_str}: {len(day_trades)} trades, PnL={day_pnl:+,.0f}, Capital={capital:,.0f}")
    
    # Summary
    wins = sum(1 for d in all_results for t in d['trades'] if t['pnl'] > 0)
    losses = sum(1 for d in all_results for t in d['trades'] if t['pnl'] <= 0)
    total_trades = wins + losses
    plus_days = sum(1 for d in all_results if d['day_pnl'] > 0)
    minus_days = sum(1 for d in all_results if d['day_pnl'] <= 0 and d['trades'])
    
    summary = {
        'mode': 'BB_trailing + daily_clamp + compound_10M',
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins/total_trades*100, 1) if total_trades else 0,
        'plus_days': plus_days,
        'minus_days': minus_days,
        'daily_win_rate': round(plus_days/(plus_days+minus_days)*100, 1) if (plus_days+minus_days) else 0,
        'total_pnl': round(total_pnl),
        'final_capital': round(capital),
        'return_pct': round((capital/1_000_000 - 1)*100, 1),
        'daily_results': all_results
    }
    
    os.makedirs('data/backtest_v3', exist_ok=True)
    with open('data/backtest_v3/summary.json', 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    print(f"\n=== SUMMARY ===")
    print(f"Trades: {total_trades} ({wins}W/{losses}L, {summary['win_rate']}%)")
    print(f"Days: {plus_days}+/{minus_days}- ({summary['daily_win_rate']}%)")
    print(f"Total PnL: ₩{total_pnl:+,.0f}")
    print(f"Final Capital: ₩{capital:,.0f} ({summary['return_pct']:+.1f}%)")

if __name__ == '__main__':
    run_backtest()
