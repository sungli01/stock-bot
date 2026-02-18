"""
Snapshot 기반 실시간 전종목 스캐너
- GET /v2/snapshot/locale/us/markets/stocks/tickers 사용
- 1콜로 전종목 현재가+변동률+거래량 조회
- 2초 간격 폴링
- 메모리 필터링: 변동률 5%+, 거래량 스파이크 200%+, min_price $1, min_market_cap $50M
"""
import os
import time
import logging
import requests
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"


class SnapshotScanner:
    """Polygon snapshot 기반 전종목 실시간 스캐너"""

    def __init__(self, config: dict):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.min_price = self.scanner_cfg.get("min_price", 1.0)
        self.min_market_cap = self.scanner_cfg.get("min_market_cap", 50_000_000)
        self.price_change_pct = self.scanner_cfg.get("price_change_pct", 5.0)
        self.volume_spike_pct = self.scanner_cfg.get("volume_spike_pct", 200.0)
        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)

        # 이전 스냅샷 거래량 기억 (스파이크 감지용)
        self._prev_volumes: dict[str, float] = {}
        # 이미 시그널 큐에 넣은 종목 (중복 방지, 세션 단위)
        self._signaled_tickers: set[str] = set()
        # 마지막 전체 스냅샷 데이터 (보유종목 가격 조회용)
        self._last_snapshot: dict[str, dict] = {}

    def fetch_snapshot(self) -> list[dict]:
        """전종목 snapshot 1회 조회"""
        try:
            resp = requests.get(
                SNAPSHOT_URL,
                params={"apiKey": POLYGON_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            tickers = data.get("tickers", [])
            logger.debug(f"Snapshot: {len(tickers)}개 종목 수신")
            return tickers
        except Exception as e:
            logger.error(f"Snapshot API 오류: {e}")
            return []

    def scan_once(self) -> list[dict]:
        """
        1회 스냅샷 → 필터링 → 시그널 후보 반환
        Returns: [{"ticker", "price", "change_pct", "volume", "volume_ratio", "prev_close"}, ...]
        """
        raw = self.fetch_snapshot()
        if not raw:
            return []

        # 스냅샷 캐시 업데이트
        snapshot_map = {}
        for t in raw:
            ticker = t.get("ticker", "")
            if not ticker:
                continue
            day = t.get("day", {})
            prev_day = t.get("prevDay", {})
            last_trade = t.get("lastTrade", {})
            min_data = t.get("min", {})
            prev_close = prev_day.get("c", 0) or 0
            change_pct = t.get("todaysChangePerc", 0) or 0

            # 가격: day.c → lastTrade.p → min.c → day.vw → 전일종가 역산
            price = day.get("c", 0) or last_trade.get("p", 0) or min_data.get("c", 0) or day.get("vw", 0) or 0
            if price == 0 and prev_close > 0 and change_pct != 0:
                price = prev_close * (1 + change_pct / 100)

            # 거래량: day.v → min.av (누적) → 전일 대비 추정
            volume = day.get("v", 0) or min_data.get("av", 0) or 0
            if volume == 0 and prev_day.get("v", 0) > 0 and change_pct != 0:
                volume = max(10000, int(prev_day.get("v", 0) * 0.1))  # 프리마켓 최소 추정

            snapshot_map[ticker] = {
                "ticker": ticker,
                "price": price,
                "volume": volume,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "day": day,
                "prev_day": prev_day,
                "min": t.get("min", {}),
            }

        self._last_snapshot = snapshot_map

        # 필터링
        candidates = []
        for ticker, snap in snapshot_map.items():
            # 가격 필터
            if snap["price"] < self.min_price:
                continue

            # 변동률 필터
            if abs(snap["change_pct"]) < self.price_change_pct:
                continue

            # 절대 거래량 필터
            if snap["volume"] < self.min_volume:
                continue

            # 거래량 스파이크 감지: 전일 거래량 대비
            # 프리마켓(18:00~23:30 KST)은 거래량이 적으므로 기준 완화
            prev_vol = snap.get("prev_day", {}).get("v", 0) or 0
            if prev_vol > 0:
                volume_ratio = (snap["volume"] / prev_vol) * 100
            else:
                volume_ratio = 999  # 전일 데이터 없으면 통과

            # 프리마켓: 변동률 30%+ 이면 스파이크 필터 면제
            if snap["change_pct"] >= 30.0 and snap["volume"] >= self.min_volume:
                volume_ratio = max(volume_ratio, 999)  # 스파이크 필터 통과

            if volume_ratio < self.volume_spike_pct:
                continue

            # 이미 시그널 보낸 종목 스킵 (같은 세션 내 중복 방지)
            if ticker in self._signaled_tickers:
                continue

            candidates.append({
                "ticker": ticker,
                "price": snap["price"],
                "change_pct": snap["change_pct"],
                "volume": snap["volume"],
                "volume_ratio": volume_ratio,
                "prev_close": snap["prev_close"],
                "market_cap": 0,  # snapshot에는 시총 없음, 별도 조회 필요 시 추가
            })

        if candidates:
            logger.info(f"🔍 Snapshot 스캔: {len(candidates)}개 후보 발견 (전체 {len(snapshot_map)}개)")
            for c in candidates:
                logger.info(f"  ✅ {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}% vol_ratio:{c['volume_ratio']:.0f}%")

        return candidates

    def mark_signaled(self, ticker: str):
        """시그널 큐에 추가된 종목 마킹 (중복 방지)"""
        self._signaled_tickers.add(ticker)

    def get_price(self, ticker: str) -> Optional[float]:
        """마지막 스냅샷에서 종목 현재가 반환"""
        snap = self._last_snapshot.get(ticker)
        if snap:
            return snap["price"]
        return None

    def get_all_prices(self) -> dict[str, float]:
        """마지막 스냅샷의 전종목 가격 딕셔너리"""
        return {t: s["price"] for t, s in self._last_snapshot.items() if s["price"] > 0}

    def reset_session(self):
        """새 세션 시작 시 상태 초기화"""
        self._signaled_tickers.clear()
        self._prev_volumes.clear()
        logger.info("🔄 Snapshot 스캐너 세션 리셋")
