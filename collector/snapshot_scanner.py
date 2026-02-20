"""
Snapshot 기반 실시간 전종목 스캐너 (v8.3)
역할 분리:
  - 후보 추출: 스냅샷에서 5%+ 급등 종목 → BarScanner에 전달
  - 매수 트리거: 모니터링 큐 종목이 20%+ 가격 → 즉시 후보 반환
  거래량 감지는 BarScanner(3분봉 완성봉 비교)가 전담
"""
import os
import time
import logging
import requests
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"


class SnapshotScanner:
    """Polygon snapshot 기반 전종목 실시간 스캐너 (v8.3)"""

    def __init__(self, config: dict, monitoring_queue: dict, queue_lock):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.min_price = self.scanner_cfg.get("min_price", 0.7)
        self.max_price = self.scanner_cfg.get("max_price", 30.0)
        self.price_change_pct = self.scanner_cfg.get("price_change_pct", 20.0)
        self.candidate_change_pct = self.scanner_cfg.get("candidate_change_pct", 5.0)  # BarScanner 후보 기준
        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)
        self.min_daily_volume = self.scanner_cfg.get("min_daily_volume", 500_000)

        # 공유 모니터링 큐 (BarScanner가 등록, SnapshotScanner가 조회)
        self.monitoring_queue = monitoring_queue
        self.queue_lock = queue_lock

        # 이미 시그널 보낸 종목 (세션 단위 중복 방지)
        self._signaled_tickers: set[str] = set()
        # 가격 추적 (price_velocity용)
        self._prev_prices: dict[str, float] = {}
        self._prev_scan_time: float = 0.0
        # 마지막 스냅샷 캐시 (가격 조회용)
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
            return resp.json().get("tickers", [])
        except Exception as e:
            logger.error(f"Snapshot API 오류: {e}")
            return []

    def scan_once(self) -> tuple[list[dict], dict[str, float]]:
        """
        1회 스냅샷 스캔
        Returns:
          candidates: 매수 후보 목록 (큐 등록 + 20%+ 확인)
          bar_candidates: BarScanner 후보 {ticker: price} (5%+ 급등)
        """
        scan_time = time.time()
        raw = self.fetch_snapshot()
        if not raw:
            return [], {}

        elapsed = scan_time - self._prev_scan_time if self._prev_scan_time > 0 else 0

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

            price = day.get("c", 0) or last_trade.get("p", 0) or min_data.get("c", 0) or day.get("vw", 0) or 0
            if price == 0 and prev_close > 0 and change_pct != 0:
                price = prev_close * (1 + change_pct / 100)

            volume = day.get("v", 0) or min_data.get("av", 0) or 0
            if volume == 0 and prev_day.get("v", 0) > 0:
                volume = max(10000, int(prev_day.get("v", 0) * 0.1))

            price_velocity = 0.0
            scan_delta_pct = 0.0
            if elapsed > 0 and ticker in self._prev_prices and self._prev_prices[ticker] > 0:
                prev_price = self._prev_prices[ticker]
                scan_delta_pct = ((price - prev_price) / prev_price) * 100
                price_velocity = scan_delta_pct / elapsed

            snapshot_map[ticker] = {
                "ticker": ticker,
                "price": price,
                "volume": volume,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "price_velocity": price_velocity,
                "scan_delta_pct": scan_delta_pct,
                "prev_day": prev_day,
                "min": min_data,
            }

        self._last_snapshot = snapshot_map

        # ── STEP 1: BarScanner 후보 추출 (5%+ 급등, 가격 범위 내) ──
        # 거래량 체크는 BarScanner가 담당 — 여기서는 가격/변동률만
        bar_candidates = {}
        for ticker, snap in snapshot_map.items():
            if ticker in self._signaled_tickers:
                continue
            if snap["price"] < self.min_price or snap["price"] > self.max_price:
                continue
            if snap["change_pct"] < self.candidate_change_pct:
                continue
            bar_candidates[ticker] = snap["price"]

        # ── STEP 2: 모니터링 큐 종목 중 20%+ → 즉시 매수 후보 ──
        # ★ API 호출 없음, 메모리 조회만 (~0ms)
        candidates = []

        with self.queue_lock:
            queued = dict(self.monitoring_queue)

        for ticker, queue_info in queued.items():
            if ticker in self._signaled_tickers:
                continue
            snap = snapshot_map.get(ticker)
            if not snap:
                continue

            # ★ 기준 가격 = 거래량 폭증 시점 가격 (전일종가 기준 아님)
            queue_price = queue_info.get("price", 0)
            if queue_price <= 0:
                continue

            pct_from_queue = (snap["price"] - queue_price) / queue_price * 100

            # ★ 방향성: 큐 등록 가격 대비 -3% 이상 꺾이면 보류 (일시적 눌림 허용)
            if snap["price"] < queue_price * 0.97:
                logger.debug(
                    f"⬇️ {ticker} 꺾임 보류 "
                    f"(기준${queue_price:.2f} → 현재${snap['price']:.2f} {pct_from_queue:+.1f}%)"
                )
                continue

            # ★ 케이스 A/B 통합: 큐 등록 시점 기준 +20%+
            # - 케이스 A: 같은 봉 내 즉시 +20% (빠른 급등)
            # - 케이스 B: 이후 3분봉 10개(30분) 이내 우상향으로 +20%
            if pct_from_queue < self.price_change_pct:
                logger.debug(
                    f"📊 {ticker} 모니터링 중: 기준${queue_price:.2f} → "
                    f"현재${snap['price']:.2f} ({pct_from_queue:+.1f}% / 목표 +{self.price_change_pct:.0f}%)"
                )
                continue

            vol_ratio = queue_info.get("vol_ratio", 999.0)

            logger.info(
                f"🎯 매수 후보: {ticker} ${snap['price']:.2f} "
                f"기준대비 {pct_from_queue:+.1f}% (기준${queue_price:.2f}) "
                f"3분봉:{vol_ratio:.0f}%"
            )

            candidates.append({
                "ticker": ticker,
                "price": snap["price"],
                "change_pct": snap["change_pct"],
                "pct_from_queue": round(pct_from_queue, 2),  # 큐 기준 상승률
                "queue_price": queue_price,
                "volume": snap["volume"],
                "volume_ratio": vol_ratio,
                "vol_3min_ratio": vol_ratio,
                "prev_close": snap["prev_close"],
                "price_velocity": snap["price_velocity"],
                "market_cap": 0,
            })

        candidates.sort(key=lambda c: -c["change_pct"])

        if candidates:
            logger.info(f"🔥 최종 후보 {len(candidates)}개 — 즉시 매수")
            for c in candidates:
                logger.info(f"  ✅ {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}%")

        # 다음 스캔 비교용 저장
        self._prev_prices = {t: s["price"] for t, s in snapshot_map.items() if s["price"] > 0}
        self._prev_scan_time = scan_time

        return candidates, bar_candidates

    def mark_signaled(self, ticker: str):
        self._signaled_tickers.add(ticker)

    def get_price(self, ticker: str) -> Optional[float]:
        snap = self._last_snapshot.get(ticker)
        return snap["price"] if snap else None

    def get_all_prices(self) -> dict[str, float]:
        return {t: s["price"] for t, s in self._last_snapshot.items() if s["price"] > 0}

    def reset_session(self):
        self._signaled_tickers.clear()
        self._prev_prices.clear()
        self._prev_scan_time = 0.0
        logger.info("🔄 Snapshot 스캐너 세션 리셋")
