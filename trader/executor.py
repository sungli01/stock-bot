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

try:
    import redis
except ImportError:
    redis = None
import yaml

from trader.kis_client import KISClient
from trader.market_hours import is_trading_window, is_us_market_open, minutes_until_session_end, get_all_timestamps, get_trading_date

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
        self.base_buy_amount = self.trading_cfg.get("total_buy_amount", 1_000_000)
        self.compound_mode = self.trading_cfg.get("compound_mode", False)
        self.compound_cap = self.trading_cfg.get("compound_cap", 5_000_000)
        self.split_count = self.trading_cfg.get("split_count", 10)
        self.split_interval = self.trading_cfg.get("split_interval_sec", 60)
        self.max_positions = self.trading_cfg.get("max_positions", 5)
        self.take_profit_pct = self.trading_cfg.get("take_profit_pct", 30.0)
        self.stop_loss_pct = self.trading_cfg.get("stop_loss_pct", -15.0)
        self.force_close_before_min = self.trading_cfg.get("force_close_before_min", 15)

        # 트레일링 스탑
        self.trailing_stop = self.trading_cfg.get("trailing_stop", False)
        self.trailing_trigger_pct = self.trading_cfg.get("trailing_trigger_pct", 30.0)
        self.trailing_drop_pct = self.trading_cfg.get("trailing_drop_pct", 10.0)
        self._peak_prices = {}  # ticker → 최고가 추적

        # 복리 누적수익 추적
        self._cumulative_pnl = 0

    @property
    def total_buy_amount(self) -> int:
        """복리 모드: base + 누적수익 (캡 적용)"""
        if not self.compound_mode:
            return self.base_buy_amount
        amount = self.base_buy_amount + max(0, self._cumulative_pnl)
        return min(amount, self.compound_cap)

    def add_pnl(self, pnl: float):
        """매매 완료 후 손익 반영 (복리용)"""
        self._cumulative_pnl += pnl
        logger.info(f"💹 누적 손익: ₩{self._cumulative_pnl:+,.0f} | 다음 투자금: ₩{self.total_buy_amount:,.0f}")

    def execute_buy(self, ticker: str, price: float) -> list[dict]:
        """
        10분할 매수 실행
        1분 간격으로 총매수금액/10 만큼씩 매수
        """
        # KST 18:00~06:00 매매 윈도우 검증
        if not is_trading_window():
            ts = get_all_timestamps()
            logger.warning(f"❌ {ticker} 매수 거부 — 매매 시간 외 (KST {ts['kst']})")
            return []

        # 세션 종료(KST 06:00) 임박 시 매수 차단
        remaining = minutes_until_session_end()
        if 0 < remaining <= self.force_close_before_min:
            logger.warning(f"❌ {ticker} 매수 거부 — 장 마감 {remaining:.0f}분 전 (청산 구간)")
            return []

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

    def execute_sell(self, ticker: str, force: bool = False) -> Optional[dict]:
        """해당 종목 전량 일괄매도. force=True면 시간 검증 스킵(강제청산용)"""
        if not force and not is_trading_window():
            ts = get_all_timestamps()
            logger.warning(f"❌ {ticker} 매도 거부 — 매매 시간 외 (KST {ts['kst']})")
            return None

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
        보유 종목 손절/익절/트레일링스탑 체크
        - -15% 도달: 즉시 손절
        - +30% 도달: 트레일링 스탑 활성화 (최고가 -10% 시 매도)
        - 트레일링 비활성화 시: +30% 즉시 익절
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
                self._peak_prices.pop(ticker, None)
                self.execute_stop_loss(ticker)
                if self.redis is not None:
                    try:
                        self.redis and self.redis.publish("channel:signal", json.dumps({
                            "ticker": ticker,
                            "signal": "STOP",
                            "pnl_pct": round(pnl_pct, 2),
                            "price": current_price,
                        }))
                    except Exception:
                        pass
                continue

            # 트레일링 스탑 로직
            if self.trailing_stop and pnl_pct >= self.trailing_trigger_pct:
                # 최고가 갱신
                prev_peak = self._peak_prices.get(ticker, current_price)
                if current_price > prev_peak:
                    self._peak_prices[ticker] = current_price
                    logger.info(f"📈 {ticker} 최고가 갱신: ${current_price:.2f} ({pnl_pct:+.1f}%)")
                else:
                    # 최고가 대비 하락폭 체크
                    peak = self._peak_prices[ticker]
                    drop_from_peak = ((peak - current_price) / peak) * 100
                    if drop_from_peak >= self.trailing_drop_pct:
                        final_pnl = ((current_price - avg_price) / avg_price) * 100
                        logger.info(f"💰 {ticker} 트레일링스탑 발동! 최고${peak:.2f} → 현재${current_price:.2f} (고점-{drop_from_peak:.1f}%) 최종수익 {final_pnl:+.1f}%")
                        self._peak_prices.pop(ticker, None)
                        self.execute_sell(ticker)
                        if self.redis is not None:
                            try:
                                self.redis and self.redis.publish("channel:signal", json.dumps({
                                    "ticker": ticker,
                                    "signal": "TRAILING_STOP",
                                    "pnl_pct": round(final_pnl, 2),
                                    "peak_price": peak,
                                    "price": current_price,
                                    "timestamps": get_all_timestamps(),
                                }))
                            except Exception:
                                pass
                        continue

            # 트레일링 비활성화 시: 고정 익절
            elif not self.trailing_stop and pnl_pct >= self.take_profit_pct:
                logger.info(f"💰 {ticker} 익절선 도달 ({pnl_pct:.1f}%) — 즉시 매도")
                self.execute_sell(ticker)
                if self.redis is not None:
                    try:
                        self.redis and self.redis.publish("channel:signal", json.dumps({
                            "ticker": ticker,
                            "signal": "TAKE_PROFIT",
                            "pnl_pct": round(pnl_pct, 2),
                            "price": current_price,
                            "timestamps": get_all_timestamps(),
                        }))
                    except Exception:
                        pass

    def force_close_all_positions(self):
        """
        데이트레이딩 강제청산 — 보유 종목 전량 시장가 매도
        장 마감 전 호출. force=True로 시간 검증 스킵.
        """
        balance = self.kis.get_balance()
        positions = balance.get("positions", [])
        if not positions:
            logger.info("💤 강제청산: 보유 종목 없음")
            return

        logger.warning(f"🚨 데이트레이딩 강제청산 시작 — {len(positions)}개 종목")
        for pos in positions:
            ticker = pos["ticker"]
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue
            logger.warning(f"🚨 {ticker} 강제청산: {qty}주 시장가 매도")
            result = self.kis.sell_market(ticker, qty)
            if result and self.redis is not None:
                try:
                    self.redis and self.redis.publish("channel:signal", json.dumps({
                        "ticker": ticker,
                        "signal": "FORCE_CLOSE",
                        "quantity": qty,
                        "timestamps": get_all_timestamps(),
                    }))
                except Exception:
                    pass
        logger.warning("🚨 강제청산 완료")

    def should_force_close(self) -> bool:
        """세션 종료(KST 06:00) 임박 여부 확인"""
        remaining = minutes_until_session_end()
        return 0 < remaining <= self.force_close_before_min

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
