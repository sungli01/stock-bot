#!/usr/bin/env python3
"""
Backtest v4.1: Strategy fix + Data caching
Key changes from v4_realistic:
1. Re-entry (Phase 4) removes liquidity/volume filter — pure price pattern only
2. All bar data cached to data/bars_cache/ for instant re-runs
3. Grouped daily data also cached
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
TAKE_PROFIT_PCT = 0.30
STOP_LOSS_PCT = -0.15
BB_PERIOD = 20
BB_STD = 2
PEAK_DROP_PCT = 0.10
TOP_N_CANDIDATES = 7

# ── Realistic constraints ──
SLIPPAGE_BUY = 0.005
SLIPPAGE_SELL = 0.005
COMMISSION_PCT = 0.001  # 0.1% per side (KRW basis)
GAP_UP_THRESHOLD = 0.10

def apply_slippage_buy(price):
    return price * (1 + SLIPPAGE_BUY)

def apply_slippage_sell(price):
    return price * (1 - SLIPPAGE_SELL)

def calc_commission(shares, price):
    """Commission: 0.1% of trade value"""
    return shares * price * COMMISSION_PCT

# Rate limit
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

# ── Cached data fetching ──
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
        if o <= 0 or c <= 0 or v < 100000 or o < 1.0:
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

# ── Bollinger Band ──
def compute_bb(closes, period=BB_PERIOD, num_std=BB_STD):
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = np.mean(window)
    std = np.std(window, ddof=0)
    return (sma + num_std * std, sma, sma - num_std * std)

# ── Market hours ──
def is_market_hours_utc(dt_utc):
    month = dt_utc.month
    if 3 <= month <= 10:  # EDT
        return (dt_utc.hour == 13 and dt_utc.minute >= 30) or (14 <= dt_utc.hour < 20)
    else:  # EST
        return (dt_utc.hour == 14 and dt_utc.minute >= 30) or (15 <= dt_utc.hour < 21)

def market_close_utc(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    month = dt.month
    if 3 <= month <= 10:
        return dt.replace(hour=20, minute=0)
    else:
        return dt.replace(hour=21, minute=0)

# ── Strategy ──
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

    # Gap filter
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
        """Liquidity filter: only for first entry"""
        vol = bar_1m.get('v', 0)
        price = bar_1m.get('c', 0)
        return vol * price >= buy_amount

    trades = []
    LOOKBACK = 10
    position = None
    i = LOOKBACK

    while i < len(bars_1m):
        bar = bars_1m[i]
        ts = bar['t']
        dt_utc = datetime.fromtimestamp(ts//1000, tz=timezone.utc)
        price = clamp(bar['c'])
        mc = market_close_utc(date_str)

        # Force close 15 min before close
        if position and dt_utc >= mc - timedelta(minutes=15):
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else bar['c'])
            sell_price = apply_slippage_sell(raw_sell)
            commission = calc_commission(position['shares'], position['buy_price']) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': position.get('trade_phase', '1st'),
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

            # Phase 2: Chase buy
            ref_price = clamp(bars_1m[i - LOOKBACK]['c'])
            if ref_price > 0 and (price / ref_price - 1) >= surge_threshold:
                if i + 1 < len(bars_1m):
                    invested = min(capital_per_position, COMPOUND_CAP / MAX_POSITIONS)
                    # Liquidity filter: ONLY for first entry
                    if len(trades) == 0 and not check_liquidity(bars_1m[i], invested):
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
                        'trade_phase': '1st', 'buy_commission': buy_commission,
                    }
                    i += 2
                    continue
            i += 1
            continue

        # ── Have position ──
        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = price

        if cur_high > position['peak']:
            position['peak'] = cur_high

        # Stop loss
        sl_price = position['buy_price'] * (1 + STOP_LOSS_PCT)
        if cur_low <= sl_price:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else sl_price)
            raw_sell = min(raw_sell, sl_price)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': position['trade_phase'],
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

        # Take profit
        tp_price = position['buy_price'] * (1 + TAKE_PROFIT_PCT)
        if cur_high >= tp_price:
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else tp_price)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': position['trade_phase'],
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

        # BB check
        idx_5m = get_5m_bar_index_at(ts)
        if idx_5m >= 0:
            bb = compute_bb(closes_5m[:idx_5m+1])
            if bb:
                bb_upper, bb_mid, bb_lower = bb
                if cur_high > bb_upper:
                    position['bb_broken'] = True

                # Phase 3/5: BB trailing sell
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
                            'ticker': ticker, 'phase': position['trade_phase'],
                            'buy_price': round(position['buy_price'], 4),
                            'sell_price': round(sell_price, 4),
                            'sell_reason': 'BB트레일링(-10%peak)',
                            'pnl_pct': round(pnl_pct * 100, 2),
                            'pnl_krw': round(pnl_krw),
                            'invested': round(position['invested']),
                            'commission': round(commission, 2),
                        })

                        # Phase 4: Re-entry after 1st trade (NO volume filter!)
                        if is_first:
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

    # Force close remaining
    if position:
        raw_sell = clamp(bars_1m[-1]['c'])
        sell_price = apply_slippage_sell(raw_sell)
        commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
        pnl_pct = (sell_price / position['buy_price'] - 1)
        pnl_krw = position['invested'] * pnl_pct - commission
        trades.append({
            'ticker': ticker, 'phase': position['trade_phase'],
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
    Phase 4: Re-entry — pure price pattern, NO volume/liquidity filter
    Look for price near BB lower + 2-3 consecutive green candles
    """
    def clamp(v):
        return min(max(v, daily_low), daily_high)

    mc = market_close_utc(date_str)

    if start_idx + 30 >= len(bars_1m):
        return None

    def get_5m_idx(ts_ms):
        for j in range(len(bars_5m)-1, -1, -1):
            if bars_5m[j]['t'] <= ts_ms:
                return j
        return -1

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

        if bb_lower <= 0:
            continue
        dist_to_lower = (price - bb_lower) / bb_lower
        if dist_to_lower > 0.02 or dist_to_lower < -0.05:
            continue

        # Check: 2-3 consecutive green candles (price pattern only, NO volume check)
        greens = 0
        for k in range(max(0, i-2), i+1):
            b = bars_1m[k]
            if clamp(b['c']) > clamp(b['o']):
                greens += 1

        if greens >= 2:
            if i + 1 < len(bars_1m):
                # NO liquidity filter for re-entry!
                raw_buy = clamp(bars_1m[i+1]['o'])
                buy_price = apply_slippage_buy(raw_buy)
                if buy_price <= 0:
                    continue
                invested = min(capital, COMPOUND_CAP / MAX_POSITIONS)
                shares = invested / buy_price
                buy_commission = calc_commission(shares, buy_price)
                return {
                    'buy_price': buy_price, 'buy_idx_1m': i + 1,
                    'invested': invested, 'shares': shares,
                    'peak': buy_price, 'bb_broken': False,
                    'trade_phase': '2nd(re-entry)',
                    'buy_commission': buy_commission,
                }

    return None


def run_backtest():
    print("=" * 60)
    print("Backtest v4.1: Strategy fix + Data caching")
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
    first_exits = 0
    reentry_exits = 0
    reentry_trades_all = []

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
                    print(f"  {t['ticker']} [{t['phase']}]: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,})")

        day_pnl = sum(t['pnl_krw'] for t in day_trades)
        capital += day_pnl
        capital = max(capital, 10000)

        for t in day_trades:
            total_trades += 1
            if t['pnl_pct'] > 0:
                wins += 1
            else:
                losses += 1
            if '1st' in t.get('phase', ''):
                first_exits += 1
            if 're-entry' in t.get('phase', ''):
                reentry_exits += 1
                reentry_trades_all.append(t)

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

    # Re-entry stats
    reentry_wins = sum(1 for t in reentry_trades_all if t['pnl_pct'] > 0)
    reentry_pnl = sum(t['pnl_krw'] for t in reentry_trades_all)
    total_pnl = sum(t['pnl_krw'] for d in all_results for t in d['trades'])
    reentry_contrib_pct = (reentry_pnl / total_pnl * 100) if total_pnl != 0 else 0

    print("\n" + "=" * 60)
    print("BACKTEST v4.1 RESULTS")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} trading days)")
    print(f"Initial: ₩{INITIAL_CAPITAL:,} → Final: ₩{capital:,.0f}")
    print(f"Return: {final_return:+.1f}% | MDD: {max_drawdown:.1f}%")
    print(f"Trades: {total_trades} (W:{wins} L:{losses}) WR:{wins/total_trades*100:.1f}%" if total_trades else "No trades")
    print(f"Re-entry: {len(reentry_trades_all)}건 (W:{reentry_wins}) 수익기여: ₩{reentry_pnl:+,} ({reentry_contrib_pct:+.1f}%)")

    # Load previous results for comparison
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_result.json') as f:
            v4_summary = json.load(f)['summary']
    except:
        v4_summary = None
    try:
        with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_realistic_result.json') as f:
            v4r_summary = json.load(f)['summary']
    except:
        v4r_summary = None

    # ── Generate report ──
    wr = f"{wins/total_trades*100:.1f}" if total_trades else "0"

    result_md = f"""# Backtest v4.1 Results — 전략 수정 + 데이터 캐싱

## 핵심 변경사항 (v4_realistic → v4.1)
1. **재진입 시 유동성/거래량 필터 제거** — 순수 가격 패턴만으로 재진입 판단
2. **데이터 캐싱** — `data/bars_cache/`에 모든 API 응답 캐시, 재실행 시 수초 완료
3. **수수료 통일** — 0.1% 편도 (KRW 기준)

## 전략 Phase 정리
| Phase | 설명 | 거래량 사용 |
|---|---|---|
| 1 (감시) | 거래량 급등 감지 → 주목 | ✅ |
| 2 (추격매수) | 1분봉 10%+ 급등 → 매수 | ❌ 가격만 |
| 3 (1차매도) | BB상단 돌파 후 고점-10% → 매도 | ❌ 가격만 |
| **4 (재진입)** | **BB하단 근처 + 연속양봉 → 재매수** | **❌ 거래량 조건 없음!** |
| 5 (2차매도) | BB상단 돌파/이탈 → 매도 | ❌ 가격만 |

## 3버전 비교표
| 항목 | v4 (이상적) | v4_realistic | **v4.1** |
|---|---|---|---|
| 초기 자본 | ₩{INITIAL_CAPITAL:,} | ₩{INITIAL_CAPITAL:,} | ₩{INITIAL_CAPITAL:,} |
| 최종 자본 | ₩{v4_summary['final_capital']:,} | ₩{v4r_summary['final_capital']:,} | **₩{capital:,.0f}** |
| 총 수익률 | {v4_summary['total_return_pct']:+.1f}% | {v4r_summary['total_return_pct']:+.1f}% | **{final_return:+.1f}%** |
| MDD | {v4_summary['max_drawdown_pct']:.1f}% | {v4r_summary['max_drawdown_pct']:.1f}% | **{max_drawdown:.1f}%** |
| 총 거래 | {v4_summary['total_trades']} | {v4r_summary['total_trades']} | **{total_trades}** |
| 승률 | {v4_summary['win_rate']:.1f}% | {v4r_summary['win_rate']:.1f}% | **{wr}%** |
| 1차 매도 | {v4_summary['first_exits']}건 | {v4r_summary['first_exits']}건 | **{first_exits}건** |
| 재진입 | {v4_summary['reentry_exits']}건 | {v4r_summary['reentry_exits']}건 | **{reentry_exits}건** |
| 수수료 | - | 적용 | **적용 (0.1%/편도)** |
| 슬리피지 | - | ±0.5% | **±0.5%** |
""" if v4_summary and v4r_summary else ""

    result_md += f"""
## v4.1 핵심 결과
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
| 총 수수료 | ${total_commission:,.2f} |

## 재진입 통계
| 항목 | 값 |
|---|---|
| 재진입 발동 횟수 | {len(reentry_trades_all)}건 |
| 재진입 성공 (수익) | {reentry_wins}건 ({reentry_wins/len(reentry_trades_all)*100:.0f}% 성공률) |
| 재진입 실패 (손실) | {len(reentry_trades_all)-reentry_wins}건 |
| 재진입 평균 수익률 | {np.mean([t['pnl_pct'] for t in reentry_trades_all]):+.1f}% |
| 재진입 총 수익 | ₩{reentry_pnl:+,} |
| 전체 수익 대비 기여도 | {reentry_contrib_pct:+.1f}% |
""" if reentry_trades_all else f"""
## 재진입 통계
| 항목 | 값 |
|---|---|
| 재진입 발동 횟수 | 0건 |
| 비고 | BB하단 근처 + 연속양봉 조건 미충족 |
"""

    # Re-entry detail
    if reentry_trades_all:
        result_md += "\n### 재진입 개별 내역\n| 종목 | 매수가 | 매도가 | 사유 | 수익률 | 수익금 |\n|---|---|---|---|---|---|\n"
        for t in reentry_trades_all:
            result_md += f"| {t['ticker']} | ${t['buy_price']:.2f} | ${t['sell_price']:.2f} | {t['sell_reason']} | {t['pnl_pct']:+.1f}% | ₩{t['pnl_krw']:+,} |\n"

    result_md += f"""
## 일별 상세
| 날짜 | 거래수 | 일 P&L | 누적 자본 |
|---|---|---|---|
"""
    for d in all_results:
        result_md += f"| {d['date']} | {len(d['trades'])} | ₩{d['day_pnl']:+,} | ₩{d['capital_after']:,} |\n"

    # Top trades
    result_md += f"""
## 개별 거래 (상위 20건, |수익률| 순)
| 날짜 | 종목 | 구분 | 매수가 | 매도가 | 사유 | 수익률 |
|---|---|---|---|---|---|---|
"""
    all_trades_flat = [(d['date'], t) for d in all_results for t in d['trades']]
    all_trades_flat.sort(key=lambda x: abs(x[1]['pnl_pct']), reverse=True)
    for dt_s, t in all_trades_flat[:20]:
        result_md += f"| {dt_s} | {t['ticker']} | {t['phase']} | ${t['buy_price']:.2f} | ${t['sell_price']:.2f} | {t['sell_reason']} | {t['pnl_pct']:+.1f}% |\n"

    # Sell reason distribution
    result_md += "\n## 매도 사유 분포\n"
    reason_counts = defaultdict(int)
    for d in all_results:
        for t in d['trades']:
            reason_counts[t['sell_reason']] += 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        result_md += f"- **{reason}**: {count}건\n"

    result_md += f"""
---
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
*Backtest v4.1 — 재진입 거래량 필터 제거, 데이터 캐싱 적용*
"""

    out_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_1_result.md'
    with open(out_path, 'w') as f:
        f.write(result_md)
    print(f"\nResults saved to {out_path}")

    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v4_1_result.json'
    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'initial_capital': INITIAL_CAPITAL,
                'stop_loss': STOP_LOSS_PCT,
                'take_profit': TAKE_PROFIT_PCT,
                'bb_period': BB_PERIOD, 'bb_std': BB_STD,
                'max_positions': MAX_POSITIONS,
                'slippage_buy': SLIPPAGE_BUY, 'slippage_sell': SLIPPAGE_SELL,
                'commission_pct': COMMISSION_PCT,
                'compound_cap': COMPOUND_CAP,
                'reentry_volume_filter': False,
            },
            'summary': {
                'final_capital': round(capital),
                'total_return_pct': round(final_return, 2),
                'max_drawdown_pct': round(max_drawdown, 2),
                'total_trades': total_trades,
                'wins': wins, 'losses': losses,
                'win_rate': round(wins/total_trades*100, 1) if total_trades else 0,
                'first_exits': first_exits,
                'reentry_exits': reentry_exits,
                'reentry_wins': reentry_wins,
                'reentry_pnl_krw': round(reentry_pnl),
                'reentry_contribution_pct': round(reentry_contrib_pct, 1),
                'avg_win_pct': round(avg_win, 2),
                'avg_loss_pct': round(avg_loss, 2),
                'total_commission_usd': round(total_commission, 2),
            },
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False)
    print(f"JSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
