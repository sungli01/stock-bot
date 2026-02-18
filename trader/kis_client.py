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
