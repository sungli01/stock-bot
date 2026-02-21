"""
Snapshot 기반 실시간 전종목 스캐너 (v9)
역할 분리:
  - 후보 추출: 스냅샷에서 5%~20% 급등 종목 → BarScanner에 전달 (Bug#6: 범위 제한)
  - 매수 트리거: 모니터링 큐 종목
      · 1차: 큐 대비 +20% → 즉시 매수 후보
      · 2차: 큐 대비 +15% → 즉시 매수 후보 (is_second=True)
  - Bug #3 수정: max_pct_from_queue 40% 초과 시 진입 차단
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
    """Polygon snapshot 기반 전종목 실시간 스캐너 (v9)"""

    def __init__(self, config: dict, monitoring_queue: dict, queue_lock):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.min_price = self.scanner_cfg.get("min_price", 0.7)
        self.max_price = self.scanner_cfg.get("max_price", 30.0)

        # 1차 트리거: 큐 대비 +20%
        self.price_change_pct = self.scanner_cfg.get("price_change_pct", 20.0)
        # [v9] 2차 트리거: 큐 대비 +15%
        self.trigger_pct_2nd = self.scanner_cfg.get("trigger_pct_2nd", 15.0)
        # [v9/Bug#3] 상단 진입 제한: 큐 대비 최대 40%
        self.max_pct_from_queue = self.scanner_cfg.get("max_pct_from_queue", 40.0)

        # BarScanner 후보 기준 (5%~20% 범위)
        self.candidate_change_pct = self.scanner_cfg.get("candidate_change_pct", 5.0)
        self.candidate_max_change_pct = self.scanner_cfg.get("candidate_max_change_pct", 20.0)

        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)
        self.min_daily_volume = self.scanner_cfg.get("min_daily_volume", 300_000)
        self.min_daily_volume_highprice = self.scanner_cfg.get("min_daily_volume_highprice", 50_000)
        self.highprice_threshold = self.scanner_cfg.get("highprice_threshold", 10.0)

        # 공유 모니터링 큐
        self.monitoring_queue = monitoring_queue
        self.queue_lock = queue_lock

        # [v9] 1차 완료 종목 (2차 신호 허용), 2차 완료 종목 (완전 차단)
        self._signaled_once: set[str] = set()    # 1차 완료
        self._signaled_twice: set[str] = set()   # 2차 완료 (완전 차단)

        # 가격 추적
        self._prev_prices: dict[str, float] = {}
        self._prev_scan_time: float = 0.0
        self._last_snapshot: dict[str, dict] = {}

    # ── 하위 호환: mark_signaled ───────────────────────────
    def mark_signaled(self, ticker: str, is_second: bool = False):
        """
        매수 완료 마킹
        - is_second=False (1차): _signaled_once에 추가 → 2차 진입은 허용
        - is_second=True  (2차): _signaled_twice에 추가 → 완전 차단
        """
        if is_second:
            self._signaled_twice.add(ticker)
            self._signaled_once.add(ticker)
            logger.info(f"🔒 {ticker} 2차 완료 → 당일 완전 차단")
        else:
            self._signaled_once.add(ticker)
            logger.info(f"1️⃣ {ticker} 1차 완료 → 2차 진입 대기")

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
          candidates: 매수 후보 목록 (1차/2차 구분 포함)
          bar_candidates: BarScanner 후보 {ticker: price} (5%~20% 급등)
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

        # ── STEP 1: BarScanner 후보 추출 ──
        bar_candidates = {}
        for ticker, snap in snapshot_map.items():
            # 2차 완료 → 완전 차단
            if ticker in self._signaled_twice:
                continue

            is_already_once = ticker in self._signaled_once  # 1차 완료 종목

            if is_already_once:
                # [v9 #2 수정] 2차 대기 종목: 변동률/가격 범위 제한 없이 포함
                # (1차 완료 후 급등 중이어도 2차 vol spike 감지해야 함)
                if snap["price"] > 0:
                    bar_candidates[ticker] = snap["price"]
            else:
                # 1차 후보: $0.70~$30, 전일비 5%~20% 범위
                if snap["price"] < self.min_price or snap["price"] > self.max_price:
                    continue
                if snap["change_pct"] < self.candidate_change_pct:
                    continue
                if snap["change_pct"] >= self.candidate_max_change_pct:
                    continue
                bar_candidates[ticker] = snap["price"]

        # ── STEP 2: 모니터링 큐 종목 중 트리거 도달 → 매수 후보 ──
        candidates = []

        with self.queue_lock:
            queued = dict(self.monitoring_queue)

        for ticker, queue_info in queued.items():
            # 2차 완료 → 완전 차단
            if ticker in self._signaled_twice:
                continue

            is_second = queue_info.get("is_second", False)

            # [v9] 큐 등록 시점 일거래량 기록 (첫 스캔 시 한 번만)
            snap_for_vol = snapshot_map.get(ticker)
            if snap_for_vol and "vol_at_queue" not in queue_info:
                with self.queue_lock:
                    if ticker in self.monitoring_queue:
                        self.monitoring_queue[ticker]["vol_at_queue"] = snap_for_vol.get("volume", 0)

            # 1차 완료 후 2차: _signaled_once에 있어야 함 (1차 완료된 종목만)
            if is_second and ticker not in self._signaled_once:
                logger.debug(f"⚠️ {ticker} is_second=True지만 1차 미완료 — 2차 스킵")
                continue

            # 1차 진입: _signaled_once에 이미 있으면 스킵 (단, 2차 큐면 허용)
            if not is_second and ticker in self._signaled_once:
                continue

            snap = snapshot_map.get(ticker)
            if not snap:
                continue

            day_volume = snap.get("volume", 0)
            cur_price = snap.get("price", 0)

            # [v9 #1 수정] 일 거래량 체크 제거 — 1차/2차 모두 무제한
            # (거래량 30% 캡으로 매수량 자체를 제한하므로 최소 거래량 불필요)

            queue_price = queue_info.get("price", 0)
            if queue_price <= 0:
                continue

            pct_from_queue = (snap["price"] - queue_price) / queue_price * 100

            # 방향성: -3% 이상 꺾이면 보류
            if snap["price"] < queue_price * 0.97:
                logger.debug(
                    f"⬇️ {ticker} 꺾임 보류 "
                    f"(기준${queue_price:.2f} → 현재${snap['price']:.2f} {pct_from_queue:+.1f}%)"
                )
                continue

            # [Bug #3] 상단 진입 제한: 큐 대비 max_pct_from_queue 초과 시 차단
            if pct_from_queue > self.max_pct_from_queue:
                logger.info(
                    f"⛔ {ticker} 상단 진입 제한: +{pct_from_queue:.1f}% > "
                    f"+{self.max_pct_from_queue:.0f}% — 과도 오버슈팅 차단"
                )
                continue

            # 트리거 체크 (1차: +20%, 2차: +15%)
            trigger_pct = self.trigger_pct_2nd if is_second else self.price_change_pct

            if pct_from_queue < trigger_pct:
                logger.debug(
                    f"📊 {'2차' if is_second else '1차'} {ticker} 모니터링 중: "
                    f"기준${queue_price:.2f} → 현재${snap['price']:.2f} "
                    f"({pct_from_queue:+.1f}% / 목표 +{trigger_pct:.0f}%)"
                )
                continue

            vol_ratio = queue_info.get("vol_ratio", 999.0)
            entry_type = "2차" if is_second else "1차"

            # [v9] 1차 매수량: 큐 등록 ~ 매수 시점 구간 거래량의 30% 이내
            USD_KRW = float(os.getenv("USD_KRW_RATE", "1450.0"))
            max_buy_krw_by_vol = None
            if not is_second:
                vol_at_queue = queue_info.get("vol_at_queue", 0)
                vol_since_queue = max(day_volume - vol_at_queue, 1)
                max_shares_30pct = vol_since_queue * 0.30
                max_buy_krw_by_vol = max_shares_30pct * cur_price * USD_KRW

            logger.info(
                f"🎯 {entry_type} 매수 후보: {ticker} ${snap['price']:.2f} "
                f"기준대비 {pct_from_queue:+.1f}% (기준${queue_price:.2f}) "
                f"3분봉:{vol_ratio:.0f}%"
                + (f" | 거래량캡 ₩{max_buy_krw_by_vol:,.0f}" if max_buy_krw_by_vol else "")
            )

            candidates.append({
                "ticker": ticker,
                "price": snap["price"],
                "change_pct": snap["change_pct"],
                "pct_from_queue": round(pct_from_queue, 2),
                "queue_price": queue_price,
                "volume": snap["volume"],
                "volume_ratio": vol_ratio,
                "vol_3min_ratio": vol_ratio,
                "prev_close": snap["prev_close"],
                "price_velocity": snap["price_velocity"],
                "market_cap": 0,
                "is_second": is_second,
                "max_buy_krw_by_vol": round(max_buy_krw_by_vol) if max_buy_krw_by_vol else None,  # [v9] 거래량 30% 캡
            })

        candidates.sort(key=lambda c: -c["change_pct"])

        if candidates:
            for c in candidates:
                entry_type = "2차" if c.get("is_second") else "1차"
                logger.info(f"  ✅ [{entry_type}] {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}%")

        self._prev_prices = {t: s["price"] for t, s in snapshot_map.items() if s["price"] > 0}
        self._prev_scan_time = scan_time

        return candidates, bar_candidates

    def get_price(self, ticker: str) -> Optional[float]:
        snap = self._last_snapshot.get(ticker)
        return snap["price"] if snap else None

    def get_all_prices(self) -> dict[str, float]:
        return {t: s["price"] for t, s in self._last_snapshot.items() if s["price"] > 0}

    def reset_session(self):
        self._signaled_once.clear()
        self._signaled_twice.clear()
        self._prev_prices.clear()
        self._prev_scan_time = 0.0
        logger.info("🔄 Snapshot 스캐너 세션 리셋 (v9)")
