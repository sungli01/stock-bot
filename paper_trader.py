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

# [Bug #4] KRW→USD 환율 (환경변수 or 기본값 1450)
USD_KRW_RATE = float(os.getenv("USD_KRW_RATE", "1450.0"))


class PaperTrader:
    def __init__(self, initial_capital=1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}  # {ticker: {shares, avg_price, buy_time, quantity}}
        self.trades = []  # 거래 이력
        self.load_state()

    def buy(self, ticker: str, price: float, amount: float, daily_volume: int = 0) -> dict | None:
        """
        가상 매수 (슬리피지 0.5% 적용, 실전 체결 가능량 제한)
        amount: 투자 금액 (KRW)
        daily_volume: 일 거래량 (주) — 0이면 제한 없음
        Returns: 주문 결과 dict or None
        """
        buy_price_usd = price * (1 + SLIPPAGE)
        buy_price = buy_price_usd  # 하위 호환 (로그용)
        commission = amount * COMMISSION_PCT

        total_cost = amount + commission
        if total_cost > self.cash:
            logger.warning(f"[가상] 잔고 부족: 필요 ₩{total_cost:,.0f}, 보유 ₩{self.cash:,.0f}")
            return None

        # [Bug #4] KRW ÷ 환율 ÷ USD 단가 = 주수
        shares = (amount / USD_KRW_RATE) / buy_price_usd

        # 실전 체결 가능량 제한: 일 거래량의 5% 초과 매수 금지
        if daily_volume > 0:
            max_shares = daily_volume * 0.05
            if shares > max_shares:
                logger.warning(f"[가상] ⚠️ {ticker} 체결 제한: {shares:.0f}주 → {max_shares:.0f}주 (일거래량 {daily_volume:,}의 5%)")
                shares = max_shares
                amount = shares * buy_price
                commission = amount * COMMISSION_PCT
                total_cost = amount + commission
                if total_cost > self.cash:
                    logger.warning(f"[가상] 잔고 부족 (조정 후): ₩{total_cost:,.0f}")
                    return None
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

    def buy_split(self, ticker: str, price: float, amount: float,
                  splits: int = 10, daily_volume: int = 0) -> dict | None:
        """
        v8: 10분할 상단 호가 매수 시뮬레이션
        - 현재가 기준 +0.1% 간격으로 10단계 위 호가에 분산 매수
        - 평균 매수가 = 현재가 × (1 + 0.05% × splits/2) ≈ 현재가 × 1.0055
        - paper에서는 가중평균으로 단순화
        """
        split_amount = amount / splits
        total_shares = 0.0
        total_cost = 0.0
        filled = 0

        # 10개 호가: +0.1%, +0.2%, ..., +1.0% 위에 각 1/10씩 주문
        for i in range(1, splits + 1):
            order_price_usd = price * (1 + i * 0.001)  # 0.1% 간격 (USD)
            order_price_usd *= (1 + SLIPPAGE * 0.5)    # 부분 슬리피지
            commission = split_amount * COMMISSION_PCT
            cost = split_amount + commission

            if cost > self.cash:
                logger.warning(f"[가상] {ticker} {i}번째 분할매수 잔고 부족 — {filled}개 체결 후 중단")
                break

            # [Bug #4] KRW 금액 ÷ 환율 ÷ USD 단가 = 주수
            shares = (split_amount / USD_KRW_RATE) / order_price_usd
            self.cash -= cost
            total_shares += shares
            total_cost += split_amount
            filled += 1

        if total_shares <= 0:
            logger.warning(f"[가상] {ticker} 10분할 매수 전부 실패 — 잔고 부족")
            return None

        avg_price = total_cost / total_shares
        commission_total = total_cost * COMMISSION_PCT

        if ticker in self.positions:
            pos = self.positions[ticker]
            all_shares = pos['shares'] + total_shares
            pos['avg_price'] = (pos['avg_price'] * pos['shares'] + avg_price * total_shares) / all_shares
            pos['shares'] = all_shares
            pos['quantity'] = int(all_shares)
        else:
            self.positions[ticker] = {
                'shares': total_shares,
                'avg_price': avg_price,
                'buy_time': datetime.now().isoformat(),
                'quantity': int(total_shares),
            }

        trade = {
            'side': 'BUY_SPLIT',
            'ticker': ticker,
            'price': round(avg_price, 4),
            'shares': round(total_shares, 4),
            'amount': round(total_cost),
            'commission': round(commission_total, 2),
            'fills': filled,
            'time': datetime.now().isoformat(),
        }
        self.trades.append(trade)
        self.save_state()

        logger.info(
            f"[가상] ✅ 10분할 매수 {ticker}: 평균${avg_price:.2f} x {total_shares:.2f}주 "
            f"= ₩{total_cost:,.0f} ({filled}/{splits} 체결)"
        )
        return {
            'ticker': ticker,
            'side': 'BUY_SPLIT',
            'price': avg_price,
            'shares': total_shares,
            'amount': total_cost,
            'fills': filled,
        }

    def partial_sell(self, ticker: str, price: float, ratio: float = 0.5) -> dict | None:
        """가상 부분 매도 (ratio만큼 물량 매도)"""
        if ticker not in self.positions:
            logger.warning(f"[가상] 부분 매도 실패: {ticker} 보유 없음")
            return None

        pos = self.positions[ticker]
        sell_shares = pos['shares'] * ratio
        sell_price = price * (1 - SLIPPAGE)
        proceeds = sell_shares * sell_price
        commission = proceeds * COMMISSION_PCT

        pnl_pct = (sell_price / pos['avg_price'] - 1) * 100
        pnl_krw = proceeds - (sell_shares * pos['avg_price']) - commission

        self.cash += proceeds - commission
        pos['shares'] -= sell_shares

        trade = {
            'side': 'PARTIAL_SELL',
            'ticker': ticker,
            'price': round(sell_price, 4),
            'shares': round(sell_shares, 4),
            'amount': round(proceeds),
            'commission': round(commission, 2),
            'pnl_pct': round(pnl_pct, 2),
            'pnl_krw': round(pnl_krw),
            'time': datetime.now().isoformat(),
        }
        self.trades.append(trade)
        self.save_state()

        logger.info(f"[가상] 💰 1차 익절 {ticker}: ${sell_price:.2f} ({pnl_pct:+.1f}%) {ratio*100:.0f}% 물량 ₩{pnl_krw:+,.0f}")
        return {
            'ticker': ticker,
            'side': 'PARTIAL_SELL',
            'price': sell_price,
            'shares': sell_shares,
            'pnl_pct': pnl_pct,
            'pnl_krw': pnl_krw,
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
