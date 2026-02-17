"""
매매 실행 모듈
- Redis channel:signal subscribe
- BUY: 10분할 매수 (1분 간격)
- SELL: 일괄매도
- STOP: 즉시 손절
- 잔고 체크 (총매수금액 100만원 제한)
"""
import json
import time
import logging
from typing import Optional

import redis
import yaml

from trader.kis_client import KISClient

logger = logging.getLogger(__name__)


class TradeExecutor:
    """매매 실행기 — 시그널 수신 후 자동 매매"""

    def __init__(self, redis_client, config: Optional[dict] = None):
        self.redis = redis_client
        if config is None:
            with open("config/config.yaml", "r") as f:
                config = yaml.safe_load(f)
        self.config = config
        self.trading_cfg = config.get("trading", {})
        self.kis = KISClient()

        # 설정값
        self.total_buy_amount = self.trading_cfg.get("total_buy_amount", 1_000_000)
        self.split_count = self.trading_cfg.get("split_count", 10)
        self.split_interval = self.trading_cfg.get("split_interval_sec", 60)
        self.max_positions = self.trading_cfg.get("max_positions", 5)
        self.take_profit_pct = self.trading_cfg.get("take_profit_pct", 30.0)
        self.stop_loss_pct = self.trading_cfg.get("stop_loss_pct", -15.0)

    def execute_buy(self, ticker: str, price: float) -> list[dict]:
        """
        10분할 매수 실행
        1분 간격으로 총매수금액/10 만큼씩 매수
        """
        # 동시 보유 종목 수 체크
        balance = self.kis.get_balance()
        current_positions = len(balance.get("positions", []))
        if current_positions >= self.max_positions:
            logger.warning(f"❌ 최대 보유 종목 수 초과 ({current_positions}/{self.max_positions})")
            return []

        # 분할 매수 금액 계산
        per_split = self.total_buy_amount / self.split_count
        quantity_per_split = max(1, int(per_split / (price * 1350)))  # 원화→달러 환산 (약 1350원/$)

        orders = []
        for i in range(self.split_count):
            logger.info(f"📈 {ticker} 분할매수 {i+1}/{self.split_count} — {quantity_per_split}주")

            order = self.kis.buy_market(ticker, quantity_per_split)
            if order:
                order["split_index"] = i + 1
                orders.append(order)
            else:
                logger.error(f"  ❌ {i+1}번째 매수 실패 — 중단")
                break

            # 마지막이 아니면 대기
            if i < self.split_count - 1:
                time.sleep(self.split_interval)

        logger.info(f"✅ {ticker} 매수 완료: {len(orders)}/{self.split_count}건 체결")
        return orders

    def execute_sell(self, ticker: str) -> Optional[dict]:
        """해당 종목 전량 일괄매도"""
        balance = self.kis.get_balance()
        position = None
        for p in balance.get("positions", []):
            if p["ticker"] == ticker:
                position = p
                break

        if not position or position["quantity"] <= 0:
            logger.warning(f"❌ {ticker} 보유 수량 없음 — 매도 불가")
            return None

        logger.info(f"📉 {ticker} 일괄매도: {position['quantity']}주")
        return self.kis.sell_market(ticker, position["quantity"])

    def execute_stop_loss(self, ticker: str) -> Optional[dict]:
        """긴급 손절 — 즉시 전량 매도"""
        logger.warning(f"🚨 {ticker} 손절 실행!")
        return self.execute_sell(ticker)

    def check_positions(self):
        """
        보유 종목 손절/익절 체크
        - +30% 도달: 추세 확인 후 매도
        - -15% 도달: 즉시 손절
        """
        balance = self.kis.get_balance()
        for pos in balance.get("positions", []):
            ticker = pos["ticker"]
            avg_price = pos["avg_price"]
            current_price = pos.get("current_price") or self.kis.get_current_price(ticker)

            if not current_price or not avg_price:
                continue

            pnl_pct = ((current_price - avg_price) / avg_price) * 100

            # 손절 체크
            if pnl_pct <= self.stop_loss_pct:
                logger.warning(f"🚨 {ticker} 손절선 도달 ({pnl_pct:.1f}%)")
                self.execute_stop_loss(ticker)
                # 손절 시그널을 Redis로 publish (알림용)
                if self.redis is not None:
                    try:
                        self.redis.publish("channel:signal", json.dumps({
                            "ticker": ticker,
                            "signal": "STOP",
                            "pnl_pct": round(pnl_pct, 2),
                            "price": current_price,
                        }))
                    except Exception:
                        pass

            # 익절 체크
            elif pnl_pct >= self.take_profit_pct:
                logger.info(f"💰 {ticker} 익절선 도달 ({pnl_pct:.1f}%) — 추세 확인 필요")
                # 추세 확인은 Analyzer에 요청 (여기서는 매도 시그널만 publish)
                if self.redis is not None:
                    try:
                        self.redis.publish("channel:signal", json.dumps({
                            "ticker": ticker,
                            "signal": "TAKE_PROFIT_CHECK",
                            "pnl_pct": round(pnl_pct, 2),
                            "price": current_price,
                        }))
                    except Exception:
                        pass

    def run_subscriber(self):
        """
        Redis channel:signal 구독 → 매매 실행
        """
        logger.info("📡 매매 실행기 시작 — channel:signal 구독 중...")
        pubsub = self.redis.pubsub()
        pubsub.subscribe("channel:signal")

        for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
                ticker = data.get("ticker")
                signal = data.get("signal")
                price = data.get("price", 0)

                if not ticker or not signal:
                    continue

                if signal == "BUY":
                    self.execute_buy(ticker, price)
                elif signal == "SELL":
                    self.execute_sell(ticker)
                elif signal == "STOP":
                    self.execute_stop_loss(ticker)

            except Exception as e:
                logger.error(f"매매 실행 오류: {e}", exc_info=True)
