#!/usr/bin/env python3
"""
Backtest v7: 엄격한 매수 필터 + 통일 트레일링
매수: 1분봉 거래량 200%+ AND 가격변동 30%+
매도: 30%+ 상승 AND BB상단 돌파 AND 최고가 갱신
  → 최고가 대비 -15% 하락 OR 최소마진 35% (35% 우선)
  BB 미돌파 시에도 peak 대비 -15% 트레일링 통일
손절: -15%
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
STOP_LOSS_PCT = -0.15       # v7: -15% (v6은 -50%)
BB_PERIOD = 20
BB_STD = 2
TOP_N_CANDIDATES = 7

# v7 specific
VOL_SURGE_PCT = 2.0         # 거래량 200%+ (v6은 100%)
PRICE_SURGE_PCT = 0.30      # 30%+ 가격 급등 (v6은 10%)
TRAILING_FROM_PEAK = 0.15   # 최고가 대비 15% 하락 시 매도
MIN_MARGIN_SELL = 0.35      # 최소 마진 35%
PROFIT_ACTIVATE = 0.30      # 30%+ 상승 필요

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

            # ── v7 BUY CONDITION: 거래량 200%+ AND 가격 30%+ ──
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
                        'invested': invested, 'shares': shares,
                        'peak': buy_price, 'bb_broken': False,
                        'sell_algo_activated': False,
                        'making_new_highs': False,
                        'buy_commission': buy_commission,
                    }
                    i += 2
                    continue
            i += 1
            continue

        # ── Have position: v7 SELL LOGIC ──
        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = price

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

        # Check sell algo conditions
        if (peak_profit_pct >= PROFIT_ACTIVATE and
            position['bb_broken'] and
            position['making_new_highs']):
            position['sell_algo_activated'] = True

        # === SELL LOGIC ===

        # 1. 손절 -15%
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
                'sell_reason': '손절(-15%)',
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

        # 2. 매도 알고리즘 충족 시 (30%+상승 + BB돌파 + 최고가갱신)
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

            # 최고가 대비 15% 하락
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

        # 3. v7: BB 미돌파 시에도 peak 대비 -15% 트레일링 (30% 플로어 대신)
        if not position['sell_algo_activated'] and peak_profit_pct >= PROFIT_ACTIVATE:
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
                    'sell_reason': '고점-15%트레일(BB미돌파)',
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
    print("Backtest v7: 거래량200%+/가격30%+ 매수 + 손절-15% + 통일트레일링")
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
        print(f"\n[{day_idx+1}/{len(trading_days)}] {date_str} | Capital: ₩{capital:,.0f}")

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
                    print(f"  {t['ticker']}: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,})")

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

    reason_counts = defaultdict(lambda: {'count': 0, 'total_pnl': 0, 'pnls': []})
    for d in all_results:
        for t in d['trades']:
            r = reason_counts[t['sell_reason']]
            r['count'] += 1
            r['total_pnl'] += t['pnl_krw']
            r['pnls'].append(t['pnl_pct'])

    wr = f"{wins/total_trades*100:.1f}" if total_trades else "0"

    # Load v5, v6 for comparison
    v5_summary = v6_summary = None
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v5_result.json') as f:
            v5_summary = json.load(f)['summary']
    except: pass
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v6_result.json') as f:
            v6_summary = json.load(f)['summary']
    except: pass

    print("\n" + "=" * 60)
    print("BACKTEST v7 RESULTS")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} days)")
    print(f"Initial: ₩{INITIAL_CAPITAL:,} → Final: ₩{capital:,.0f}")
    print(f"Return: {final_return:+.1f}% | MDD: {max_drawdown:.1f}%")
    print(f"Trades: {total_trades} (W:{wins} L:{losses}) WR:{wr}%")
    print(f"Avg Win: {avg_win:+.1f}% | Avg Loss: {avg_loss:+.1f}%")
    print(f"Days: +{plus_days} / -{minus_days} / 0:{zero_days}")
    print(f"Commission: ₩{total_commission:,.0f}")

    print(f"\n{'='*60}")
    print(f"{'지표':<20} {'v5':>12} {'v6':>12} {'v7':>12}")
    print(f"{'='*60}")
    v5f = v5_summary['final_capital'] if v5_summary else 0
    v6f = v6_summary['final_capital'] if v6_summary else 0
    v5r = v5_summary['total_return_pct'] if v5_summary else 0
    v6r = v6_summary['total_return_pct'] if v6_summary else 0
    v5wr = v5_summary['win_rate'] if v5_summary else 0
    v6wr = v6_summary['win_rate'] if v6_summary else 0
    v5t = v5_summary['total_trades'] if v5_summary else 0
    v6t = v6_summary['total_trades'] if v6_summary else 0
    v5mdd = v5_summary['max_drawdown_pct'] if v5_summary else 0
    v6mdd = v6_summary['max_drawdown_pct'] if v6_summary else 0
    print(f"{'최종자본':<20} {'₩'+f'{v5f:,}':>12} {'₩'+f'{v6f:,}':>12} {'₩'+f'{round(capital):,}':>12}")
    print(f"{'수익률':<20} {f'{v5r:+.1f}%':>12} {f'{v6r:+.1f}%':>12} {f'{final_return:+.1f}%':>12}")
    print(f"{'승률':<20} {f'{v5wr:.1f}%':>12} {f'{v6wr:.1f}%':>12} {f'{wr}%':>12}")
    print(f"{'거래수':<20} {v5t:>12} {v6t:>12} {total_trades:>12}")
    print(f"{'MDD':<20} {f'{v5mdd:.1f}%':>12} {f'{v6mdd:.1f}%':>12} {f'{max_drawdown:.1f}%':>12}")
    print(f"{'='*60}")

    print("\n매도 사유 분포:")
    for reason, stats in sorted(reason_counts.items(), key=lambda x: -x[1]['count']):
        avg_r = np.mean(stats['pnls']) if stats['pnls'] else 0
        print(f"  {reason}: {stats['count']}건, 평균 {avg_r:+.1f}%, 총 ₩{stats['total_pnl']:+,}")

    # Save JSON
    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v7_result.json'
    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'initial_capital': INITIAL_CAPITAL,
                'min_price': MIN_PRICE,
                'stop_loss': STOP_LOSS_PCT,
                'vol_surge_pct': VOL_SURGE_PCT,
                'price_surge_pct': PRICE_SURGE_PCT,
                'trailing_from_peak': TRAILING_FROM_PEAK,
                'min_margin_sell': MIN_MARGIN_SELL,
                'profit_activate': PROFIT_ACTIVATE,
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
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nJSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
