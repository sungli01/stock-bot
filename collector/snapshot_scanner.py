"""
Snapshot 기반 실시간 전종목 스캐너 (v8 — 3분봉 모멘텀 엔진)
- GET /v2/snapshot/locale/us/markets/stocks/tickers 사용
- 1콜로 전종목 현재가+변동률+거래량 조회
- 1차 필터: 20%+ 급등
- 2차 필터: 3분봉 직전 대비 현재 거래량 1000%+ (진짜 모멘텀만)
"""
import os
import time
import math
import logging
import requests
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
SNAPSHOT_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"


class SnapshotScanner:
    """Polygon snapshot 기반 전종목 실시간 스캐너"""

    def __init__(self, config: dict):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.min_price = self.scanner_cfg.get("min_price", 0.7)
        self.max_price = self.scanner_cfg.get("max_price", 10.0)
        self.min_market_cap = self.scanner_cfg.get("min_market_cap", 50_000_000)
        self.price_change_pct = self.scanner_cfg.get("price_change_pct", 20.0)  # v8: 20%
        self.volume_spike_pct = self.scanner_cfg.get("volume_spike_pct", 200.0)
        self.vol_3min_ratio_pct = self.scanner_cfg.get("vol_3min_ratio_pct", 1000.0)  # v8: 1000%
        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)

        # 이전 스냅샷 거래량 기억 (스파이크 감지용)
        self._prev_volumes: dict[str, float] = {}
        # 이전 스냅샷 가격 기억 (가격 속도 추적용)
        self._prev_prices: dict[str, float] = {}
        self._prev_scan_time: float = 0.0
        # 이미 시그널 큐에 넣은 종목 (중복 방지, 세션 단위)
        self._signaled_tickers: set[str] = set()
        # 급등 최초 감지 시점 {ticker: timestamp} — 5분 경과 시 매수 제외
        self._surge_first_seen: dict[str, float] = {}
        # 급등 만료 로그 1회만 출력 (로그 과다 방지)
        self._surge_logged_expire: set[str] = set()
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
        Returns: [{"ticker", "price", "change_pct", "volume", "volume_ratio", "prev_close", "price_velocity"}, ...]
        """
        scan_time = time.time()
        raw = self.fetch_snapshot()
        if not raw:
            return []

        # 시간 간격 계산 (초)
        elapsed = scan_time - self._prev_scan_time if self._prev_scan_time > 0 else 0

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

            # 직전 스캔 대비 가격 변화율 (price_velocity: %/초)
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
                "day": day,
                "prev_day": prev_day,
                "min": t.get("min", {}),
            }

        self._last_snapshot = snapshot_map

        # ── v8: 1차 필터 (스냅샷) ─────────────────────────────────
        pre_candidates = []
        min_daily_volume = self.scanner_cfg.get("min_daily_volume", 500_000)

        for ticker, snap in snapshot_map.items():
            # 가격 필터 ($0.70 ~ $10.00 페니스탁만)
            if snap["price"] < self.min_price or snap["price"] > self.max_price:
                continue

            # 이미 시그널 보낸 종목 스킵
            if ticker in self._signaled_tickers:
                continue

            # ★ 핵심 1차 조건: 20%+ 급등
            if snap["change_pct"] < self.price_change_pct:
                continue

            # 절대 거래량 필터 (유동성)
            if snap["volume"] < min_daily_volume:
                continue

            pre_candidates.append(snap)

        # 1차 통과 종목 로그
        if pre_candidates:
            logger.info(f"🔍 1차 통과 ({len(pre_candidates)}개): " +
                        ", ".join(f"{s['ticker']} {s['change_pct']:+.1f}%" for s in pre_candidates[:5]))

        # ── v8: 2차 필터 (3분봉 거래량 1000%+) ─────────────────
        candidates = []
        MAX_3MIN_CHECK = 10  # API 레이트 리밋 대응: 최대 10개만 체크
        checked = 0

        for snap in pre_candidates[:MAX_3MIN_CHECK]:
            ticker = snap["ticker"]

            # 급등 최초 감지 시점 추적 & 15분 경과 시 제외 (v8: 더 넉넉)
            if ticker not in self._surge_first_seen:
                self._surge_first_seen[ticker] = scan_time
                logger.info(f"🚀 {ticker} 급등 최초 감지 ({snap['change_pct']:+.1f}%)")
            surge_elapsed = scan_time - self._surge_first_seen[ticker]
            if surge_elapsed > 900:  # 15분
                if ticker not in self._surge_logged_expire:
                    logger.info(f"⏰ {ticker} 급등 후 {surge_elapsed:.0f}초 경과 — 제외")
                    self._surge_logged_expire.add(ticker)
                continue

            # ★ 핵심 2차 조건: 3분봉 직전 대비 현재 거래량 1000%+
            cur_vol, prev_vol = self._fetch_3min_volume(ticker)
            checked += 1

            if prev_vol <= 0:
                # 데이터 없으면 스냅샷 거래량으로 대체 판단
                vol_ratio_3min = 999.0  # 통과 (데이터 없으면 검증 불가)
                logger.info(f"  ⚠️ {ticker} 3분봉 데이터 없음 — 스냅샷 기준 통과")
            else:
                vol_ratio_3min = (cur_vol / prev_vol) * 100
                if vol_ratio_3min < self.vol_3min_ratio_pct:
                    logger.info(f"  ❌ {ticker} 3분봉 거래량 미달: {vol_ratio_3min:.0f}% (기준 {self.vol_3min_ratio_pct:.0f}%)")
                    continue
                logger.info(f"  ✅ {ticker} 3분봉 거래량 폭발: {vol_ratio_3min:.0f}% (cur:{cur_vol:.0f} prev:{prev_vol:.0f})")

            # 전일 거래량 대비 스냅샷 스파이크 비율
            prev_day_vol = snap.get("prev_day", {}).get("v", 0) or 0
            volume_ratio = (snap["volume"] / prev_day_vol * 100) if prev_day_vol > 0 else 999.0

            candidates.append({
                "ticker": ticker,
                "price": snap["price"],
                "change_pct": snap["change_pct"],
                "volume": snap["volume"],
                "volume_ratio": volume_ratio,
                "vol_3min_ratio": vol_ratio_3min,
                "prev_close": snap["prev_close"],
                "price_velocity": snap["price_velocity"],
                "market_cap": 0,
            })

        # 정렬: 변동률 높은 순
        candidates.sort(key=lambda c: -c["change_pct"])

        if candidates:
            logger.info(f"🎯 최종 통과 {len(candidates)}개 (3분봉 1000%+ 검증 완료)")
            for c in candidates:
                logger.info(f"  🔥 {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}% 3min:{c['vol_3min_ratio']:.0f}%")

        # 현재 가격을 다음 스캔 비교용으로 저장
        self._prev_prices = {t: s["price"] for t, s in snapshot_map.items() if s["price"] > 0}
        self._prev_scan_time = scan_time

        return candidates

    def _fetch_3min_volume(self, ticker: str) -> tuple[float, float]:
        """
        Polygon aggs API로 3분봉 최근 2개 조회
        Returns: (current_bar_volume, prev_bar_volume)
        current가 0이면 데이터 없음으로 처리
        """
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/3/minute/{today}/{today}"
            resp = requests.get(url, params={
                "adjusted": "true",
                "sort": "desc",
                "limit": 3,
                "apiKey": POLYGON_API_KEY,
            }, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            bars = data.get("results", [])
            if len(bars) >= 2:
                # desc 정렬: bars[0]=최신(현재 진행중 or 직전), bars[1]=그 이전
                # 현재 진행 중인 봉은 미완성이므로 bars[1] vs bars[2] 비교가 더 안정적
                # 단, 3개 있으면 완성된 2개(bars[1], bars[2]) 비교
                if len(bars) >= 3:
                    return bars[1]["v"], bars[2]["v"]  # 완성된 최신 봉 vs 그 직전
                return bars[0]["v"], bars[1]["v"]
            elif len(bars) == 1:
                return bars[0]["v"], 0
            return 0, 0
        except Exception as e:
            logger.debug(f"{ticker} 3분봉 조회 오류: {e}")
            return 0, 0

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
        self._prev_prices.clear()
        self._prev_scan_time = 0.0
        self._surge_first_seen.clear()
        self._surge_logged_expire.clear()
        logger.info("🔄 Snapshot 스캐너 세션 리셋")
