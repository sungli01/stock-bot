#!/usr/bin/env python3
"""
Backtest v5.1: 계단식 트레일링 플로어
- min_price $0.7
- Stop loss: -50%
- 계단식 플로어: 30→60→120→300→400→500→600→700→800→900→1000%
- +300% 고정 익절 제거 → 계단식 플로어가 대체
- No re-entry
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
STOP_LOSS_PCT = -0.50       # -50% absolute stop
# No fixed take profit — staircase floor replaces it
FLOORS = [30, 60, 120, 300, 400, 500, 600, 700, 800, 900, 1000]  # % thresholds
BB_PERIOD = 20
BB_STD = 2
TOP_N_CANDIDATES = 7

# ── Realistic constraints ──
SLIPPAGE_BUY = 0.005
SLIPPAGE_SELL = 0.005
COMMISSION_PCT = 0.001
GAP_UP_THRESHOLD = 0.10

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
    """KST 05:45 = UTC 20:45"""
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return dt.replace(hour=20, minute=45)

def get_current_floor(peak_profit_pct):
    """계단식 플로어: peak_profit_pct(%)가 도달한 최고 플로어 반환"""
    current_floor = 0
    for f in FLOORS:
        if peak_profit_pct >= f:
            current_floor = f
        else:
            break
    return current_floor


def simulate_day(ticker, date_str, daily_info, capital_per_position, prev_close=None):
    bars_1m = get_bars(ticker, date_str, 1, 'minute')
    bars_5m = get_bars(ticker, date_str, 5, 'minute')

    if not bars_1m or len(bars_1m) < 20:
        return []
    if not bars_5m or len(bars_5m) < 5:
        return []

    daily_high = daily_info['high']
    daily_low = daily_info['low']
    daily_open = daily_info.get('open', 0)

    if prev_close and prev_close > 0 and daily_open > 0:
        gap_pct = (daily_open / prev_close - 1)
        surge_threshold = 0.15 if gap_pct >= GAP_UP_THRESHOLD else 0.10
    else:
        surge_threshold = 0.10

    max_1m_high = max(b['h'] for b in bars_1m)
    if daily_high > 0 and max_1m_high > daily_high * 3:
        return []

    bars_1m = [b for b in bars_1m if is_market_hours_utc(
        datetime.fromtimestamp(b['t']//1000, tz=timezone.utc))]
    bars_5m = [b for b in bars_5m if is_market_hours_utc(
        datetime.fromtimestamp(b['t']//1000, tz=timezone.utc))]

    if not bars_1m or not bars_5m:
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
    LOOKBACK = 10
    position = None
    mc = market_close_utc(date_str)
    fc = force_close_utc(date_str)
    close_time = min(mc - timedelta(minutes=15), fc)

    i = LOOKBACK
    while i < len(bars_1m):
        bar = bars_1m[i]
        ts = bar['t']
        dt_utc = datetime.fromtimestamp(ts//1000, tz=timezone.utc)
        price = clamp(bar['c'])

        # Force close at KST 05:45 or 15min before market close
        if position and dt_utc >= close_time:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else bar['c'])
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            peak_profit_pct = (position['peak'] / position['buy_price'] - 1) * 100
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            current_floor = get_current_floor(peak_profit_pct)
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '장마감(KST 05:45)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct, 2),
                'bb_broken': position.get('bb_broken', False),
                'floor_activated': current_floor,
                'floor_stages_hit': list(position.get('floor_stages_hit', [])),
            })
            position = None
            break

        if position is None:
            if len(trades) >= 1:
                break

            ref_price = clamp(bars_1m[i - LOOKBACK]['c'])
            if ref_price > 0 and (price / ref_price - 1) >= surge_threshold:
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
                        'buy_commission': buy_commission,
                        'floor_stages_hit': set(),
                    }
                    i += 2
                    continue
            i += 1
            continue

        # ── Have position: v5.1 staircase floor sell logic ──
        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = price

        if cur_high > position['peak']:
            position['peak'] = cur_high

        peak_profit_pct = (position['peak'] / position['buy_price'] - 1) * 100  # in %
        cur_profit_pct = (cur_close / position['buy_price'] - 1) * 100  # in %
        cur_profit_pct_low = (cur_low / position['buy_price'] - 1) * 100  # in %

        # Track which floor stages have been hit
        for f in FLOORS:
            if peak_profit_pct >= f:
                position['floor_stages_hit'].add(f)

        current_floor = get_current_floor(peak_profit_pct)

        # BB check (informational tracking)
        idx_5m = get_5m_bar_index_at(ts)
        if idx_5m >= 0:
            bb = compute_bb(closes_5m[:idx_5m+1])
            if bb:
                bb_upper, bb_mid, bb_lower = bb
                if cur_high > bb_upper:
                    position['bb_broken'] = True

        # === SELL LOGIC (v5.1 — 계단식 트레일링 플로어) ===

        # 1. Absolute stop loss: -50%
        if cur_profit_pct_low <= -50:
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
                'sell_reason': '손절(-50%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct, 2),
                'bb_broken': position.get('bb_broken', False),
                'floor_activated': current_floor,
                'floor_stages_hit': list(position.get('floor_stages_hit', [])),
            })
            position = None
            i += 2
            continue

        # 2. 계단식 플로어 보호: current_floor > 0 이고 현재 수익률이 플로어 밑으로 떨어지면 매도
        if current_floor > 0 and cur_profit_pct < current_floor:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': f'플로어보호({current_floor}%)',
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(position['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct, 2),
                'bb_broken': position.get('bb_broken', False),
                'floor_activated': current_floor,
                'floor_stages_hit': list(position.get('floor_stages_hit', [])),
            })
            position = None
            i += 2
            continue

        # 3. Otherwise: HOLD
        i += 1

    # Force close remaining at last bar
    if position:
        raw_sell = clamp(bars_1m[-1]['c'])
        sell_price = apply_slippage_sell(raw_sell)
        commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
        peak_profit_pct = (position['peak'] / position['buy_price'] - 1) * 100
        pnl_pct = (sell_price / position['buy_price'] - 1)
        pnl_krw = position['invested'] * pnl_pct - commission
        current_floor = get_current_floor(peak_profit_pct)
        trades.append({
            'ticker': ticker, 'phase': '1st',
            'buy_price': round(position['buy_price'], 4),
            'sell_price': round(sell_price, 4),
            'sell_reason': '장마감(KST 05:45)',
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_krw': round(pnl_krw),
            'invested': round(position['invested']),
            'commission': round(commission, 2),
            'peak_profit_pct': round(peak_profit_pct, 2),
            'bb_broken': position.get('bb_broken', False),
            'floor_activated': current_floor,
            'floor_stages_hit': list(position.get('floor_stages_hit', [])),
        })

    return trades


def run_backtest():
    print("=" * 60)
    print("Backtest v5.1: 계단식 트레일링 플로어")
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

    # Floor stage activation tracking
    floor_stage_counts = defaultdict(int)  # how many trades hit each floor stage

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
                    print(f"  {t['ticker']}: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,}) peak:{t['peak_profit_pct']:.1f}%")
                    # Track floor stages
                    for stage in t.get('floor_stages_hit', []):
                        floor_stage_counts[stage] += 1

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

    # 100%+ trades
    big_winners = []
    for d in all_results:
        for t in d['trades']:
            if t['peak_profit_pct'] >= 100:
                big_winners.append({'date': d['date'], **t})

    # 300%+ trades
    huge_winners = [t for t in big_winners if t['peak_profit_pct'] >= 300]

    bb_broken_trades = [t for d in all_results for t in d['trades'] if t.get('bb_broken')]

    wr = f"{wins/total_trades*100:.1f}" if total_trades else "0"

    print("\n" + "=" * 60)
    print("BACKTEST v5.1 RESULTS — 계단식 트레일링 플로어")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} trading days)")
    print(f"Initial: ₩{INITIAL_CAPITAL:,} → Final: ₩{capital:,.0f}")
    print(f"Return: {final_return:+.1f}% | MDD: {max_drawdown:.1f}%")
    print(f"Trades: {total_trades} (W:{wins} L:{losses}) WR:{wr}%" if total_trades else "No trades")
    print(f"100%+ peak 종목: {len(big_winners)}건 | 300%+ peak 종목: {len(huge_winners)}건")
    print(f"\n플로어 단계별 발동 횟수:")
    for f in FLOORS:
        cnt = floor_stage_counts.get(f, 0)
        print(f"  {f:>4}% 플로어: {cnt}건")

    # ── Load previous results for comparison ──
    prev_results = {}
    for name, path in [
        ('v4', 'backtest_v4_result.json'),
        ('v4_realistic', 'backtest_v4_realistic_result.json'),
        ('v4.1', 'backtest_v4_1_result.json'),
        ('v5', 'backtest_v5_result.json'),
    ]:
        try:
            with open(f'/home/ubuntu/.openclaw/workspace/stock-bot/{path}') as f:
                prev_results[name] = json.load(f)['summary']
        except:
            prev_results[name] = None

    # ── Generate report ──
    result_md = f"""# Backtest v5.1 Results — 계단식 트레일링 플로어

## 전략 변경사항 (v5 → v5.1)
| 항목 | v5 | v5.1 |
|---|---|---|
| 손절 | -50% | -50% (동일) |
| 익절 | +300% 고정 | **계단식 플로어 (제한 없음)** |
| 30% 플로어 | 단일 30% 플로어 | **30→60→120→300→…→1000% 계단식** |
| 재진입 | 없음 | 없음 (동일) |

## 계단식 플로어 로직
```
FLOORS = [30, 60, 120, 300, 400, 500, 600, 700, 800, 900, 1000]

peak까지 도달한 최고 플로어 계산 → 현재 수익이 그 밑으로 내려가면 매도
예: peak 150% → 플로어 120% → 현재 119%면 매도
예: peak 350% → 플로어 300% → 현재 280%면 매도 (v5는 300%에서 이미 익절됨)
```

## v4~v5.1 전체 비교표
| 항목 | v4 | v4_real | v4.1 | v5 | **v5.1** |
|---|---|---|---|---|---|"""

    def get_prev(name, key, fmt=None):
        s = prev_results.get(name)
        if not s:
            return "N/A"
        v = s.get(key, 'N/A')
        if fmt and v != 'N/A':
            return fmt.format(v)
        return str(v)

    rows = [
        ('초기 자본', [f"₩{INITIAL_CAPITAL:,}"] * 5),
        ('최종 자본', [get_prev(n, 'final_capital', '₩{:,}') for n in ['v4','v4_realistic','v4.1','v5']] + [f"**₩{capital:,.0f}**"]),
        ('총 수익률', [get_prev(n, 'total_return_pct', '{:+.1f}%') for n in ['v4','v4_realistic','v4.1','v5']] + [f"**{final_return:+.1f}%**"]),
        ('MDD', [get_prev(n, 'max_drawdown_pct', '{:.1f}%') for n in ['v4','v4_realistic','v4.1','v5']] + [f"**{max_drawdown:.1f}%**"]),
        ('총 거래', [get_prev(n, 'total_trades') for n in ['v4','v4_realistic','v4.1','v5']] + [f"**{total_trades}**"]),
        ('승률', [get_prev(n, 'win_rate', '{:.1f}%') for n in ['v4','v4_realistic','v4.1','v5']] + [f"**{wr}%**"]),
    ]

    for label, vals in rows:
        result_md += f"\n| {label} | " + " | ".join(vals) + " |"

    result_md += f"""

## v5.1 핵심 결과
| 항목 | 값 |
|---|---|
| 초기 자본 | ₩{INITIAL_CAPITAL:,} |
| 최종 자본 | ₩{capital:,.0f} |
| **총 수익률** | **{final_return:+.1f}%** |
| MDD | {max_drawdown:.1f}% |
| 총 거래 수 | {total_trades} |
| 승률 | {wr}% (W:{wins} L:{losses}) |
| 평균 수익 (승) | {avg_win:+.1f}% |
| 평균 손실 (패) | {avg_loss:+.1f}% |
| 수익 일수 | {plus_days}일 |
| 손실 일수 | {minus_days}일 |
| 무거래 일수 | {zero_days}일 |
| 총 수수료 | ₩{total_commission:,.0f} |

## 플로어 단계별 발동 횟수
| 플로어 단계 | 도달 거래 수 |
|---|---|
"""
    for f in FLOORS:
        cnt = floor_stage_counts.get(f, 0)
        result_md += f"| {f}% | {cnt}건 |\n"

    result_md += f"""
## 매도 사유 분포
| 매도 사유 | 건수 | 평균 수익률 | 총 수익금 |
|---|---|---|---|
"""
    for reason, stats in sorted(reason_counts.items(), key=lambda x: -x[1]['count']):
        avg_r = np.mean(stats['pnls']) if stats['pnls'] else 0
        result_md += f"| {reason} | {stats['count']}건 | {avg_r:+.1f}% | ₩{stats['total_pnl']:+,} |\n"

    # 100%+ winners
    result_md += f"""
## 🏆 100%+ 수익 종목 (peak 기준)
| 날짜 | 종목 | 매수가 | 최고수익률 | 실매도수익률 | 매도사유 |
|---|---|---|---|---|---|
"""
    for t in sorted(big_winners, key=lambda x: -x['peak_profit_pct']):
        result_md += f"| {t['date']} | {t['ticker']} | ${t['buy_price']:.2f} | {t['peak_profit_pct']:+.1f}% | {t['pnl_pct']:+.1f}% | {t['sell_reason']} |\n"

    # 300%+ detailed analysis
    if huge_winners:
        result_md += f"""
## 🔥 300%+ 도달 종목 상세 분석
"""
        for t in sorted(huge_winners, key=lambda x: -x['peak_profit_pct']):
            result_md += f"""
### {t['ticker']} ({t['date']})
- 매수가: ${t['buy_price']:.4f}
- 최고 수익률: {t['peak_profit_pct']:+.1f}%
- 실매도 수익률: {t['pnl_pct']:+.1f}%
- 매도 사유: {t['sell_reason']}
- 플로어 단계 도달: {sorted(t.get('floor_stages_hit', []))}
- BB돌파: {'✅' if t.get('bb_broken') else '❌'}
"""

    result_md += f"""
## 일별 상세
| 날짜 | 거래수 | 일 P&L | 누적 자본 |
|---|---|---|---|
"""
    for d in all_results:
        result_md += f"| {d['date']} | {len(d['trades'])} | ₩{d['day_pnl']:+,} | ₩{d['capital_after']:,} |\n"

    result_md += f"""
## 개별 거래 (상위 20건, |수익률| 순)
| 날짜 | 종목 | 매수가 | 매도가 | 사유 | 수익률 | peak | 플로어 |
|---|---|---|---|---|---|---|---|
"""
    all_trades_flat = [(d['date'], t) for d in all_results for t in d['trades']]
    all_trades_flat.sort(key=lambda x: abs(x[1]['pnl_pct']), reverse=True)
    for dt_s, t in all_trades_flat[:20]:
        result_md += f"| {dt_s} | {t['ticker']} | ${t['buy_price']:.2f} | ${t['sell_price']:.2f} | {t['sell_reason']} | {t['pnl_pct']:+.1f}% | {t['peak_profit_pct']:+.1f}% | {t['floor_activated']}% |\n"

    result_md += f"""
---
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*Backtest v5.1 — 계단식 트레일링 플로어, 손절-50%, 재진입 없음*
"""

    out_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v5_1_result.md'
    with open(out_path, 'w') as f:
        f.write(result_md)
    print(f"\nResults saved to {out_path}")

    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v5_1_result.json'
    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'initial_capital': INITIAL_CAPITAL,
                'min_price': MIN_PRICE,
                'stop_loss': STOP_LOSS_PCT,
                'floors': FLOORS,
                'bb_period': BB_PERIOD, 'bb_std': BB_STD,
                'max_positions': MAX_POSITIONS,
                'slippage_buy': SLIPPAGE_BUY, 'slippage_sell': SLIPPAGE_SELL,
                'commission_pct': COMMISSION_PCT,
                'compound_cap': COMPOUND_CAP,
                'reentry': False,
            },
            'summary': {
                'final_capital': round(capital),
                'total_return_pct': round(final_return, 2),
                'max_drawdown_pct': round(max_drawdown, 2),
                'total_trades': total_trades,
                'wins': wins, 'losses': losses,
                'win_rate': round(wins/total_trades*100, 1) if total_trades else 0,
                'avg_win_pct': round(float(avg_win), 2),
                'avg_loss_pct': round(float(avg_loss), 2),
                'total_commission': round(total_commission, 2),
                'bb_broken_trades': len(bb_broken_trades),
                'floor_stage_counts': {str(k): v for k, v in sorted(floor_stage_counts.items())},
                'big_winners_100pct': len(big_winners),
                'huge_winners_300pct': len(huge_winners),
            },
            'big_winners': big_winners,
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"JSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
