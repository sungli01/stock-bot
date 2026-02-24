"""
실시간 트레이딩 엔진
- 매 1분 Polygon API에서 실시간 데이터 수신
- Feeder → 케이스 분류
- Trader → 매수/매도 신호
- KIS API로 주문 실행
- PAPER_MODE=True/False 환경변수로 제어
"""

import os
import time
import logging
from datetime import datetime, timezone
import pytz
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


class TradingEngine:
    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode or os.environ.get("PAPER_MODE", "true").lower() == "true"
        self.positions = {}       # ticker → {qty, avg_price, entry_time, case_type}
        self.balance = float(os.environ.get("SEED_AMOUNT", 1_000_000))
        self.initial_balance = self.balance
        self.daily_pnl = 0.0
        self.trade_log = []

        from collector.polygon_client import PolygonClient
        from processor.feature_engine import FeatureEngine
        from processor.event_detector import EventDetector
        from processor.case_classifier import CaseClassifier
        from trading.risk_manager import RiskManager
        from reporter.telegram_reporter import TelegramReporter

        self.polygon = PolygonClient()
        self.feature_engine = FeatureEngine()
        self.event_detector = EventDetector()
        self.case_classifier = CaseClassifier()
        self.risk_manager = RiskManager(self.balance)
        self.reporter = TelegramReporter()

        if not self.paper_mode:
            from trading.kis_client import KISClient
            self.kis = KISClient()
        else:
            self.kis = None
            logger.info("📄 PAPER MODE 활성화 — 실제 주문 없음")

    def run(self):
        """메인 트레이딩 루프"""
        logger.info(f"🚀 TradingEngine 시작 (PAPER_MODE={self.paper_mode})")
        self.reporter.send(f"🚀 penny-ai 트레이딩 엔진 시작\nPAPER_MODE={self.paper_mode}\n시드: {self.balance:,.0f}원")

        while True:
            now_et = datetime.now(ET)

            # 장 시간 체크 (프리마켓 04:00 ~ 본장 마감 16:00 ET)
            if not self._is_market_hours(now_et):
                logger.info(f"⏰ 장외 시간 ({now_et.strftime('%H:%M ET')}) — 대기 중...")
                time.sleep(60)
                continue

            try:
                self._trading_cycle()
            except Exception as e:
                logger.error(f"트레이딩 사이클 오류: {e}")
                self.reporter.send(f"⚠️ 트레이딩 오류: {e}")

            time.sleep(60)  # 1분 대기

    def _is_market_hours(self, now_et: datetime) -> bool:
        """프리마켓(04:00) ~ 본장 마감(16:00) ET 체크"""
        if now_et.weekday() >= 5:  # 주말
            return False
        hour = now_et.hour
        return 4 <= hour < 16

    def _trading_cycle(self):
        """1분 사이클: 데이터 수집 → 신호 생성 → 주문"""
        now_et = datetime.now(ET)
        date_str = now_et.strftime("%Y-%m-%d")

        # 1. 감시 종목 결정 (당일 상승률 상위 10종목)
        watchlist = self._get_watchlist(date_str)
        if not watchlist:
            return

        for ticker in watchlist:
            try:
                self._process_ticker(ticker, date_str, now_et)
            except Exception as e:
                logger.error(f"{ticker} 처리 오류: {e}")

        # 포지션 모니터링
        self._monitor_positions()

    def _get_watchlist(self, date_str: str) -> list:
        """당일 상승률 상위 10종목 반환"""
        try:
            gainers = self.polygon.get_top_gainers(date_str, min_price=0.5, max_price=50, top_n=10)
            return [g["ticker"] for g in gainers]
        except Exception as e:
            logger.error(f"워치리스트 조회 실패: {e}")
            return []

    def _process_ticker(self, ticker: str, date_str: str, now_et: datetime):
        """종목별 신호 처리"""
        # 1분봉 수집
        bars = self.polygon.get_intraday_1m(ticker, date_str)
        if bars is None or len(bars) < 20:
            return

        # 피처 계산
        features = self.feature_engine.compute(bars)

        # 이벤트 감지
        events = self.event_detector.detect(features)

        # 케이스 분류
        case = self.case_classifier.classify(events, features)

        # 리스크 체크
        if not self.risk_manager.can_trade(ticker, self.balance, self.daily_pnl):
            return

        # 매수 신호
        if ticker not in self.positions:
            if case["type"] in ["A", "B", "E"] and case.get("second_surge_confirmed"):
                self._buy(ticker, features["current_price"], case)

        # 매도 신호
        elif ticker in self.positions:
            self._check_sell(ticker, features["current_price"], case)

    def _buy(self, ticker: str, price: float, case: dict):
        """매수 실행"""
        amount = self.risk_manager.calc_position_size(self.balance)
        qty = int(amount / price)
        if qty <= 0:
            return

        cost = qty * price * (1 + 0.001)  # 수수료 0.1%

        if self.paper_mode:
            self.balance -= cost
            self.positions[ticker] = {
                "qty": qty,
                "avg_price": price,
                "entry_time": datetime.now(ET),
                "case_type": case["type"],
                "peak_price": price,
                "cost": cost
            }
            logger.info(f"📈 [PAPER] BUY {ticker} {qty}주 @ ${price:.4f} (케이스 {case['type']})")
            self.reporter.send(
                f"📈 매수 신호 [{case['type']}형]\n"
                f"종목: {ticker}\n"
                f"가격: ${price:.4f}\n"
                f"수량: {qty}주\n"
                f"금액: {cost:,.0f}원\n"
                f"PAPER MODE"
            )
        else:
            # 실전 KIS API 주문
            result = self.kis.buy_market_order(ticker, qty)
            if result:
                self.positions[ticker] = {
                    "qty": qty,
                    "avg_price": price,
                    "entry_time": datetime.now(ET),
                    "case_type": case["type"],
                    "peak_price": price,
                    "cost": cost
                }

    def _check_sell(self, ticker: str, current_price: float, case: dict):
        """매도 조건 체크"""
        pos = self.positions[ticker]
        avg_price = pos["avg_price"]
        case_type = pos["case_type"]
        pnl_pct = (current_price - avg_price) / avg_price * 100

        # 피크 가격 업데이트
        if current_price > pos["peak_price"]:
            pos["peak_price"] = current_price

        peak_price = pos["peak_price"]
        drop_from_peak = (current_price - peak_price) / peak_price * 100

        # 매도 조건
        should_sell = False
        sell_reason = ""

        # A형: 피크 -5% 트레일링
        if case_type == "A" and drop_from_peak <= -5.0:
            should_sell = True
            sell_reason = "A형 트레일링 -5%"

        # B형: 피크 -3% 빠른 이탈
        elif case_type == "B" and drop_from_peak <= -3.0:
            should_sell = True
            sell_reason = "B형 트레일링 -3%"

        # E형: 피크 -5% 트레일링
        elif case_type == "E" and drop_from_peak <= -5.0:
            should_sell = True
            sell_reason = "E형 트레일링 -5%"

        # 손절: -7%
        elif pnl_pct <= -7.0:
            should_sell = True
            sell_reason = "손절 -7%"

        # 시간 초과: 60분
        elif (datetime.now(ET) - pos["entry_time"]).seconds >= 3600:
            should_sell = True
            sell_reason = "60분 시간초과"

        if should_sell:
            self._sell(ticker, current_price, sell_reason, pnl_pct)

    def _sell(self, ticker: str, price: float, reason: str, pnl_pct: float):
        """매도 실행"""
        pos = self.positions[ticker]
        qty = pos["qty"]
        revenue = qty * price * (1 - 0.001)  # 수수료 0.1%
        pnl = revenue - pos["cost"]
        self.daily_pnl += pnl

        if self.paper_mode:
            self.balance += revenue
            emoji = "✅" if pnl > 0 else "❌"
            logger.info(f"📉 [PAPER] SELL {ticker} {qty}주 @ ${price:.4f} PnL: {pnl_pct:+.2f}%")
            self.reporter.send(
                f"{emoji} 매도 [{reason}]\n"
                f"종목: {ticker}\n"
                f"가격: ${price:.4f}\n"
                f"수익률: {pnl_pct:+.2f}%\n"
                f"손익: {pnl:+,.0f}원\n"
                f"잔고: {self.balance:,.0f}원"
            )

            self.trade_log.append({
                "ticker": ticker,
                "entry_price": pos["avg_price"],
                "exit_price": price,
                "qty": qty,
                "pnl_pct": pnl_pct,
                "pnl": pnl,
                "case_type": pos["case_type"],
                "reason": reason,
                "entry_time": pos["entry_time"].isoformat(),
                "exit_time": datetime.now(ET).isoformat()
            })
        else:
            self.kis.sell_market_order(ticker, qty)
            self.balance += revenue

        del self.positions[ticker]

    def _monitor_positions(self):
        """포지션 모니터링 (일일 손실 한도 체크)"""
        total_value = self.balance
        for ticker, pos in self.positions.items():
            total_value += pos["qty"] * pos.get("peak_price", pos["avg_price"])

        daily_return = (total_value - self.initial_balance) / self.initial_balance * 100

        if daily_return <= -5.0:
            logger.warning(f"⚠️ 일일 손실 한도 -5% 도달! 거래 중단")
            self.reporter.send(f"⚠️ 일일 손실 한도 도달 (-5%)\n모든 포지션 청산 후 거래 중단")
            # 모든 포지션 강제 청산
            for ticker in list(self.positions.keys()):
                pos = self.positions[ticker]
                self._sell(ticker, pos.get("peak_price", pos["avg_price"]), "일일손실한도", -5.0)
