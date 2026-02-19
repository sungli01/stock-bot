#!/usr/bin/env python3
"""v6를 기반으로 v11 백테스트 생성 (v6 매수 + v10 매도)"""
with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v6.py') as f:
    code = f.read()

# 1. Stop loss -50 -> -30
code = code.replace('STOP_LOSS_PCT = -0.50', 'STOP_LOSS_PCT = -0.30')

# 2. Naming
code = code.replace('Backtest v6', 'Backtest v11')
code = code.replace('backtest_v6', 'backtest_v11')
code = code.replace('v6:', 'v11:')

# 3. Max price filter
code = code.replace('MIN_PRICE = 0.7', 'MIN_PRICE = 0.7\nMAX_PRICE = 10.0')

# 4. Add max_price and 100% filters in get_day_gainers
code = code.replace(
    'if o <= 0 or c <= 0 or v < 100000 or o < MIN_PRICE:',
    'if o <= 0 or c <= 0 or v < 100000 or o < MIN_PRICE or o > MAX_PRICE:'
)
code = code.replace(
    'if change_pct >= 10 and v >= 500000:',
    'if 10 <= change_pct < 100 and v >= 500000:'
)

# 5. Replace staircase: remove +300% take profit, add 20min no-movement exit
# Find the sell logic section and add 20-min check before other sells
old_sell_start = "        # === SELL LOGIC ==="
new_sell_block = """        # === SELL LOGIC (v11) ===

        # 0. 20분 무변동: 매수 후 20분 경과 & ±3% 이내면 매도
        bars_since_buy = i - position['buy_idx_1m']
        if bars_since_buy >= 20:
            profit_from_buy = (cur_close / position['buy_price'] - 1)
            if abs(profit_from_buy) <= 0.03:
                raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                sell_price = apply_slippage_sell(raw_sell)
                commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                pnl_pct = (sell_price / position['buy_price'] - 1)
                pnl_krw = position['invested'] * pnl_pct - commission
                trades.append({
                    'ticker': ticker, 'phase': '1st',
                    'buy_price': round(position['buy_price'], 4),
                    'sell_price': round(sell_price, 4),
                    'sell_reason': '20분무변동',
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
                continue"""

if old_sell_start in code:
    code = code.replace(old_sell_start, new_sell_block)

# 6. Change staircase floors in sell logic: replace 300% take profit
# Remove the +300% take profit block entirely - replace with staircase 120->200->300
old_tp = """        # 2. Big take profit: +300%
        cur_profit_pct_high = (cur_high / position['buy_price'] - 1)
        if cur_profit_pct_high >= TAKE_PROFIT_PCT:
            tp_target = position['buy_price'] * (1 + TAKE_PROFIT_PCT)
            raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else tp_target)
            sell_price = apply_slippage_sell(raw_sell)
            commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
            pnl_pct = (sell_price / position['buy_price'] - 1)
            pnl_krw = position['invested'] * pnl_pct - commission
            trades.append({
                'ticker': ticker, 'phase': '1st',
                'buy_price': round(position['buy_price'], 4),
                'sell_price': round(sell_price, 4),
                'sell_reason': '익절(+300%)',
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
            continue"""

new_staircase = """        # 2. 계단식 플로어: 120% -> 200% -> 300%
        STAIRCASE = [1.20, 2.00, 3.00]
        if peak_profit_pct >= 1.20:
            current_floor_pct = 1.20
            for sf in STAIRCASE:
                if peak_profit_pct >= sf:
                    current_floor_pct = sf
            if cur_profit_pct < current_floor_pct:
                raw_sell = clamp(bars_1m[min(i+1, len(bars_1m)-1)]['o'] if i+1 < len(bars_1m) else cur_close)
                sell_price = apply_slippage_sell(raw_sell)
                commission = position.get('buy_commission', 0) + calc_commission(position['shares'], sell_price)
                pnl_pct_val = (sell_price / position['buy_price'] - 1)
                pnl_krw = position['invested'] * pnl_pct_val - commission
                trades.append({
                    'ticker': ticker, 'phase': '1st',
                    'buy_price': round(position['buy_price'], 4),
                    'sell_price': round(sell_price, 4),
                    'sell_reason': f'계단{int(current_floor_pct*100)}%',
                    'pnl_pct': round(pnl_pct_val * 100, 2),
                    'pnl_krw': round(pnl_krw),
                    'invested': round(position['invested']),
                    'commission': round(commission, 2),
                    'peak_profit_pct': round(peak_profit_pct * 100, 2),
                    'bb_broken': position.get('bb_broken', False),
                    'sell_algo_activated': position.get('sell_algo_activated', False),
                })
                position = None
                i += 2
                continue"""

if old_tp in code:
    code = code.replace(old_tp, new_staircase)
else:
    print("WARNING: Could not find take profit block to replace")

with open('/home/ubuntu/.openclaw/workspace/stock-bot/backtest_v11.py', 'w') as f:
    f.write(code)

print("v11 created successfully")
