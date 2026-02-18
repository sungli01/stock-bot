"""
KIS 현재가 API 기반 소형주 실시간 스캐너
- 워치리스트(data/watchlist.json) 전체를 주기적 스캔
- 전일종가 대비 10%+ 상승 종목 감지
- API 호출 간격: 0.1초 (초당 10건, KIS 제한 고려)
"""
import os
import json
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.koreainvestment.com:9443"
PRICE_URL = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"

# 거래소 코드 매핑 (watchlist.json의 exchange → KIS EXCD)
EXCHANGE_MAP = {
    "XNAS": "NAS",  # 나스닥
    "XNYS": "NYS",  # 뉴욕
    "XASE": "AMS",  # 아멕스 (AMEX)
    "NAS": "NAS",
    "NYS": "NYS",
    "AMS": "AMS",
    "NASDAQ": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")


class KISScanner:
    """KIS 현재가 API 기반 소형주 워치리스트 스캐너"""

    def __init__(self, config: dict):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.min_change_pct = self.scanner_cfg.get("kis_min_change_pct", 10.0)
        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)

        # KIS 클라이언트에서 토큰 재사용
        from trader.kis_client import KISClient
        self.kis = KISClient()

        # 워치리스트 로드
        self.watchlist = self._load_watchlist()
        logger.info(f"📋 KIS 스캐너: 워치리스트 {len(self.watchlist)}개 종목 로드")

        # 시그널 중복 방지 (SnapshotScanner와 공유할 수 있도록 외부에서 set 전달 가능)
        self._signaled_tickers: set[str] = set()

    def _load_watchlist(self) -> list[dict]:
        """watchlist.json 로드"""
        try:
            with open(WATCHLIST_PATH, "r") as f:
                data = json.load(f)
            # [{"ticker": "AAPL", "exchange": "XNAS", ...}, ...]
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            logger.warning(f"⚠️ 워치리스트 없음: {WATCHLIST_PATH}")
            return []
        except Exception as e:
            logger.error(f"워치리스트 로드 실패: {e}")
            return []

    def _get_excd(self, item: dict) -> str:
        """종목의 KIS 거래소 코드 결정"""
        exchange = item.get("exchange", "") or item.get("primary_exchange", "")
        return EXCHANGE_MAP.get(exchange, "NAS")  # 기본값 나스닥

    def _fetch_price(self, ticker: str, excd: str) -> Optional[dict]:
        """KIS 현재가 API 1건 호출"""
        if not self.kis.connected:
            return None

        headers = self.kis._headers("HHDFS00000300")
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": ticker,
        }
        try:
            r = requests.get(PRICE_URL, headers=headers, params=params, timeout=10)
            data = r.json()
            if data.get("rt_cd") != "0":
                return None
            output = data.get("output", {})
            last = float(output.get("last", "0") or "0")
            base = float(output.get("base", "0") or "0")
            rate = float(output.get("rate", "0") or "0")
            tvol = int(float(output.get("tvol", "0") or "0"))
            sign = output.get("sign", "")

            if last <= 0:
                return None

            return {
                "ticker": ticker,
                "price": last,
                "prev_close": base,
                "change_pct": rate,
                "volume": tvol,
                "sign": sign,
            }
        except Exception as e:
            logger.debug(f"KIS 현재가 조회 실패 [{ticker}]: {e}")
            return None

    def scan_once(self) -> list[dict]:
        """
        워치리스트 전체를 KIS 현재가 API로 스캔.
        전일종가 대비 10%+ 상승 종목 리턴.
        """
        if not self.kis.connected:
            logger.warning("KIS 미연결 — 스캔 스킵")
            return []

        if not self.watchlist:
            return []

        candidates = []
        scanned = 0
        errors = 0

        for item in self.watchlist:
            ticker = item["ticker"]

            # 이미 시그널 처리된 종목 스킵
            if ticker in self._signaled_tickers:
                continue

            excd = self._get_excd(item)
            result = self._fetch_price(ticker, excd)

            if result:
                scanned += 1
                # 변동률 필터
                if result["change_pct"] >= self.min_change_pct:
                    # 거래량 필터
                    if result["volume"] >= self.min_volume:
                        candidates.append({
                            "ticker": ticker,
                            "price": result["price"],
                            "change_pct": result["change_pct"],
                            "volume": result["volume"],
                            "volume_ratio": 999,  # KIS에서는 전일 거래량 비교 불가, 통과 처리
                            "prev_close": result["prev_close"],
                            "market_cap": item.get("market_cap", 0),
                            "source": "kis",
                        })
            else:
                errors += 1

            # API 호출 간격: 0.1초 (초당 10건)
            time.sleep(0.1)

        if candidates:
            logger.info(f"🔍 KIS 스캔 완료: {len(candidates)}개 후보 (스캔 {scanned}/{len(self.watchlist)}, 에러 {errors})")
            for c in candidates:
                logger.info(f"  🔥 {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}% vol:{c['volume']:,}")
        else:
            logger.debug(f"KIS 스캔 완료: 후보 없음 (스캔 {scanned}/{len(self.watchlist)})")

        return candidates

    def get_price(self, ticker: str) -> Optional[float]:
        """개별 종목 현재가 (KIS API)"""
        # 워치리스트에서 거래소 찾기
        excd = "NAS"
        for item in self.watchlist:
            if item["ticker"] == ticker:
                excd = self._get_excd(item)
                break

        result = self._fetch_price(ticker, excd)
        return result["price"] if result else None

    def mark_signaled(self, ticker: str):
        """시그널 처리된 종목 마킹"""
        self._signaled_tickers.add(ticker)

    def share_signaled(self, signaled_set: set):
        """SnapshotScanner와 시그널 세트 공유"""
        self._signaled_tickers = signaled_set

    def reset_session(self):
        """세션 리셋"""
        self._signaled_tickers.clear()
        logger.info("🔄 KIS 스캐너 세션 리셋")
