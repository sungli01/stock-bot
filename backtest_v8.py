#!/usr/bin/env python3
"""
Backtest v8: v6 기반 변경
매수: 거래량 200%+ / 가격 15%+ 급등
매도: 손절 -35%, BB돌파시 v6동일(35%마진/고점-15%트레일),
      BB미돌파시 30%플로어 제거→peak -15% 트레일링 통일,
      30분 내 +5% 미달 시 조기손절
"""
import os, sys, time, json, math
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
from pathlib import Path
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
CACHE_DIR = Path('/home/ubuntu/.openclaw/workspace/stock-bot/data/bars_cache')
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ──
INITIAL_CAPITAL = 100_000
COMPOUND_CAP = 25_000_000
MAX_POSITIONS = 2
MIN_PRICE = 0.7
STOP_LOSS_PCT = -0.35          # v8: -35% (v6: -50%)
BB_PERIOD = 20
BB_STD = 2
TOP_N_CANDIDATES = 7

# v8 buy conditions (strengthened from v6)
VOL_SURGE_PCT = 2.0            # v8: 200% (v6: 100%)
PRICE_SURGE_PCT = 0.15         # v8: 15% (v6: 10%)

# v8 sell conditions
TRAILING_FROM_PEAK = 0.15      # 최고가 대비 15% 하락 시 매도
MIN_MARGIN_SELL = 0.35         # 최소 마진 35%에서 매도 (우선)
PROFIT_ACTIVATE = 0.30         # 30%+ 상승 필요
EARLY_EXIT_MINUTES = 30        # v8: 30분 내
EARLY_EXIT_MIN_GAIN = 0.05    # v8: +5% 미달 시 조기 손절

# ── Realistic constraints ──
SLIPPAGE_BUY = 0.005
SLIPPAGE_SELL = 0.005
COMMISSION_PCT = 0.001

def apply_slippage_buy(price):
    return price * (1 + SLIPPAGE_BUY)

def apply_slippage_sell(price):
    return price * (1 - SLIPPAGE_SELL)

def calc_commission(shares, price):
    return shares * price * COMMISSION_PCT

_last_call = 0
def api_get(url, params=None):
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < 0.15:
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

def _cache_path(key):
    return CACHE_DIR / f"{key}.json"

def _load_cache(key):
    p = _cache_path(key)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None

def _save_cache(key, data):
    with open(_cache_path(key), 'w') as f:
        json.dump(data, f)

def get_trading_days(start, end):
    key = f"trading_days_{start}_{end}"
    cached = _load_cache(key)
    if cached:
        return cached
    url = f"{BASE}/v2/aggs/ticker/SPY/range/1/day/{start}/{end}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": "250"})
    days = []
    for bar in data.get('results', []):
        ts = bar['t'] // 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        days.append(dt.strftime('%Y-%m-%d'))
    _save_cache(key, days)
    return days

_prev_day_closes = {}

def get_day_gainers(date_str):
    global _prev_day_closes
    key = f"grouped_{date_str}"
    cached = _load_cache(key)
    if cached is None:
        url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
        data = api_get(url, {"adjusted": "true"})
        results = data.get('results', [])
        _save_cache(key, results)
    else:
        results = cached

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
        if o <= 0 or c <= 0 or v < 100000 or o < MIN_PRICE:
            continue
        change_pct = (h / o - 1) * 100
        if change_pct >= 10 and v >= 500000:
            candidates.append({
                'ticker': ticker,
                'open': o, 'close': c, 'high': h, 'low': l,
                'volume': v, 'change_pct': change_pct,
                'prev_close': _prev_day_closes.get(ticker, None),
            })

    candidates.sort(key=lambda x: x['change_pct'] * math.log10(max(x['volume'],1)), reverse=True)
    _prev_day_closes = new_closes
    return candidates[:TOP_N_CANDIDATES]

def get_bars(ticker, date_str, multiplier, timespan):
    key = f"{ticker}_{date_str}_{multiplier}{timespan[0]}"
    cached = _load_cache(key)
    if cached is not None:
        return cached
    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    results = data.get('results', [])
    _save_cache(key, results)
    return results

def compute_bb(closes, period=BB_PERIOD, num_std=BB_STD):
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = np.mean(window)
    std = np.std(window, ddof=0)
    return (sma + num_std * std, sma, sma - num_std * std)

def bar_to_utc(bar):
    return datetime.fromtimestamp(bar['t'] // 1000, tz=timezone.utc)

def is_market_hours_utc(dt_utc):
    month = dt_utc.month
    if 3 <= month <= 10:
        return (dt_utc.hour == 13 and dt_utc.minute >= 30) or (14 <= dt_utc.hour < 20)
    else:
        return (dt_utc.hour == 14 and dt_utc.minute >= 30) or (15 <= dt_utc.hour < 21)

def market_close_utc(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    month = dt.month
    if 3 <= month <= 10:
        return dt.replace(hour=20, minute=0)
    else:
        return dt.replace(hour=21, minute=0)

def force_close_utc(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return dt.replace(hour=20, minute=45)


def simulate_day(ticker, date_str, daily_info, capital_per_position, prev_close=None):
    bars_1m = get_bars(ticker, date_str, 1, 'minute')
    bars_5m = get_bars(ticker, date_str, 5, 'minute')

    if not bars_1m or len(bars_1m) < 20:
        return []
    if not bars_5m or len(bars_5m) < 5:
        return []

    daily_high = daily_info['high']
    daily_low = daily_info['low']

    max_1m_high = max(b['h'] for b in bars_1m)
    if daily_high > 0 and max_1m_high > daily_high * 3:
        return []

    def clamp(v):
        return min(max(v, daily_low), daily_high)

    closes_5m = [clamp(b['c']) for b in bars_5m]

    def get_5m_bar_index_at(ts_ms):
        for i in range(len(bars_5m)-1, -1, -1):
            if bars_5m[i]['t'] <= ts_ms:
                return i
        return -1

    def check_liquidity(bar_1m, buy_amount):
        vol = bar_1m.get('v', 0)
        price = bar_1m.get('c', 0)
        return vol * price >= buy_amount

    trades = []
    mc = market_close_utc(date_str)
    fc = force_close_utc(date_str)
    close_time = min(mc - timedelta(minutes=15), fc)

    position = None
    LOOKBACK = 10

    i = LOOKBACK
    while i < len(bars_1m):
        bar = bars_1m[i]
        ts = bar['t']
        dt_utc = bar_to_utc(bar)
        price = clamp(bar['c'])

        # Force close
        if position and dt_utc >= close_time:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else bar['c'])
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '장마감',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round((position['peak'] / position['buy_price'] - 1) * 100, 2),
                'bb_broken': position.get('bb_broken', False),
                'sell_algo_activated': position.get('sell_algo_activated', False),
            })
            position = None
            break

        if position is None:
            if len(trades) >= 1:
                break

            # ── v8 BUY CONDITION ──
            prev_vols = [bars_1m[j]['v'] for j in range(i - LOOKBACK, i)]
            avg_vol = np.mean(prev_vols) if prev_vols else 0

            vol_surge = (bar['v'] / avg_vol - 1) >= VOL_SURGE_PCT if avg_vol > 0 else False

            ref_price = clamp(bars_1m[i - LOOKBACK]['c'])
            price_surge = (price / ref_price - 1) >= PRICE_SURGE_PCT if ref_price > 0 else False

            if vol_surge and price_surge:
                if i + 1 < len(bars_1m):
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
                        'buy_price': buy_price, 'buy_idx_1m': i + 1,
                        'buy_time': dt_utc,
                        'invested': invested, 'shares': shares,
                        'peak': buy_price, 'bb_broken': False,
                        'sell_algo_activated': False,
                        'making_new_highs': False,
                        'buy_commission': buy_commission,
                        'vol_surge_pct': round((bar['v'] / avg_vol - 1) * 100, 1) if avg_vol > 0 else 0,
                        'price_surge_pct': round((price / ref_price - 1) * 100, 1) if ref_price > 0 else 0,
                    }
                    i += 2
                    continue
            i += 1
            continue

        # ── Have position: v8 SELL LOGIC ──
        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = price

        # Update peak
        prev_peak = position['peak']
        if cur_high > position['peak']:
            position['peak'] = cur_high
            position['making_new_highs'] = True

        peak_profit_pct = (position['peak'] / position['buy_price'] - 1)
        cur_profit_pct = (cur_close / position['buy_price'] - 1)
        cur_profit_pct_low = (cur_low / position['buy_price'] - 1)

        # BB check
        idx_5m = get_5m_bar_index_at(ts)
        if idx_5m >= 0:
            bb = compute_bb(closes_5m[:idx_5m+1])
            if bb:
                bb_upper, bb_mid, bb_lower = bb
                if cur_high > bb_upper:
                    position['bb_broken'] = True

        # Check if sell algorithm conditions met
        if (peak_profit_pct >= PROFIT_ACTIVATE and
            position['bb_broken'] and
            position['making_new_highs']):
            position['sell_algo_activated'] = True

        # === SELL LOGIC ===

        # 0. v8 조기 손절: 매수 후 30분 내 +5% 미달
        minutes_held = (dt_utc - position['buy_time']).total_seconds() / 60
        if minutes_held >= EARLY_EXIT_MINUTES and cur_profit_pct < EARLY_EXIT_MIN_GAIN and not position.get('early_exit_checked'):
            position['early_exit_checked'] = True
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '조기손절(30분+5%미달)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct * 100, 2),
                'bb_broken': position.get('bb_broken', False),
                'sell_algo_activated': position.get('sell_algo_activated', False),
            })
            position = None
            i += 2
            continue

        # 1. 절대 손절 -35%
        if cur_profit_pct_low <= STOP_LOSS_PCT:
            sl_target = position['buy_price'] * (1 + STOP_LOSS_PCT)
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else sl_target)
            raw_sell = min(raw_sell, sl_target)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '손절(-35%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct * 100, 2),
                'bb_broken': position.get('bb_broken', False),
                'sell_algo_activated': position.get('sell_algo_activated', False),
            })
            position = None
            i += 2
            continue

        # 2. v6 매도 알고리즘 (30%+상승 + BB돌파 + 최고가갱신 조건 충족 시)
        if position['sell_algo_activated']:
            drop_from_peak = (position['peak'] - cur_close) / position['peak'] if position['peak'] > 0 else 0

            # 35% 최소마진 우선
            if cur_profit_pct < MIN_MARGIN_SELL:
                raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                sell_price = apply_slippage_sell(raw_sell)
                commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                pnl_pct = (sell_price / position['buy_price'] - 1)
                pnl_krw = position['invested'] * pnl_pct - commission
                trades.append({
                    'ticker': ticker, 'phase': '1st',
                    'buy_price': round(position['buy_price'], 4),
                    'sell_price': round(sell_price, 4),
                    'sell_reason': '35%마진보호',
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'pnl_krw': round(pnl_krw),
                    'invested': round(position['invested']),
                    'commission': round(commission, 2),
                    'peak_profit_pct': round(peak_profit_pct * 100, 2),
                    'bb_broken': True,
                    'sell_algo_activated': True,
                })
                position = None
                i += 2
                continue

            # 최고가 대비 15% 하락 시 매도
            if drop_from_peak >= TRAILING_FROM_PEAK:
                raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                sell_price = apply_slippage_sell(raw_sell)
                commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                pnl_pct = (sell_price / position['buy_price'] - 1)
                pnl_krw = position['invested'] * pnl_pct - commission
                trades.append({
                    'ticker': ticker, 'phase': '1st',
                    'buy_price': round(position['buy_price'], 4),
                    'sell_price': round(sell_price, 4),
                    'sell_reason': '고점-15%트레일',
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'pnl_krw': round(pnl_krw),
                    'invested': round(position['invested']),
                    'commission': round(commission, 2),
                    'peak_profit_pct': round(peak_profit_pct * 100, 2),
                    'bb_broken': True,
                    'sell_algo_activated': True,
                })
                position = None
                i += 2
                continue

        # 3. v8: BB 미돌파 시 30% 플로어 제거 → peak 대비 -15% 트레일링 통일
        if not position['sell_algo_activated'] and peak_profit_pct > 0:
            drop_from_peak = (position['peak'] - cur_close) / position['peak'] if position['peak'] > 0 else 0
            if drop_from_peak >= TRAILING_FROM_PEAK:
                raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                sell_price = apply_slippage_sell(raw_sell)
                commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                pnl_pct = (sell_price / position['buy_price'] - 1)
                pnl_krw = position['invested'] * pnl_pct - commission
                trades.append({
                    'ticker': ticker, 'phase': '1st',
                    'buy_price': round(position['buy_price'], 4),
                    'sell_price': round(sell_price, 4),
                    'sell_reason': '트레일링-15%(BB미돌파)',
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'pnl_krw': round(pnl_krw),
                    'invested': round(position['invested']),
                    'commission': round(commission, 2),
                    'peak_profit_pct': round(peak_profit_pct * 100, 2),
                    'bb_broken': position.get('bb_broken', False),
                    'sell_algo_activated': False,
                })
                position = None
                i += 2
                continue

        # Reset new highs flag
        if cur_high <= prev_peak:
            position['making_new_highs'] = False

        i += 1

    # Force close remaining
    if position:
        raw_sell = clamp(bars_1m[-1]['c'])
        sell_price = apply_slippage_sell(raw_sell)
        commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
        peak_profit_pct = (position['peak'] / position['buy_price'] - 1)
        pnl_pct = (sell_price / position['buy_price'] - 1)
        pnl_krw = position['invested'] * pnl_pct - commission
        trades.append({
            'ticker': ticker, 'phase': '1st',
            'buy_price': round(position['buy_price'], 4),
            'sell_price': round(sell_price, 4),
            'sell_reason': '장마감',
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_krw': round(pnl_krw),
            'invested': round(position['invested']),
            'commission': round(commission, 2),
            'peak_profit_pct': round(peak_profit_pct * 100, 2),
            'bb_broken': position.get('bb_broken', False),
            'sell_algo_activated': position.get('sell_algo_activated', False),
        })

    return trades


def run_backtest():
    print("=" * 60)
    print("Backtest v8: 거래량200%/가격15%/손절35%/트레일링통일/조기손절")
    print("=" * 60)

    end_date = '2026-02-18'
    start_date = '2025-11-15'

    print(f"Fetching trading days {start_date} ~ {end_date}...")
    all_days = get_trading_days(start_date, end_date)
    trading_days = all_days[-60:] if len(all_days) >= 60 else all_days
    print(f"Got {len(trading_days)} trading days: {trading_days[0]} ~ {trading_days[-1]}")

    capital = INITIAL_CAPITAL
    all_results = []
    total_trades = 0
    wins = 0
    losses = 0

    for day_idx, date_str in enumerate(trading_days):
        print(f"\n[{day_idx+1}/{len(trading_days)}] {date_str} | Capital: ${capital:,.0f}")

        candidates = get_day_gainers(date_str)
        if not candidates:
            print("  No candidates found")
            all_results.append({'date': date_str, 'trades': [], 'day_pnl': 0, 'capital_after': round(capital)})
            continue

        print(f"  Candidates: {[c['ticker'] for c in candidates[:5]]}")
        cap_per_pos = min(capital / MAX_POSITIONS, COMPOUND_CAP / MAX_POSITIONS)
        day_trades = []
        positions_used = 0

        for cand in candidates:
            if positions_used >= MAX_POSITIONS:
                break
            ticker = cand['ticker']
            daily_info = {'high': cand['high'], 'low': cand['low'], 'open': cand['open']}
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
                    print(f"  {t['ticker']}: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (${t['pnl_krw']:+,})")

        day_pnl = sum(t['pnl_krw'] for t in day_trades)
        capital += day_pnl
        capital = max(capital, 10000)

        for t in day_trades:
            total_trades += 1
            if t['pnl_pct'] > 0:
                wins += 1
            else:
                losses += 1

        all_results.append({
            'date': date_str, 'trades': day_trades,
            'day_pnl': round(day_pnl), 'capital_after': round(capital),
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

    total_commission = sum(t.get('commission', 0) for d in all_results for t in d['trades'])

    # Sell reason stats
    reason_counts = defaultdict(lambda: {'count': 0, 'total_pnl': 0, 'pnls': []})
    for d in all_results:
        for t in d['trades']:
            r = reason_counts[t['sell_reason']]
            r['count'] += 1
            r['total_pnl'] += t['pnl_krw']
            r['pnls'].append(t['pnl_pct'])

    wr = f"{wins/total_trades*100:.1f}" if total_trades else "0"

    # Load previous versions
    prev_versions = {}
    for v in ['v5', 'v6', 'v7']:
        try:
            with open(f'/home/ubuntu/.openclaw/workspace/stock-bot/backtest_{v}_result.json') as f:
                prev_versions[v] = json.load(f)['summary']
        except:
            pass

    print("\n" + "=" * 60)
    print("BACKTEST v8 RESULTS")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} days)")
    print(f"Initial: ${INITIAL_CAPITAL:,} → Final: ${capital:,.0f}")
    print(f"Return: {final_return:+.1f}% | MDD: {max_drawdown:.1f}%")
    print(f"Trades: {total_trades} (W:{wins} L:{losses}) WR:{wr}%")
    print(f"Avg Win: {avg_win:+.1f}% | Avg Loss: {avg_loss:+.1f}%")
    print(f"Days: +{plus_days} / -{minus_days} / 0:{zero_days}")
    print(f"Commission: ${total_commission:,.0f}")

    print(f"\n{'='*60}")
    print("VERSION COMPARISON")
    print(f"{'='*60}")
    print(f"{'Version':<8} {'Final($)':>12} {'Return%':>10} {'WR%':>8} {'Trades':>8} {'MDD%':>8}")
    print("-" * 56)
    for v, s in sorted(prev_versions.items()):
        print(f"{v:<8} {s['final_capital']:>12,} {s['total_return_pct']:>+10.1f} {s['win_rate']:>8.1f} {s['total_trades']:>8} {s['max_drawdown_pct']:>8.1f}")
    print(f"{'v8':<8} {round(capital):>12,} {final_return:>+10.1f} {float(wr):>8.1f} {total_trades:>8} {max_drawdown:>8.1f}")

    print("\n매도 사유 분포:")
    for reason, stats in sorted(reason_counts.items(), key=lambda x: -x[1]['count']):
        avg_r = np.mean(stats['pnls']) if stats['pnls'] else 0
        print(f"  {reason}: {stats['count']}건, 평균 {avg_r:+.1f}%, 총 ${stats['total_pnl']:+,.0f}")

    # Save JSON
    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v8_result.json'
    reason_summary = {}
    for reason, stats in reason_counts.items():
        reason_summary[reason] = {
            'count': stats['count'],
            'total_pnl': round(stats['total_pnl']),
            'avg_pnl_pct': round(np.mean(stats['pnls']), 2) if stats['pnls'] else 0,
        }

    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'version': 'v8',
                'initial_capital': INITIAL_CAPITAL,
                'min_price': MIN_PRICE,
                'stop_loss': STOP_LOSS_PCT,
                'vol_surge_pct': VOL_SURGE_PCT,
                'price_surge_pct': PRICE_SURGE_PCT,
                'trailing_from_peak': TRAILING_FROM_PEAK,
                'min_margin_sell': MIN_MARGIN_SELL,
                'profit_activate': PROFIT_ACTIVATE,
                'early_exit_minutes': EARLY_EXIT_MINUTES,
                'early_exit_min_gain': EARLY_EXIT_MIN_GAIN,
                'bb_period': BB_PERIOD, 'bb_std': BB_STD,
                'max_positions': MAX_POSITIONS,
                'slippage_buy': SLIPPAGE_BUY, 'slippage_sell': SLIPPAGE_SELL,
                'commission_pct': COMMISSION_PCT,
                'compound_cap': COMPOUND_CAP,
            },
            'summary': {
                'final_capital': round(capital),
                'total_return_pct': round(final_return, 2),
                'max_drawdown_pct': round(max_drawdown, 2),
                'total_trades': total_trades,
                'wins': wins, 'losses': losses,
                'win_rate': round(wins/total_trades*100, 1) if total_trades else 0,
                'avg_win_pct': round(avg_win, 2),
                'avg_loss_pct': round(avg_loss, 2),
                'total_commission': round(total_commission, 2),
                'plus_days': plus_days, 'minus_days': minus_days, 'zero_days': zero_days,
            },
            'sell_reason_summary': reason_summary,
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nJSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
