"""
Paper Trading 시스템
가상 잔고로 실제 Polygon 데이터 기반 매수/매도 시뮬레이션
"""
import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("paper_trader")

DATA_DIR = Path(os.path.dirname(__file__)) / "data"
PORTFOLIO_FILE = DATA_DIR / "paper_portfolio.json"
SLIPPAGE = 0.005  # 0.5%
COMMISSION_PCT = 0.001  # 0.1%


class PaperTrader:
    def __init__(self, initial_capital=1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}  # {ticker: {shares, avg_price, buy_time, quantity}}
        self.trades = []  # 거래 이력
        self.load_state()

    def buy(self, ticker: str, price: float, amount: float) -> dict | None:
        """
        가상 매수 (슬리피지 0.5% 적용)
        amount: 투자 금액 (KRW)
        Returns: 주문 결과 dict or None
        """
        buy_price = price * (1 + SLIPPAGE)
        commission = amount * COMMISSION_PCT

        total_cost = amount + commission
        if total_cost > self.cash:
            logger.warning(f"[가상] 잔고 부족: 필요 ₩{total_cost:,.0f}, 보유 ₩{self.cash:,.0f}")
            return None

        shares = amount / buy_price
        self.cash -= total_cost

        if ticker in self.positions:
            pos = self.positions[ticker]
            total_shares = pos['shares'] + shares
            pos['avg_price'] = (pos['avg_price'] * pos['shares'] + buy_price * shares) / total_shares
            pos['shares'] = total_shares
            pos['quantity'] = int(total_shares)
        else:
            self.positions[ticker] = {
                'shares': shares,
                'avg_price': buy_price,
                'buy_time': datetime.now().isoformat(),
                'quantity': int(shares),
            }

        trade = {
            'side': 'BUY',
            'ticker': ticker,
            'price': round(buy_price, 4),
            'shares': round(shares, 4),
            'amount': round(amount),
            'commission': round(commission, 2),
            'time': datetime.now().isoformat(),
        }
        self.trades.append(trade)
        self.save_state()

        logger.info(f"[가상] ✅ 매수 {ticker}: ${buy_price:.2f} x {shares:.2f}주 = ₩{amount:,.0f}")
        return {
            'ticker': ticker,
            'side': 'BUY',
            'price': buy_price,
            'shares': shares,
            'amount': amount,
        }

    def sell(self, ticker: str, price: float) -> dict | None:
        """가상 매도"""
        if ticker not in self.positions:
            logger.warning(f"[가상] 매도 실패: {ticker} 보유 없음")
            return None

        pos = self.positions[ticker]
        sell_price = price * (1 - SLIPPAGE)
        shares = pos['shares']
        proceeds = shares * sell_price
        commission = proceeds * COMMISSION_PCT

        pnl_pct = (sell_price / pos['avg_price'] - 1) * 100
        pnl_krw = proceeds - (shares * pos['avg_price']) - commission

        self.cash += proceeds - commission
        del self.positions[ticker]

        trade = {
            'side': 'SELL',
            'ticker': ticker,
            'price': round(sell_price, 4),
            'shares': round(shares, 4),
            'amount': round(proceeds),
            'commission': round(commission, 2),
            'pnl_pct': round(pnl_pct, 2),
            'pnl_krw': round(pnl_krw),
            'time': datetime.now().isoformat(),
        }
        self.trades.append(trade)
        self.save_state()

        emoji = '💰' if pnl_pct > 0 else '🚨'
        logger.info(f"[가상] {emoji} 매도 {ticker}: ${sell_price:.2f} ({pnl_pct:+.1f}%) ₩{pnl_krw:+,.0f}")
        return {
            'ticker': ticker,
            'side': 'SELL',
            'price': sell_price,
            'shares': shares,
            'pnl_pct': pnl_pct,
            'pnl_krw': pnl_krw,
        }

    def get_balance(self) -> dict:
        """KIS get_balance()와 동일한 형식 반환"""
        positions_list = []
        for ticker, pos in self.positions.items():
            positions_list.append({
                'ticker': ticker,
                'quantity': int(pos['shares']),
                'avg_price': pos['avg_price'],
                'current_price': pos['avg_price'],  # 실시간 가격은 외부에서 업데이트
                'shares': pos['shares'],
            })
        return {
            'cash': self.cash,
            'positions': positions_list,
        }

    def get_portfolio_value(self, prices: dict = None) -> float:
        """총 평가액 (prices: {ticker: current_price})"""
        total = self.cash
        for ticker, pos in self.positions.items():
            p = (prices or {}).get(ticker, pos['avg_price'])
            total += pos['shares'] * p
        return total

    def get_status_text(self, prices: dict = None) -> str:
        """텔레그램 상태 보고용 텍스트"""
        total_value = self.get_portfolio_value(prices)
        pnl = total_value - self.initial_capital
        pnl_pct = (total_value / self.initial_capital - 1) * 100

        lines = [
            f"📋 [가상매매] 포트폴리오",
            f"━━━━━━━━━━━━━━",
            f"총 평가: ₩{total_value:,.0f} ({pnl_pct:+.1f}%)",
            f"현금: ₩{self.cash:,.0f}",
            f"수익: ₩{pnl:+,.0f}",
        ]
        if self.positions:
            lines.append(f"보유 {len(self.positions)}종목:")
            for ticker, pos in self.positions.items():
                p = (prices or {}).get(ticker, pos['avg_price'])
                pos_pnl = (p / pos['avg_price'] - 1) * 100
                lines.append(f"  {ticker}: ${p:.2f} ({pos_pnl:+.1f}%)")
        lines.append(f"총 거래: {len(self.trades)}건")
        return "\n".join(lines)

    def save_state(self):
        """JSON 저장"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            'cash': self.cash,
            'initial_capital': self.initial_capital,
            'positions': self.positions,
            'trades': self.trades[-100:],  # 최근 100건만
            'updated_at': datetime.now().isoformat(),
        }
        try:
            with open(PORTFOLIO_FILE, 'w') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"포트폴리오 저장 실패: {e}")

    def load_state(self):
        """JSON 로드 (없으면 초기값)"""
        try:
            if PORTFOLIO_FILE.exists():
                with open(PORTFOLIO_FILE) as f:
                    state = json.load(f)
                self.cash = state.get('cash', self.initial_capital)
                self.initial_capital = state.get('initial_capital', self.initial_capital)
                self.positions = state.get('positions', {})
                self.trades = state.get('trades', [])
                logger.info(f"[가상] 포트폴리오 복원: ₩{self.cash:,.0f}, {len(self.positions)}종목")
        except Exception as e:
            logger.warning(f"포트폴리오 로드 실패 (초기값 사용): {e}")

    def get_telegram_backup_text(self) -> str:
        """텔레그램 백업용 JSON 텍스트"""
        state = {
            'cash': round(self.cash),
            'positions': {k: {'shares': round(v['shares'], 4), 'avg_price': round(v['avg_price'], 4)} for k, v in self.positions.items()},
        }
        return f"📦 Paper Portfolio Backup:\n```\n{json.dumps(state, indent=2)}\n```"
