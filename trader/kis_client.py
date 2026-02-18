"""
KIS 한국투자증권 REST API 클라이언트
- 해외주식 시장가 주문 (매수/매도)
- 잔고 조회
- 토큰 자동 발급/갱신
"""
import os
import logging
import requests
import json
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")
KIS_ACCOUNT_PRODUCT = os.getenv("KIS_ACCOUNT_PRODUCT", "01")
KIS_IS_VIRTUAL = os.getenv("KIS_IS_VIRTUAL", "true").lower() == "true"

# 실전 vs 모의
BASE_URL = "https://openapivts.koreainvestment.com:29443" if KIS_IS_VIRTUAL else "https://openapi.koreainvestment.com:9443"


class KISClient:
    """KIS OpenAPI REST 클라이언트 (해외주식)"""

    def __init__(self):
        self.access_token = None
        self.token_expires = 0
        self.connected = False

        if KIS_APP_KEY and KIS_APP_KEY != "your_kis_app_key_here":
            try:
                self._get_token()
                self.connected = True
                logger.info(f"✅ KIS API 연결 완료 ({'모의' if KIS_IS_VIRTUAL else '실전'})")
            except Exception as e:
                logger.error(f"KIS API 연결 실패: {e}")
        else:
            logger.warning("⚠️ KIS API 키 없음 — stub 모드")

    def _get_token(self):
        """OAuth 토큰 발급"""
        url = f"{BASE_URL}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        }
        r = requests.post(url, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        self.access_token = data["access_token"]
        self.token_expires = time.time() + int(data.get("expires_in", 86400)) - 60
        logger.info("KIS 토큰 발급 완료")

    def _ensure_token(self):
        """토큰 만료 시 자동 갱신"""
        if time.time() >= self.token_expires:
            self._get_token()

    def _headers(self, tr_id: str) -> dict:
        """공통 헤더"""
        self._ensure_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
        }

    # ─── 주문 ──────────────────────────────────────────────
    def buy_market(self, ticker: str, quantity: int) -> Optional[dict]:
        """해외주식 시장가 매수"""
        if not self.connected:
            logger.warning(f"[STUB] 매수: {ticker} x{quantity}")
            return {"order_id": "stub", "ticker": ticker, "quantity": quantity,
                    "filled_price": 0, "filled_at": datetime.now(timezone.utc).isoformat()}

        # 해외주식 매수: JTTT1002U (실전) / VTTT1002U (모의)
        tr_id = "VTTT1002U" if KIS_IS_VIRTUAL else "JTTT1002U"
        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "OVRS_EXCG_CD": "NASD",  # 나스닥 (NYSE는 "NYSE", AMEX는 "AMEX")
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",  # 시장가
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 시장가 주문
        }
        try:
            r = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
            data = r.json()
            if data.get("rt_cd") == "0":
                order_no = data.get("output", {}).get("ODNO", "unknown")
                logger.info(f"✅ 매수 주문 성공: {ticker} x{quantity} (주문번호: {order_no})")
                return {
                    "order_id": order_no,
                    "ticker": ticker,
                    "quantity": quantity,
                    "filled_price": 0,  # 체결가는 별도 조회 필요
                    "filled_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                msg = data.get("msg1", data.get("msg", "unknown error"))
                logger.error(f"❌ 매수 실패 [{ticker}]: {msg}")
                return None
        except Exception as e:
            logger.error(f"❌ 매수 주문 예외 [{ticker}]: {e}")
            return None

    def sell_market(self, ticker: str, quantity: int) -> Optional[dict]:
        """해외주식 시장가 매도"""
        if not self.connected:
            logger.warning(f"[STUB] 매도: {ticker} x{quantity}")
            return {"order_id": "stub", "ticker": ticker, "quantity": quantity,
                    "filled_price": 0, "filled_at": datetime.now(timezone.utc).isoformat()}

        # 해외주식 매도: JTTT1006U (실전) / VTTT1001U (모의)
        tr_id = "VTTT1001U" if KIS_IS_VIRTUAL else "JTTT1006U"
        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "OVRS_EXCG_CD": "NASD",
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        try:
            r = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
            data = r.json()
            if data.get("rt_cd") == "0":
                order_no = data.get("output", {}).get("ODNO", "unknown")
                logger.info(f"✅ 매도 주문 성공: {ticker} x{quantity} (주문번호: {order_no})")
                return {
                    "order_id": order_no,
                    "ticker": ticker,
                    "quantity": quantity,
                    "filled_price": 0,
                    "filled_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                msg = data.get("msg1", data.get("msg", "unknown error"))
                logger.error(f"❌ 매도 실패 [{ticker}]: {msg}")
                return None
        except Exception as e:
            logger.error(f"❌ 매도 주문 예외 [{ticker}]: {e}")
            return None

    # ─── 잔고 ──────────────────────────────────────────────
    def get_balance(self) -> dict:
        """해외주식 잔고 조회"""
        if not self.connected:
            return {"cash": 1_000_000, "positions": []}

        # JTTT3012R (실전) / VTTS3012R (모의)
        tr_id = "VTTS3012R" if KIS_IS_VIRTUAL else "JTTT3012R"
        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            data = r.json()
            positions = []
            for item in data.get("output1", []):
                if int(item.get("OVRS_CBLC_QTY", "0")) > 0:
                    positions.append({
                        "ticker": item.get("OVRS_PDNO", ""),
                        "quantity": int(item.get("OVRS_CBLC_QTY", "0")),
                        "avg_price": float(item.get("PCH_AMT", "0")),
                        "current_price": float(item.get("NOW_PRIC2", "0")),
                    })
            # output2에서 예수금
            output2 = data.get("output2", {})
            cash_usd = float(output2.get("FRCR_PCHS_AMT1", "0")) if isinstance(output2, dict) else 0
            return {"cash": cash_usd, "positions": positions}
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
            return {"cash": 0, "positions": []}

    # ─── 지정가 주문 ─────────────────────────────────────────
    def _place_limit_order(self, side: str, ticker: str, quantity: int, price: float) -> Optional[dict]:
        """해외주식 지정가 주문 (side: 'BUY' or 'SELL')"""
        if not self.connected:
            logger.warning(f"[STUB] 지정가 {side}: {ticker} x{quantity} @{price}")
            return {"order_id": "stub", "ticker": ticker, "quantity": quantity,
                    "limit_price": price, "filled_price": price,
                    "filled_at": datetime.now(timezone.utc).isoformat()}

        if side == "BUY":
            tr_id = "VTTT1002U" if KIS_IS_VIRTUAL else "JTTT1002U"
        else:
            tr_id = "VTTT1001U" if KIS_IS_VIRTUAL else "JTTT1006U"

        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "OVRS_EXCG_CD": "NASD",
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 지정가
        }
        try:
            r = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
            data = r.json()
            if data.get("rt_cd") == "0":
                order_no = data.get("output", {}).get("ODNO", "unknown")
                logger.info(f"✅ 지정가 {side} 주문: {ticker} x{quantity} @${price:.2f} (#{order_no})")
                return {
                    "order_id": order_no,
                    "ticker": ticker,
                    "quantity": quantity,
                    "limit_price": price,
                    "filled_price": 0,
                    "filled_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                msg = data.get("msg1", data.get("msg", "unknown error"))
                logger.error(f"❌ 지정가 {side} 실패 [{ticker}]: {msg}")
                return None
        except Exception as e:
            logger.error(f"❌ 지정가 {side} 예외 [{ticker}]: {e}")
            return None

    def _cancel_order(self, order_id: str, ticker: str) -> bool:
        """해외주식 주문 취소"""
        if not self.connected:
            logger.warning(f"[STUB] 주문 취소: {order_id}")
            return True

        # 정정취소: JTTT1004U (실전) / VTTT1004U (모의)
        tr_id = "VTTT1004U" if KIS_IS_VIRTUAL else "JTTT1004U"
        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        body = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "OVRS_EXCG_CD": "NASD",
            "PDNO": ticker,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": "0",  # 잔량 전부
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        try:
            r = requests.post(url, headers=self._headers(tr_id), json=body, timeout=10)
            data = r.json()
            if data.get("rt_cd") == "0":
                logger.info(f"✅ 주문 취소 성공: {order_id}")
                return True
            else:
                msg = data.get("msg1", data.get("msg", "unknown error"))
                logger.warning(f"⚠️ 주문 취소 실패 [{order_id}]: {msg}")
                return False
        except Exception as e:
            logger.error(f"❌ 주문 취소 예외: {e}")
            return False

    def _check_order_filled(self, order_id: str, ticker: str) -> Optional[dict]:
        """주문 체결 여부 확인. 체결 시 {'filled': True, 'price': float, 'qty': int}"""
        if not self.connected:
            return {"filled": True, "price": 100.0, "qty": 1}

        # 체결 조회: JTTT3001R (실전) / VTTS3001R (모의) — 주문별 체결 내역
        tr_id = "VTTS3001R" if KIS_IS_VIRTUAL else "JTTT3001R"
        url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-ccnl"
        params = {
            "CANO": KIS_ACCOUNT_NO,
            "ACNT_PRDT_CD": KIS_ACCOUNT_PRODUCT,
            "PDNO": ticker,
            "ORD_STRT_DT": datetime.now(timezone.utc).strftime("%Y%m%d"),
            "ORD_END_DT": datetime.now(timezone.utc).strftime("%Y%m%d"),
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "01",  # 체결만
            "OVRS_EXCG_CD": "NASD",
            "SORT_SQN": "DS",
            "ORD_GNO_BRNO": "",
            "ODNO": order_id,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            data = r.json()
            for item in data.get("output1", []):
                if item.get("ODNO") == order_id or item.get("ORGN_ODNO") == order_id:
                    filled_qty = int(item.get("FLL_QTY", "0") or item.get("TOT_CCLD_QTY", "0"))
                    filled_price = float(item.get("FLL_AMT", "0") or item.get("OVRS_EXCG_UNPR", "0"))
                    if filled_qty > 0 and filled_price > 0:
                        return {"filled": True, "price": filled_price, "qty": filled_qty}
            return {"filled": False, "price": 0, "qty": 0}
        except Exception as e:
            logger.error(f"체결 조회 실패 [{order_id}]: {e}")
            return None

    def get_ask_price(self, ticker: str) -> Optional[float]:
        """해외주식 매도호가(ask) 조회"""
        # 현재가 조회로 대체 (KIS 호가 API 제한적)
        return self.get_current_price(ticker)

    # ─── 3분할 매수 ────────────────────────────────────────
    def buy_split(self, ticker: str, total_quantity: int) -> list[dict]:
        """
        3분할 지정가 매수
        1차 (40%): 현재 ask 가격 지정가 → 즉시
        2차 (35%): 1차 체결 확인 후 5초 대기 → 체결가 +0.5% 지정가
        3차 (25%): 2차 체결 후 10초 대기 → 가격 확인 후 진입/취소 판단
        미체결 15초 후 잔여 주문 취소
        """
        orders = []
        qty1 = max(1, int(total_quantity * 0.40))
        qty2 = max(1, int(total_quantity * 0.35))
        qty3 = max(1, total_quantity - qty1 - qty2)

        # ── 1차 매수 (40%) ────────────────────────────
        ask_price = self.get_ask_price(ticker)
        if not ask_price:
            logger.error(f"❌ {ticker} 호가 조회 실패 — 분할매수 중단")
            return orders

        logger.info(f"📈 {ticker} 분할매수 1/3: {qty1}주 @${ask_price:.2f} (40%)")
        order1 = self._place_limit_order("BUY", ticker, qty1, ask_price)
        if not order1:
            return orders

        # 1차 체결 대기 (최대 15초)
        fill1 = self._wait_for_fill(order1["order_id"], ticker, timeout=15)
        if not fill1 or not fill1.get("filled"):
            logger.warning(f"⚠️ {ticker} 1차 미체결 — 취소 후 중단")
            self._cancel_order(order1["order_id"], ticker)
            return orders
        order1["filled_price"] = fill1["price"]
        orders.append(order1)

        # ── 2차 매수 (35%) ────────────────────────────
        time.sleep(5)
        price2 = round(fill1["price"] * 1.005, 2)  # 체결가 +0.5%
        logger.info(f"📈 {ticker} 분할매수 2/3: {qty2}주 @${price2:.2f} (35%, +0.5%)")
        order2 = self._place_limit_order("BUY", ticker, qty2, price2)
        if not order2:
            return orders

        fill2 = self._wait_for_fill(order2["order_id"], ticker, timeout=15)
        if not fill2 or not fill2.get("filled"):
            logger.warning(f"⚠️ {ticker} 2차 미체결 — 취소")
            self._cancel_order(order2["order_id"], ticker)
            return orders
        order2["filled_price"] = fill2["price"]
        orders.append(order2)

        # ── 3차 매수 (25%) ────────────────────────────
        time.sleep(10)
        current_price = self.get_current_price(ticker)
        if not current_price:
            logger.warning(f"⚠️ {ticker} 3차 가격 조회 실패 — 스킵")
            return orders

        # 3차 진입 판단: 현재가가 평균 체결가 대비 +2% 이내면 진입
        avg_filled = (fill1["price"] * qty1 + fill2["price"] * qty2) / (qty1 + qty2)
        if current_price > avg_filled * 1.02:
            logger.info(f"⚠️ {ticker} 3차 진입 취소 — 가격 급등 (현재 ${current_price:.2f} vs 평균 ${avg_filled:.2f})")
            return orders

        price3 = round(current_price, 2)
        logger.info(f"📈 {ticker} 분할매수 3/3: {qty3}주 @${price3:.2f} (25%)")
        order3 = self._place_limit_order("BUY", ticker, qty3, price3)
        if not order3:
            return orders

        fill3 = self._wait_for_fill(order3["order_id"], ticker, timeout=15)
        if not fill3 or not fill3.get("filled"):
            logger.warning(f"⚠️ {ticker} 3차 미체결 — 취소")
            self._cancel_order(order3["order_id"], ticker)
            return orders
        order3["filled_price"] = fill3["price"]
        orders.append(order3)

        logger.info(f"✅ {ticker} 3분할 매수 완료: {len(orders)}/3건 체결")
        return orders

    # ─── 2분할 매도 ────────────────────────────────────────
    def sell_split(self, ticker: str, total_quantity: int) -> list[dict]:
        """
        2분할 매도
        1차 (60%): 시장가 즉시
        2차 (40%): 30초 대기 후 지정가 (1차 체결가 이상), 하락시 시장가 전환
        """
        orders = []
        qty1 = max(1, int(total_quantity * 0.60))
        qty2 = max(1, total_quantity - qty1)

        # ── 1차 매도 (60%) 시장가 ─────────────────────
        logger.info(f"📉 {ticker} 분할매도 1/2: {qty1}주 시장가 (60%)")
        order1 = self.sell_market(ticker, qty1)
        if not order1:
            logger.error(f"❌ {ticker} 1차 매도 실패")
            # 실패 시 전량 시장가 시도
            fallback = self.sell_market(ticker, total_quantity)
            if fallback:
                orders.append(fallback)
            return orders
        orders.append(order1)

        # 1차 체결가 확인
        time.sleep(2)
        fill1 = self._check_order_filled(order1["order_id"], ticker)
        fill1_price = fill1["price"] if fill1 and fill1.get("filled") else 0

        # ── 2차 매도 (40%) 30초 대기 ──────────────────
        time.sleep(30)
        if fill1_price > 0:
            # 현재가 확인
            current_price = self.get_current_price(ticker)
            if current_price and current_price >= fill1_price:
                # 지정가 매도 (1차 체결가 이상)
                limit_price = round(fill1_price, 2)
                logger.info(f"📉 {ticker} 분할매도 2/2: {qty2}주 지정가 @${limit_price:.2f} (40%)")
                order2 = self._place_limit_order("SELL", ticker, qty2, limit_price)
                if order2:
                    fill2 = self._wait_for_fill(order2["order_id"], ticker, timeout=15)
                    if fill2 and fill2.get("filled"):
                        order2["filled_price"] = fill2["price"]
                        orders.append(order2)
                    else:
                        # 미체결 → 시장가 전환
                        logger.warning(f"⚠️ {ticker} 2차 지정가 미체결 → 시장가 전환")
                        self._cancel_order(order2["order_id"], ticker)
                        order2_market = self.sell_market(ticker, qty2)
                        if order2_market:
                            orders.append(order2_market)
                    return orders

            # 하락 시 시장가 전환
            logger.info(f"📉 {ticker} 분할매도 2/2: {qty2}주 시장가 (하락 감지)")
            order2 = self.sell_market(ticker, qty2)
        else:
            # 1차 체결가 불명 → 시장가
            logger.info(f"📉 {ticker} 분할매도 2/2: {qty2}주 시장가")
            order2 = self.sell_market(ticker, qty2)

        if order2:
            orders.append(order2)

        logger.info(f"✅ {ticker} 2분할 매도 완료: {len(orders)}/2건")
        return orders

    def _wait_for_fill(self, order_id: str, ticker: str, timeout: int = 15) -> Optional[dict]:
        """주문 체결 대기 (polling). timeout 초 후 미체결 반환"""
        elapsed = 0
        interval = 1.5
        while elapsed < timeout:
            result = self._check_order_filled(order_id, ticker)
            if result and result.get("filled"):
                return result
            time.sleep(interval)
            elapsed += interval
        return {"filled": False, "price": 0, "qty": 0}

    # ─── 잔고 ──────────────────────────────────────────────
    def get_current_price(self, ticker: str) -> Optional[float]:
        """해외주식 현재가 (Polygon snapshot 사용 권장, 이건 백업용)"""
        if not self.connected:
            return None

        tr_id = "HHDFS00000300"
        url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"
        params = {
            "AUTH": "",
            "EXCD": "NAS",
            "SYMB": ticker,
        }
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            data = r.json()
            price = float(data.get("output", {}).get("LAST", "0"))
            return price if price > 0 else None
        except Exception as e:
            logger.error(f"현재가 조회 실패 [{ticker}]: {e}")
            return None
