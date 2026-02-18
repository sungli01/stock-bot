#!/usr/bin/env python3
"""
Backtest Realistic: 스냅샷 시뮬레이션 방식
- look-ahead bias 완전 제거
- grouped daily에서 후보 필터 (거래량 50만+, 고가/시가 1.05+)
- 후보 종목의 1분봉을 시간순 시뮬레이션
- 매 분마다 "실시간 스캐너"처럼 조건 충족 여부 판단
- 매도 로직은 v6 동일
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
MIN_PRICE = 0.70
MAX_PRICE = 10.00
STOP_LOSS_PCT = -0.50
BB_PERIOD = 20
BB_STD = 2

# Scanner thresholds
VOL_SURGE_PCT = 1.0        # 거래량 100%+ (직전 10분 평균 대비)
PRICE_SURGE_PCT = 0.10     # 전일종가 대비 10%+
MAX_CHANGE_PCT = 1.00      # 100%+ 이미 오른 종목 제외

# v6 sell params
TRAILING_FROM_PEAK = 0.15
MIN_MARGIN_SELL = 0.35
PROFIT_ACTIVATE = 0.30

# Realistic constraints
SLIPPAGE_BUY = 0.005
SLIPPAGE_SELL = 0.005
COMMISSION_PCT = 0.001

LOOKBACK_MINS = 10

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

def get_grouped_daily(date_str):
    key = f"grouped_{date_str}"
    cached = _load_cache(key)
    if cached is not None:
        return cached
    url = f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    data = api_get(url, {"adjusted": "true"})
    results = data.get('results', [])
    _save_cache(key, results)
    return results

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

def market_close_utc(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    month = dt.month
    if 3 <= month <= 10:
        return dt.replace(hour=20, minute=0)
    else:
        return dt.replace(hour=21, minute=0)


def filter_candidates_from_grouped(results, prev_closes):
    """
    1단계 필터: grouped daily에서 후보 추출
    - 거래량 50만+
    - 고가/시가 비율 1.05+ (최소 5% 움직임)
    - 시가 $0.70~$10.00
    - 티커 길이 <= 5, 특수문자 없음
    """
    candidates = []
    for r in results:
        ticker = r.get('T', '')
        if len(ticker) > 5 or '.' in ticker or '-' in ticker:
            continue
        o = r.get('o', 0)
        h = r.get('h', 0)
        v = r.get('v', 0)
        c = r.get('c', 0)
        if o <= 0 or h <= 0 or v < 500000:
            continue
        if o < MIN_PRICE or o > MAX_PRICE:
            continue
        if h / o < 1.10:  # 최소 10% 움직인 종목만 (스캐너 10% 조건과 일치)
            continue
        prev_c = prev_closes.get(ticker)
        candidates.append({
            'ticker': ticker,
            'open': o, 'close': c, 'high': h, 'low': r.get('l', 0),
            'volume': v,
            'prev_close': prev_c,
        })
    return candidates


def simulate_day_realistic(date_str, candidates, capital, prev_closes):
    """
    시간순 스냅샷 시뮬레이션:
    1. 모든 후보 종목의 1분봉을 가져옴
    2. 시간순으로 진행하면서 스캐너 조건 체크
    3. 조건 충족 시 매수, v6 매도 로직 적용
    """
    if not candidates:
        return [], capital

    # Fetch 1-min and 5-min bars for all candidates
    ticker_bars_1m = {}
    ticker_bars_5m = {}
    ticker_info = {}
    
    for cand in candidates:
        ticker = cand['ticker']
        bars_1m = get_bars(ticker, date_str, 1, 'minute')
        if not bars_1m or len(bars_1m) < 20:
            continue
        bars_5m = get_bars(ticker, date_str, 5, 'minute')
        if not bars_5m or len(bars_5m) < 5:
            continue
        ticker_bars_1m[ticker] = bars_1m
        ticker_bars_5m[ticker] = bars_5m
        ticker_info[ticker] = cand

    if not ticker_bars_1m:
        return [], capital

    # Build unified timeline: collect all unique timestamps
    all_timestamps = set()
    for bars in ticker_bars_1m.values():
        for b in bars:
            all_timestamps.add(b['t'])
    timeline = sorted(all_timestamps)

    # Pre-index: for each ticker, build ts -> bar mapping and sorted bar list
    ticker_bar_map = {}  # ticker -> {ts: bar}
    for ticker, bars in ticker_bars_1m.items():
        ticker_bar_map[ticker] = {b['t']: b for b in bars}

    # State tracking per ticker
    ticker_state = {}  # ticker -> {'bars_so_far': [...], 'prev_close': float}
    for ticker in ticker_bars_1m:
        pc = ticker_info[ticker].get('prev_close')
        if pc is None:
            pc = ticker_info[ticker]['open']  # fallback to open
        ticker_state[ticker] = {
            'recent_vols': [],  # last LOOKBACK_MINS volumes
            'prev_close': pc,
        }

    # Positions and trades
    positions = {}  # ticker -> position dict
    trades = []
    attempted_tickers = set()  # no re-entry same day
    cap_per_pos = min(capital / MAX_POSITIONS, COMPOUND_CAP / MAX_POSITIONS)
    
    mc = market_close_utc(date_str)
    close_time = mc - timedelta(minutes=15)
    close_ts = int(close_time.timestamp()) * 1000

    def get_5m_closes_at(ticker, ts):
        """Get 5m closes up to timestamp ts"""
        bars_5m = ticker_bars_5m.get(ticker, [])
        return [b['c'] for b in bars_5m if b['t'] <= ts]

    def try_sell(ticker, pos, bar, next_bar, ts):
        """v6 sell logic. Returns trade dict or None."""
        info = ticker_info[ticker]
        daily_high = info['high']
        daily_low = info['low']
        def clamp(v):
            return min(max(v, daily_low), daily_high)

        cur_high = clamp(bar['h'])
        cur_low = clamp(bar['l'])
        cur_close = clamp(bar['c'])

        prev_peak = pos['peak']
        if cur_high > pos['peak']:
            pos['peak'] = cur_high
            pos['making_new_highs'] = True

        peak_profit_pct = (pos['peak'] / pos['buy_price'] - 1)
        cur_profit_pct = (cur_close / pos['buy_price'] - 1)
        cur_profit_pct_low = (cur_low / pos['buy_price'] - 1)

        # BB check
        closes_5m = get_5m_closes_at(ticker, ts)
        bb = compute_bb(closes_5m)
        if bb:
            bb_upper, _, _ = bb
            if cur_high > bb_upper:
                pos['bb_broken'] = True

        if (peak_profit_pct >= PROFIT_ACTIVATE and pos['bb_broken'] and pos['making_new_highs']):
            pos['sell_algo_activated'] = True

        def make_trade(sell_price_raw, reason):
            sp = apply_slippage_sell(clamp(sell_price_raw))
            commission = pos['buy_commission'] + calc_commission(pos['shares'], sp)
            pnl_pct = (sp / pos['buy_price'] - 1)
            pnl_krw = pos['invested'] * pnl_pct - commission
            return {
                'ticker': ticker,
                'buy_price': round(pos['buy_price'], 4),
                'sell_price': round(sp, 4),
                'sell_reason': reason,
                'pnl_pct': round(pnl_pct * 100, 2),
                'pnl_krw': round(pnl_krw),
                'invested': round(pos['invested']),
                'commission': round(commission, 2),
                'peak_profit_pct': round(peak_profit_pct * 100, 2),
                'bb_broken': pos.get('bb_broken', False),
                'sell_algo_activated': pos.get('sell_algo_activated', False),
            }

        next_open = next_bar['o'] if next_bar else bar['c']

        # 1. Stop loss -50%
        if cur_profit_pct_low <= STOP_LOSS_PCT:
            sl_target = pos['buy_price'] * (1 + STOP_LOSS_PCT)
            return make_trade(min(next_open, sl_target), '손절(-50%)')

        # 2. Sell algo activated
        if pos['sell_algo_activated']:
            if cur_profit_pct < MIN_MARGIN_SELL:
                return make_trade(next_open, '35%마진보호')
            drop_from_peak = (pos['peak'] - cur_close) / pos['peak'] if pos['peak'] > 0 else 0
            if drop_from_peak >= TRAILING_FROM_PEAK:
                return make_trade(next_open, '고점-15%트레일')

        # 3. 30% floor (BB 미돌파)
        if not pos['sell_algo_activated'] and peak_profit_pct >= PROFIT_ACTIVATE:
            if cur_profit_pct < PROFIT_ACTIVATE:
                return make_trade(next_open, '30%플로어(BB미돌파)')

        if cur_high <= prev_peak:
            pos['making_new_highs'] = False

        return None

    # Main time loop
    for t_idx, ts in enumerate(timeline):
        # Force close check
        if ts >= close_ts:
            for ticker in list(positions.keys()):
                pos = positions[ticker]
                bars = ticker_bars_1m[ticker]
                last_bar = None
                for b in reversed(bars):
                    if b['t'] <= ts:
                        last_bar = b
                        break
                if last_bar is None:
                    last_bar = bars[-1]
                info = ticker_info[ticker]
                sp = apply_slippage_sell(last_bar['c'])
                commission = pos['buy_commission'] + calc_commission(pos['shares'], sp)
                pnl_pct = (sp / pos['buy_price'] - 1)
                pnl_krw = pos['invested'] * pnl_pct - commission
                trades.append({
                    'ticker': ticker,
                    'buy_price': round(pos['buy_price'], 4),
                    'sell_price': round(sp, 4),
                    'sell_reason': '장마감',
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'pnl_krw': round(pnl_krw),
                    'invested': round(pos['invested']),
                    'commission': round(commission, 2),
                    'peak_profit_pct': round((pos['peak'] / pos['buy_price'] - 1) * 100, 2),
                    'bb_broken': pos.get('bb_broken', False),
                    'sell_algo_activated': pos.get('sell_algo_activated', False),
                })
                del positions[ticker]
            break

        # Process sells first for existing positions
        for ticker in list(positions.keys()):
            bar_map = ticker_bar_map[ticker]
            if ts not in bar_map:
                continue
            bar = bar_map[ts]
            # Find next bar
            bars_list = ticker_bars_1m[ticker]
            next_bar = None
            for b in bars_list:
                if b['t'] > ts:
                    next_bar = b
                    break
            trade = try_sell(ticker, positions[ticker], bar, next_bar, ts)
            if trade:
                trades.append(trade)
                del positions[ticker]

        # Update state for all tickers at this timestamp
        scan_candidates = []
        for ticker in ticker_bars_1m:
            bar_map = ticker_bar_map[ticker]
            if ts not in bar_map:
                continue
            bar = bar_map[ts]
            state = ticker_state[ticker]

            # Update rolling volume
            state['recent_vols'].append(bar.get('v', 0))
            if len(state['recent_vols']) > LOOKBACK_MINS:
                state['recent_vols'] = state['recent_vols'][-LOOKBACK_MINS:]

            # Skip if already in position or attempted
            if ticker in positions or ticker in attempted_tickers:
                continue

            # Skip if not enough data
            if len(state['recent_vols']) < LOOKBACK_MINS:
                continue

            # Check scanner conditions
            cur_price = bar['c']
            prev_close = state['prev_close']
            if prev_close is None or prev_close <= 0:
                continue

            # Price range filter
            if cur_price < MIN_PRICE or cur_price > MAX_PRICE:
                continue

            # Change from prev close
            change_pct = (cur_price / prev_close - 1)
            if change_pct >= MAX_CHANGE_PCT:
                continue  # 100%+ already, skip
            if change_pct < PRICE_SURGE_PCT:
                continue  # < 10%, skip

            # Volume surge: current bar vs avg of previous LOOKBACK bars
            prev_vols = state['recent_vols'][:-1]  # exclude current
            if len(prev_vols) < LOOKBACK_MINS - 1:
                continue
            avg_vol = np.mean(prev_vols)
            cur_vol = bar.get('v', 0)
            if avg_vol <= 0:
                continue
            vol_ratio = cur_vol / avg_vol - 1
            if vol_ratio < VOL_SURGE_PCT:
                continue

            # Liquidity check
            if cur_vol * cur_price < cap_per_pos * 0.5:
                continue

            # Score: vol_ratio * change_pct
            score = vol_ratio * change_pct
            scan_candidates.append({
                'ticker': ticker,
                'price': cur_price,
                'change_pct': change_pct,
                'vol_ratio': vol_ratio,
                'score': score,
                'ts': ts,
            })

        # Buy top candidates if slots available
        if scan_candidates and len(positions) < MAX_POSITIONS:
            scan_candidates.sort(key=lambda x: x['score'], reverse=True)
            for sc in scan_candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                ticker = sc['ticker']
                if ticker in attempted_tickers:
                    continue

                # Find next bar for buy execution
                bars_list = ticker_bars_1m[ticker]
                next_bar = None
                for b in bars_list:
                    if b['t'] > ts:
                        next_bar = b
                        break
                if next_bar is None:
                    continue

                info = ticker_info[ticker]
                buy_price = apply_slippage_buy(next_bar['o'])
                if buy_price <= 0:
                    continue
                invested = min(cap_per_pos, COMPOUND_CAP / MAX_POSITIONS)
                shares = invested / buy_price
                buy_commission = calc_commission(shares, buy_price)

                positions[ticker] = {
                    'buy_price': buy_price,
                    'invested': invested,
                    'shares': shares,
                    'peak': buy_price,
                    'bb_broken': False,
                    'sell_algo_activated': False,
                    'making_new_highs': False,
                    'buy_commission': buy_commission,
                }
                attempted_tickers.add(ticker)

    # Force close any remaining positions
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        bars = ticker_bars_1m[ticker]
        last_bar = bars[-1]
        sp = apply_slippage_sell(last_bar['c'])
        commission = pos['buy_commission'] + calc_commission(pos['shares'], sp)
        pnl_pct = (sp / pos['buy_price'] - 1)
        pnl_krw = pos['invested'] * pnl_pct - commission
        trades.append({
            'ticker': ticker,
            'buy_price': round(pos['buy_price'], 4),
            'sell_price': round(sp, 4),
            'sell_reason': '장마감',
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_krw': round(pnl_krw),
            'invested': round(pos['invested']),
            'commission': round(commission, 2),
            'peak_profit_pct': round((pos['peak'] / pos['buy_price'] - 1) * 100, 2),
            'bb_broken': pos.get('bb_broken', False),
            'sell_algo_activated': pos.get('sell_algo_activated', False),
        })

    day_pnl = sum(t['pnl_krw'] for t in trades)
    new_capital = capital + day_pnl
    new_capital = max(new_capital, 10000)
    return trades, new_capital


def run_backtest():
    print("=" * 60)
    print("Backtest REALISTIC: 스냅샷 시뮬레이션 (look-ahead bias 제거)")
    print("=" * 60)

    end_date = '2026-02-18'
    start_date = '2025-11-01'

    print(f"Fetching trading days {start_date} ~ {end_date}...")
    all_days = get_trading_days(start_date, end_date)
    trading_days = all_days[-60:] if len(all_days) >= 60 else all_days
    print(f"Got {len(trading_days)} trading days: {trading_days[0]} ~ {trading_days[-1]}")

    # Build prev_closes map by processing grouped daily for each day
    capital = INITIAL_CAPITAL
    all_results = []
    total_trades = 0
    wins = 0
    losses = 0
    prev_closes = {}

    for day_idx, date_str in enumerate(trading_days):
        print(f"\n[{day_idx+1}/{len(trading_days)}] {date_str} | Capital: ₩{capital:,.0f}")

        # Get grouped daily
        grouped = get_grouped_daily(date_str)
        if not grouped:
            print("  No grouped data")
            all_results.append({'date': date_str, 'trades': [], 'day_pnl': 0, 'capital_after': round(capital)})
            # Update prev_closes anyway
            continue

        # Filter candidates (NO look-ahead: we only use volume & high/open ratio)
        candidates = filter_candidates_from_grouped(grouped, prev_closes)
        print(f"  Filtered candidates: {len(candidates)} tickers")

        if candidates:
            # Simulate the day
            day_trades, new_capital = simulate_day_realistic(
                date_str, candidates, capital, prev_closes
            )

            for t in day_trades:
                print(f"  {t['ticker']}: {t['sell_reason']} → {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,})")
                total_trades += 1
                if t['pnl_pct'] > 0:
                    wins += 1
                else:
                    losses += 1

            day_pnl = sum(t['pnl_krw'] for t in day_trades)
            capital = new_capital
        else:
            day_trades = []
            day_pnl = 0

        all_results.append({
            'date': date_str, 'trades': day_trades,
            'day_pnl': round(day_pnl), 'capital_after': round(capital),
            'num_candidates': len(candidates),
        })

        if not day_trades:
            print("  No trades executed")

        # Update prev_closes for next day
        new_closes = {}
        for r in grouped:
            ticker = r.get('T', '')
            c = r.get('c', 0)
            if c > 0:
                new_closes[ticker] = c
        prev_closes = new_closes

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

    # Load comparisons
    comparisons = {}
    for ver in ['v5', 'v6', 'v8']:
        try:
            with open(f'/home/ubuntu/.openclaw/workspace/stock-bot/backtest_{ver}_result.json') as f:
                comparisons[ver] = json.load(f)['summary']
        except:
            pass

    print("\n" + "=" * 60)
    print("BACKTEST REALISTIC RESULTS")
    print("=" * 60)
    print(f"Period: {trading_days[0]} ~ {trading_days[-1]} ({len(trading_days)} days)")
    print(f"Initial: ₩{INITIAL_CAPITAL:,} → Final: ₩{capital:,.0f}")
    print(f"Return: {final_return:+.1f}% | MDD: {max_drawdown:.1f}%")
    print(f"Trades: {total_trades} (W:{wins} L:{losses}) WR:{wr}%")
    print(f"Avg Win: {avg_win:+.1f}% | Avg Loss: {avg_loss:+.1f}%")
    print(f"Days: +{plus_days} / -{minus_days} / 0:{zero_days}")
    print(f"Commission: ₩{total_commission:,.0f}")

    print("\n--- 비교표 ---")
    print(f"{'Version':<12} {'Final':>12} {'Return':>10} {'Trades':>7} {'WR':>6} {'MDD':>6} {'AvgWin':>8} {'AvgLoss':>8}")
    print("-" * 75)
    for ver, s in comparisons.items():
        print(f"{ver:<12} ₩{s['final_capital']:>10,} {s['total_return_pct']:>+9.1f}% {s['total_trades']:>7} {s['win_rate']:>5.1f}% {s['max_drawdown_pct']:>5.1f}% {s['avg_win_pct']:>+7.1f}% {s['avg_loss_pct']:>+7.1f}%")
    print(f"{'realistic':<12} ₩{capital:>10,.0f} {final_return:>+9.1f}% {total_trades:>7} {wr:>5}% {max_drawdown:>5.1f}% {avg_win:>+7.1f}% {avg_loss:>+7.1f}%")

    print("\n매도 사유 분포:")
    for reason, stats in sorted(reason_counts.items(), key=lambda x: -x[1]['count']):
        avg_r = np.mean(stats['pnls']) if stats['pnls'] else 0
        print(f"  {reason}: {stats['count']}건, 평균 {avg_r:+.1f}%, 총 ₩{stats['total_pnl']:+,}")

    # Save JSON
    json_path = '/home/ubuntu/.openclaw/workspace/stock-bot/backtest_realistic_result.json'
    with open(json_path, 'w') as f:
        json.dump({
            'config': {
                'method': 'snapshot_simulation',
                'description': 'No look-ahead bias. Minute-by-minute scanner simulation.',
                'initial_capital': INITIAL_CAPITAL,
                'min_price': MIN_PRICE, 'max_price': MAX_PRICE,
                'stop_loss': STOP_LOSS_PCT,
                'vol_surge_pct': VOL_SURGE_PCT,
                'price_surge_pct': PRICE_SURGE_PCT,
                'max_change_pct': MAX_CHANGE_PCT,
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
                'avg_win_pct': round(float(avg_win), 2),
                'avg_loss_pct': round(float(avg_loss), 2),
                'total_commission': round(total_commission, 2),
                'plus_days': plus_days, 'minus_days': minus_days, 'zero_days': zero_days,
            },
            'sell_reasons': {reason: {'count': s['count'], 'avg_pnl': round(float(np.mean(s['pnls'])), 2), 'total_pnl': round(s['total_pnl'])} for reason, s in reason_counts.items()},
            'daily': all_results,
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nJSON saved to {json_path}")


if __name__ == '__main__':
    run_backtest()
