"""
한국투자증권 API 클라이언트
미국 주식 매매 지원
"""

import os
import json
import time
import logging
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

KIS_BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
KIS_BASE_URL_VIRTUAL = "https://openapivts.koreainvestment.com:29443"


class KISClient:
    """한국투자증권 API 클라이언트 (미국 주식)"""

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        account_no: Optional[str] = None,
        account_product: Optional[str] = None,
        is_virtual: Optional[bool] = None,
    ):
        self.app_key = app_key or os.environ.get("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("KIS_APP_SECRET", "")
        self.account_no = account_no or os.environ.get("KIS_ACCOUNT_NO", "")
        self.account_product = account_product or os.environ.get("KIS_ACCOUNT_PRODUCT", "01")

        if is_virtual is None:
            is_virtual_str = os.environ.get("KIS_IS_VIRTUAL", "true").lower()
            self.is_virtual = is_virtual_str in ("true", "1", "yes")
        else:
            self.is_virtual = is_virtual

        self.base_url = KIS_BASE_URL_VIRTUAL if self.is_virtual else KIS_BASE_URL_REAL
        self._access_token = None
        self._token_expires_at = None

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/plain",
        })

        logger.info(f"KIS API 초기화 ({'모의투자' if self.is_virtual else '실투자'})")

    def _get_access_token(self) -> str:
        """액세스 토큰 발급/갱신"""
        now = datetime.now()

        if (self._access_token and self._token_expires_at and
                now < self._token_expires_at - timedelta(minutes=5)):
            return self._access_token

        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

        resp = self.session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires_at = now + timedelta(seconds=expires_in)

        logger.info("KIS 액세스 토큰 발급 완료")
        return self._access_token

    def _get_headers(self, tr_id: str, hash_body: dict = None) -> dict:
        """API 요청 헤더 생성"""
        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

        if hash_body:
            hash_key = self._get_hash_key(hash_body)
            if hash_key:
                headers["hashkey"] = hash_key

        return headers

    def _get_hash_key(self, body: dict) -> Optional[str]:
        """해시키 발급"""
        try:
            url = f"{self.base_url}/uapi/hashkey"
            resp = self.session.post(url, json=body, timeout=5)
            resp.raise_for_status()
            return resp.json().get("HASH")
        except Exception as e:
            logger.warning(f"해시키 발급 실패: {e}")
            return None

    def _request(self, method: str, endpoint: str, tr_id: str,
                 params: dict = None, body: dict = None, retries: int = 3) -> dict:
        """API 요청"""
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers(tr_id, body)

        for attempt in range(retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=headers, params=params, timeout=10)
                else:
                    resp = self.session.post(url, headers=headers, json=body, timeout=10)

                resp.raise_for_status()
                data = resp.json()

                rt_cd = data.get("rt_cd", "")
                if rt_cd != "0":
                    msg = data.get("msg1", "Unknown error")
                    logger.error(f"KIS API 오류 ({tr_id}): {rt_cd} - {msg}")
                    if attempt < retries - 1:
                        time.sleep(1)
                        continue
                    raise RuntimeError(f"KIS API 오류: {msg}")

                return data

            except requests.exceptions.RequestException as e:
                logger.error(f"KIS 요청 실패 (시도 {attempt+1}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)

        raise RuntimeError(f"KIS API 최종 실패: {endpoint}")

    # ─── 잔고 조회 ────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """해외주식 잔고 조회"""
        tr_id = "VTTS3012R" if self.is_virtual else "TTTS3012R"
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-balance",
                             tr_id, params=params)
        return data

    def get_account_balance_usd(self) -> float:
        """USD 잔고 조회"""
        data = self.get_balance()
        output2 = data.get("output2", [{}])
        if output2:
            return float(output2[0].get("frcr_dncl_amt_2", 0))
        return 0.0

    def get_positions(self) -> list[dict]:
        """보유 종목 조회"""
        data = self.get_balance()
        output1 = data.get("output1", [])
        positions = []
        for item in output1:
            qty = int(item.get("ovrs_cblc_qty", 0))
            if qty > 0:
                positions.append({
                    "ticker": item.get("ovrs_pdno", ""),
                    "qty": qty,
                    "avg_price": float(item.get("pchs_avg_pric", 0)),
                    "current_price": float(item.get("now_pric2", 0)),
                    "pnl": float(item.get("evlu_pfls_amt", 0)),
                    "pnl_pct": float(item.get("evlu_pfls_rt", 0)),
                })
        return positions

    # ─── 현재가 조회 ────────────────────────────────────────────────

    def get_current_price(self, ticker: str, exchange: str = "NASD") -> dict:
        """해외주식 현재가 조회"""
        tr_id = "HHDFS00000300"
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
        }
        data = self._request("GET", "/uapi/overseas-price/v1/quotations/price",
                             tr_id, params=params)
        output = data.get("output", {})
        return {
            "ticker": ticker,
            "price": float(output.get("last", 0)),
            "open": float(output.get("open", 0)),
            "high": float(output.get("high", 0)),
            "low": float(output.get("low", 0)),
            "volume": int(output.get("tvol", 0)),
            "change_pct": float(output.get("rate", 0)),
        }

    # ─── 주문 ────────────────────────────────────────────────

    def buy_market(self, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        """시장가 매수"""
        tr_id = "VTTT1002U" if self.is_virtual else "TTTT1002U"
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": exchange,
            "PDNO": ticker,
            "ORD_DVSN": "00",    # 지정가
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",  # 시장가
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info(f"매수 주문: {ticker} {qty}주 시장가 ({'모의' if self.is_virtual else '실제'})")
        data = self._request("POST", "/uapi/overseas-stock/v1/trading/order",
                             tr_id, body=body)
        order_no = data.get("output", {}).get("ODNO", "")
        logger.info(f"매수 주문 완료: {ticker} {qty}주, 주문번호: {order_no}")
        return {"order_no": order_no, "ticker": ticker, "qty": qty, "type": "BUY"}

    def sell_market(self, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        """시장가 매도"""
        tr_id = "VTTT1001U" if self.is_virtual else "TTTT1001U"
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": exchange,
            "PDNO": ticker,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info(f"매도 주문: {ticker} {qty}주 시장가 ({'모의' if self.is_virtual else '실제'})")
        data = self._request("POST", "/uapi/overseas-stock/v1/trading/order",
                             tr_id, body=body)
        order_no = data.get("output", {}).get("ODNO", "")
        logger.info(f"매도 주문 완료: {ticker} {qty}주, 주문번호: {order_no}")
        return {"order_no": order_no, "ticker": ticker, "qty": qty, "type": "SELL"}

    def buy_limit(self, ticker: str, qty: int, price: float, exchange: str = "NASD") -> dict:
        """지정가 매수"""
        tr_id = "VTTT1002U" if self.is_virtual else "TTTT1002U"
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": exchange,
            "PDNO": ticker,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.4f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        logger.info(f"지정가 매수: {ticker} {qty}주 @ ${price:.4f}")
        data = self._request("POST", "/uapi/overseas-stock/v1/trading/order",
                             tr_id, body=body)
        order_no = data.get("output", {}).get("ODNO", "")
        return {"order_no": order_no, "ticker": ticker, "qty": qty, "price": price, "type": "BUY_LIMIT"}

    def cancel_order(self, order_no: str, ticker: str, qty: int, exchange: str = "NASD") -> dict:
        """주문 취소"""
        tr_id = "VTTT1004U" if self.is_virtual else "TTTT1004U"
        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "OVRS_EXCG_CD": exchange,
            "PDNO": ticker,
            "ORGN_ODNO": order_no,
            "ORD_SVR_DVSN_CD": "0",
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "ORD_DVSN": "00",
        }
        data = self._request("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl",
                             tr_id, body=body)
        return data

    def get_order_status(self, order_no: str) -> dict:
        """주문 상태 조회"""
        tr_id = "VTTS3035R" if self.is_virtual else "TTTS3035R"
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "ODNO": order_no,
            "OVRS_EXCG_CD": "NASD",
        }
        data = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl",
                             tr_id, params=params)
        return data
