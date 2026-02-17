"""
KIS 한국투자증권 API 클라이언트 (Stub 구현)
- 해외주식 시장가 주문
- 잔고 조회
- 현재가 조회
- 실제 API 호출은 키 없이 stub으로 구현, 인터페이스만 확립
"""
import os
import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
KIS_IS_VIRTUAL = os.getenv("KIS_IS_VIRTUAL", "true").lower() == "true"
USE_STUB = not KIS_APP_KEY or KIS_APP_KEY == "your_kis_app_key_here"


class KISClient:
    """KIS OpenAPI 클라이언트 (해외주식)"""

    def __init__(self):
        if not USE_STUB:
            # 실제 KIS SDK 연결
            try:
                import pykis
                self.kis = pykis.PyKis(
                    id=KIS_ACCOUNT_NO,
                    key=KIS_APP_KEY,
                    secret=KIS_APP_SECRET,
                    virtual=KIS_IS_VIRTUAL,
                )
                logger.info("✅ KIS API 연결 완료")
            except Exception as e:
                logger.error(f"KIS API 연결 실패: {e}")
                self.kis = None
        else:
            self.kis = None
            logger.warning("⚠️ KIS API 키 없음 — stub 모드로 실행")

        # Stub 포지션 관리
        self._stub_positions: dict[str, dict] = {}
        self._stub_balance: float = 1_000_000  # 100만원

    # ─── 주문 ──────────────────────────────────────────────
    def buy_market(self, ticker: str, quantity: int) -> Optional[dict]:
        """
        해외주식 시장가 매수
        Returns: {"order_id", "ticker", "quantity", "filled_price", "filled_at"}
        """
        if USE_STUB:
            return self._stub_buy(ticker, quantity)

        try:
            # 실제 KIS API 주문 (pykis 라이브러리)
            order = self.kis.order(
                market="US",
                ticker=ticker,
                side="buy",
                quantity=quantity,
                price=0,  # 시장가
            )
            return {
                "order_id": str(order.order_id),
                "ticker": ticker,
                "quantity": quantity,
                "filled_price": order.filled_price,
                "filled_at": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"매수 주문 실패 [{ticker}]: {e}")
            return None

    def sell_market(self, ticker: str, quantity: int) -> Optional[dict]:
        """
        해외주식 시장가 매도
        Returns: {"order_id", "ticker", "quantity", "filled_price", "filled_at"}
        """
        if USE_STUB:
            return self._stub_sell(ticker, quantity)

        try:
            order = self.kis.order(
                market="US",
                ticker=ticker,
                side="sell",
                quantity=quantity,
                price=0,
            )
            return {
                "order_id": str(order.order_id),
                "ticker": ticker,
                "quantity": quantity,
                "filled_price": order.filled_price,
                "filled_at": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"매도 주문 실패 [{ticker}]: {e}")
            return None

    # ─── 잔고 ──────────────────────────────────────────────
    def get_balance(self) -> dict:
        """
        잔고 조회
        Returns: {"cash": float, "positions": [{"ticker", "quantity", "avg_price", "current_price"}]}
        """
        if USE_STUB:
            return self._stub_get_balance()

        try:
            balance = self.kis.balance()
            positions = []
            for p in balance.positions:
                positions.append({
                    "ticker": p.ticker,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "current_price": p.current_price,
                })
            return {"cash": balance.cash, "positions": positions}
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return {"cash": 0, "positions": []}

    def get_current_price(self, ticker: str) -> Optional[float]:
        """현재가 조회"""
        if USE_STUB:
            return self._stub_price(ticker)

        try:
            quote = self.kis.quote(market="US", ticker=ticker)
            return quote.price
        except Exception as e:
            logger.error(f"현재가 조회 실패 [{ticker}]: {e}")
            return None

    # ─── Stub 메서드 ───────────────────────────────────────
    def _stub_buy(self, ticker: str, quantity: int) -> dict:
        import random
        price = self._stub_price(ticker) or 50.0
        total = price * quantity

        if total > self._stub_balance:
            logger.warning(f"[STUB] 잔고 부족: 필요 ${total:.2f}, 보유 ${self._stub_balance:.2f}")
            return None

        self._stub_balance -= total
        if ticker in self._stub_positions:
            pos = self._stub_positions[ticker]
            old_total = pos["avg_price"] * pos["quantity"]
            pos["quantity"] += quantity
            pos["avg_price"] = (old_total + total) / pos["quantity"]
        else:
            self._stub_positions[ticker] = {
                "quantity": quantity,
                "avg_price": price,
            }

        order_id = str(uuid.uuid4())[:8]
        logger.info(f"[STUB] 매수 체결: {ticker} x{quantity} @ ${price:.2f} (주문ID: {order_id})")
        return {
            "order_id": order_id,
            "ticker": ticker,
            "quantity": quantity,
            "filled_price": price,
            "filled_at": datetime.utcnow().isoformat(),
        }

    def _stub_sell(self, ticker: str, quantity: int) -> dict:
        price = self._stub_price(ticker) or 50.0

        if ticker in self._stub_positions:
            pos = self._stub_positions[ticker]
            pos["quantity"] -= quantity
            if pos["quantity"] <= 0:
                del self._stub_positions[ticker]

        self._stub_balance += price * quantity

        order_id = str(uuid.uuid4())[:8]
        logger.info(f"[STUB] 매도 체결: {ticker} x{quantity} @ ${price:.2f} (주문ID: {order_id})")
        return {
            "order_id": order_id,
            "ticker": ticker,
            "quantity": quantity,
            "filled_price": price,
            "filled_at": datetime.utcnow().isoformat(),
        }

    def _stub_get_balance(self) -> dict:
        positions = []
        for ticker, pos in self._stub_positions.items():
            current = self._stub_price(ticker) or pos["avg_price"]
            positions.append({
                "ticker": ticker,
                "quantity": pos["quantity"],
                "avg_price": pos["avg_price"],
                "current_price": current,
            })
        return {"cash": self._stub_balance, "positions": positions}

    def _stub_price(self, ticker: str) -> float:
        """모의 현재가"""
        import random
        base_prices = {
            "NVDA": 142.5, "AAPL": 185.3, "TSLA": 250.0,
            "AMD": 165.0, "PLTR": 25.0, "SOFI": 10.5,
        }
        base = base_prices.get(ticker, 50.0)
        return round(base * (1 + random.uniform(-0.02, 0.02)), 2)
