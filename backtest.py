"""
매일 실행하는 데이터 기반 백테스트
- Polygon 과거 데이터로 전체 매매 사이클 시뮬레이션
- 동시 보유 최대 2종목, 70:30 비중
- 결과를 텔레그램으로 전송
"""
import os, sys, requests, time, json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# .env 로드
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k] = v

from polygon import RESTClient
import yaml

# Config
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'config.yaml')
with open(config_path) as f:
    CONFIG = yaml.safe_load(f)

client = RESTClient(api_key=os.environ['POLYGON_API_KEY'])
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

BUDGET = CONFIG['trading']['total_buy_amount']
SPLIT = CONFIG['trading']['split_count']
STOP_LOSS = CONFIG['trading']['stop_loss_pct'] / 100
TAKE_PROFIT = CONFIG['trading']['take_profit_pct'] / 100
MAX_POS = CONFIG['trading']['max_positions']  # 2
ALLOC = CONFIG['trading'].get('allocation_ratio', [0.7, 0.3])
KRW_USD = 1350

def send_tg(text):
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        except:
            pass

def calc_indicators(closes, volumes):
    """기술지표 계산"""
    s = pd.Series(closes)
    ema5 = s.ewm(span=5).mean().iloc[-1]
    ema20 = s.ewm(span=20).mean().iloc[-1]
    
    # RSI
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_g = np.mean(gains[-14:]) if len(gains) >= 14 else np.mean(gains)
    avg_l = np.mean(losses[-14:]) if len(losses) >= 14 else np.mean(losses)
    rsi = 100 - (100 / (1 + (avg_g / avg_l if avg_l > 0 else 100)))
    
    # MACD
    ema12 = s.ewm(span=12).mean()
    ema26 = s.ewm(span=26).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9).mean()
    macd_hist = (macd - macd_sig).iloc[-1]
    
    # 볼린저밴드
    sma20 = s.rolling(20).mean().iloc[-1]
    std20 = s.rolling(20).std().iloc[-1]
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_pos = (closes[-1] - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5
    
    # Volume
    avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    
    # 종합 스코어
    score = 0
    score += 30 if ema5 > ema20 else -30
    score += 25 if macd_hist > 0 else -25
    score += 15 if 30 < rsi < 70 else -15
    score += 30 if vol_ratio > 2.0 else (15 if vol_ratio > 1.5 else 0)
    
    confidence = min(max((score + 100) / 2, 0), 100)
    
    return {
        'ema5': ema5, 'ema20': ema20, 'rsi': rsi,
        'macd_hist': macd_hist, 'bb_pos': bb_pos,
        'vol_ratio': vol_ratio, 'score': score, 'confidence': confidence
    }


def run_backtest(days=30):
    """과거 N일 백테스트"""
    print(f"{'='*60}")
    print(f"StockBot 백테스트 ({days}일)")
    print(f"{'='*60}")
    
    send_tg(f"🔄 StockBot 백테스트 시작\n━━━━━━━━━━━━━━\n기간: 최근 {days}거래일\n예산: ₩{BUDGET:,}\n최대 보유: {MAX_POS}종목 (비중 {int(ALLOC[0]*100)}:{int(ALLOC[1]*100)})\n━━━━━━━━━━━━━━")
    
    # 1. 전종목 스냅샷으로 활발한 종목 추출
    print("\n[1/4] 전종목 스캔...")
    snaps = client.get_snapshot_all('stocks')
    
    candidates = []
    for s in snaps:
        try:
            if not s.day or not s.day.close or not s.todays_change_percent:
                continue
            price = s.day.close
            vol = s.day.volume or 0
            mcap = 0
            
            if (price >= CONFIG['scanner']['min_price'] and 
                vol >= CONFIG['scanner']['min_volume'] and
                abs(s.todays_change_percent) >= 3.0):
                candidates.append({
                    'ticker': s.ticker,
                    'price': price,
                    'change': s.todays_change_percent,
                    'volume': vol
                })
        except:
            continue
    
    # 상위 변동률 50개
    candidates = sorted(candidates, key=lambda x: abs(x['change']), reverse=True)[:50]
    print(f"  후보: {len(candidates)}종목")
    
    # 2. 시총 필터 + 과거 데이터 확보
    print("\n[2/4] 과거 데이터 수집 + 시총 필터...")
    stocks_data = []
    
    for c in candidates[:30]:
        try:
            # 시총 체크
            detail = client.get_ticker_details(c['ticker'])
            mcap = detail.market_cap or 0
            if mcap < CONFIG['scanner']['min_market_cap']:
                continue
            
            # 과거 데이터
            end = datetime.now().strftime('%Y-%m-%d')
            start = (datetime.now() - timedelta(days=days*2)).strftime('%Y-%m-%d')
            aggs = list(client.get_aggs(c['ticker'], 1, 'day', start, end, limit=days+30))
            time.sleep(0.15)
            
            if len(aggs) >= 30:
                stocks_data.append({
                    'ticker': c['ticker'],
                    'name': detail.name or c['ticker'],
                    'mcap': mcap,
                    'aggs': aggs
                })
        except:
            continue
    
    print(f"  데이터 확보: {len(stocks_data)}종목")
    
    # 3. 백테스트 실행
    print(f"\n[3/4] 백테스트 실행...")
    all_trades = []
    
    for stock in stocks_data:
        ticker = stock['ticker']
        aggs = stock['aggs']
        position = None
        
        for i in range(25, len(aggs)):
            closes = np.array([a.close for a in aggs[:i+1]])
            volumes = np.array([a.volume for a in aggs[:i+1]])
            current = aggs[i]
            price = current.close
            ts = datetime.fromtimestamp(current.timestamp/1000)
            date_str = ts.strftime('%m/%d')
            
            ind = calc_indicators(closes, volumes)
            
            # 전일 대비 변동
            prev_close = aggs[i-1].close
            change_pct = ((price - prev_close) / prev_close) * 100
            prev_vol = aggs[i-1].volume or 1
            vol_spike = (current.volume / prev_vol) * 100
            
            if position is None:
                # 매수 시그널
                if (change_pct >= CONFIG['scanner']['price_change_pct'] and
                    vol_spike >= CONFIG['scanner']['volume_spike_pct'] and
                    current.volume >= CONFIG['scanner']['min_volume'] and
                    ind['score'] > 30 and change_pct > 0):
                    
                    alloc_usd = (BUDGET / KRW_USD)
                    shares = max(1, int(alloc_usd / SPLIT / price)) * SPLIT
                    
                    position = {
                        'entry_price': price,
                        'shares': shares,
                        'entry_date': date_str,
                        'entry_time': ts.strftime('%H:%M'),
                        'signal_conf': ind['confidence'],
                        'entry_ind': ind.copy(),
                        'entry_change': change_pct,
                        'max_price': price,
                    }
            else:
                # 매도 체크
                position['max_price'] = max(position['max_price'], price)
                pnl_pct = (price - position['entry_price']) / position['entry_price']
                
                sell_reason = None
                if pnl_pct <= STOP_LOSS:
                    sell_reason = "🛑 손절 (-15%)"
                elif pnl_pct >= TAKE_PROFIT and ind['score'] < 0:
                    sell_reason = "💰 익절+추세꺾임"
                elif pnl_pct >= TAKE_PROFIT * 0.5 and ind['ema5'] < ind['ema20'] and ind['macd_hist'] < 0:
                    sell_reason = "📉 추세 반전"
                
                if sell_reason:
                    pnl_usd = (price - position['entry_price']) * position['shares']
                    all_trades.append({
                        'ticker': ticker,
                        'name': stock['name'][:15],
                        'signal_date': position['entry_date'],
                        'signal_time': position['entry_time'],
                        'signal_conf': position['signal_conf'],
                        'buy_date': position['entry_date'],
                        'buy_price': position['entry_price'],
                        'sell_date': date_str,
                        'sell_price': price,
                        'shares': position['shares'],
                        'pnl_pct': pnl_pct * 100,
                        'pnl_usd': pnl_usd,
                        'pnl_krw': pnl_usd * KRW_USD,
                        'reason': sell_reason,
                        'entry_rsi': position['entry_ind']['rsi'],
                        'exit_rsi': ind['rsi'],
                        'entry_macd': position['entry_ind']['macd_hist'],
                        'exit_macd': ind['macd_hist'],
                        'vol_ratio': position['entry_ind']['vol_ratio'],
                        'holding_days': i - [j for j in range(len(aggs)) if aggs[j].close == position['entry_price']][0] if position['entry_price'] in [a.close for a in aggs] else 0
                    })
                    position = None
        
        # 미청산 포지션
        if position:
            price = aggs[-1].close
            pnl_pct = (price - position['entry_price']) / position['entry_price']
            pnl_usd = (price - position['entry_price']) * position['shares']
            all_trades.append({
                'ticker': ticker,
                'name': stock['name'][:15],
                'signal_date': position['entry_date'],
                'signal_time': position['entry_time'],
                'signal_conf': position['signal_conf'],
                'buy_date': position['entry_date'],
                'buy_price': position['entry_price'],
                'sell_date': '보유중',
                'sell_price': price,
                'shares': position['shares'],
                'pnl_pct': pnl_pct * 100,
                'pnl_usd': pnl_usd,
                'pnl_krw': pnl_usd * KRW_USD,
                'reason': '⏳ 보유 중',
                'entry_rsi': position['entry_ind']['rsi'],
                'exit_rsi': ind['rsi'],
                'entry_macd': position['entry_ind']['macd_hist'],
                'exit_macd': ind['macd_hist'],
                'vol_ratio': position['entry_ind']['vol_ratio'],
                'holding_days': 0
            })
    
    # 4. 70:30 비중 적용 — 신뢰도 순 정렬
    all_trades = sorted(all_trades, key=lambda x: x['signal_conf'], reverse=True)
    
    # 리포트
    print(f"\n[4/4] 리포트 생성...")
    
    wins = [t for t in all_trades if t['pnl_pct'] > 0]
    losses = [t for t in all_trades if t['pnl_pct'] <= 0 and t['reason'] != '⏳ 보유 중']
    holding = [t for t in all_trades if t['reason'] == '⏳ 보유 중']
    total_pnl = sum(t['pnl_krw'] for t in all_trades)
    win_rate = len(wins) / max(len(wins) + len(losses), 1) * 100
    
    msg = f"📊 StockBot 백테스트 리포트\n━━━━━━━━━━━━━━\n"
    msg += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    msg += f"기간: 최근 {days}거래일\n"
    msg += f"스캔: {len(snaps):,}종목 → {len(stocks_data)}종목\n"
    msg += f"매매: {len(all_trades)}건 (승 {len(wins)} / 패 {len(losses)} / 보유 {len(holding)})\n"
    msg += f"승률: {win_rate:.0f}%\n\n"
    
    # 상위 매매 (70:30 기준 최대 2건씩)
    msg += "💼 매매 상세 (신뢰도순):\n"
    for i, t in enumerate(all_trades[:6]):
        alloc_pct = ALLOC[0]*100 if i == 0 else ALLOC[1]*100 if i == 1 else 0
        emoji = "✅" if t['pnl_pct'] > 0 else "❌" if t['pnl_pct'] < -5 else "➡️"
        msg += f"\n{emoji} #{i+1} {t['ticker']} ({t['name']})"
        if alloc_pct > 0:
            msg += f" [{alloc_pct:.0f}%배분]"
        msg += f"\n  시그널: {t['signal_date']} {t['signal_time']} (conf {t['signal_conf']:.0f}%)\n"
        msg += f"  매수: {t['buy_date']} @ ${t['buy_price']:.2f}\n"
        msg += f"  매도: {t['sell_date']} @ ${t['sell_price']:.2f}\n"
        msg += f"  수익: {t['pnl_pct']:+.1f}% (₩{t['pnl_krw']:+,.0f})\n"
        msg += f"  사유: {t['reason']}\n"
        msg += f"  RSI {t['entry_rsi']:.0f}→{t['exit_rsi']:.0f} | MACD {t['entry_macd']:.3f}→{t['exit_macd']:.3f} | Vol {t['vol_ratio']:.1f}x\n"
    
    # 70:30 시뮬레이션 수익
    if len(all_trades) >= 2:
        top1_pnl = all_trades[0]['pnl_pct'] * ALLOC[0]
        top2_pnl = all_trades[1]['pnl_pct'] * ALLOC[1]
        weighted_pnl = top1_pnl + top2_pnl
        weighted_krw = weighted_pnl / 100 * BUDGET
        msg += f"\n━━━━━━━━━━━━━━\n"
        msg += f"📈 70:30 포트 수익: {weighted_pnl:+.1f}% (₩{weighted_krw:+,.0f})\n"
    
    msg += f"\n💰 전체 총손익: ₩{total_pnl:+,.0f}\n"
    msg += f"━━━━━━━━━━━━━━"
    
    send_tg(msg)
    print(f"\n총 {len(all_trades)}건 매매, 승률 {win_rate:.0f}%, 총손익 ₩{total_pnl:+,.0f}")
    print("텔레그램 전송 완료!")
    
    return all_trades


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_backtest(days)
