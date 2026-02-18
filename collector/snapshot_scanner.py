"""
Snapshot 기반 실시간 전종목 스캐너
- GET /v2/snapshot/locale/us/markets/stocks/tickers 사용
- 1콜로 전종목 현재가+변동률+거래량 조회
- 2초 간격 폴링
- 메모리 필터링: 변동률 5%+, 거래량 스파이크 200%+, min_price $1, min_market_cap $50M
- 급등 초기 포착: 직전 스캔 대비 가격 속도 추적, 고점 추격 방지
"""
import os
import time
import math
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
        self.min_price = self.scanner_cfg.get("min_price", 0.7)
        self.max_price = self.scanner_cfg.get("max_price", 10.0)
        self.min_market_cap = self.scanner_cfg.get("min_market_cap", 50_000_000)
        self.price_change_pct = self.scanner_cfg.get("price_change_pct", 5.0)
        self.volume_spike_pct = self.scanner_cfg.get("volume_spike_pct", 200.0)
        self.min_volume = self.scanner_cfg.get("min_volume", 10_000)

        # 이전 스냅샷 거래량 기억 (스파이크 감지용)
        self._prev_volumes: dict[str, float] = {}
        # 이전 스냅샷 가격 기억 (가격 속도 추적용)
        self._prev_prices: dict[str, float] = {}
        self._prev_scan_time: float = 0.0
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

        # 필터링
        candidates = []
        for ticker, snap in snapshot_map.items():
            # 가격 필터 ($0.70 ~ $10.00 페니스탁만)
            if snap["price"] < self.min_price or snap["price"] > self.max_price:
                continue

            # 고점 추격 방지: 전일종가 대비 100%+ 이미 오른 종목 제외
            if snap["change_pct"] >= 100.0:
                continue

            # 이미 시그널 보낸 종목 스킵 (같은 세션 내 중복 방지)
            if ticker in self._signaled_tickers:
                continue

            # 급등 초기 감지: 2초 사이 2%+ 상승 → 변동률/거래량 기준 완화
            is_early_surge = snap["scan_delta_pct"] >= 2.0 and elapsed > 0

            # 변동률 필터 (급등 초기 시그널이면 3%부터 허용)
            min_change = 3.0 if is_early_surge else self.price_change_pct
            if abs(snap["change_pct"]) < min_change:
                continue

            # 절대 거래량 필터
            if snap["volume"] < self.min_volume:
                continue

            # 거래량 스파이크 감지: 전일 거래량 대비
            prev_vol = snap.get("prev_day", {}).get("v", 0) or 0
            if prev_vol > 0:
                volume_ratio = (snap["volume"] / prev_vol) * 100
            else:
                volume_ratio = 999  # 전일 데이터 없으면 통과

            # 프리마켓: 변동률 30%+ 이면 스파이크 필터 면제
            if snap["change_pct"] >= 30.0 and snap["volume"] >= self.min_volume:
                volume_ratio = max(volume_ratio, 999)  # 스파이크 필터 통과

            # 급등 초기 시그널이면 거래량 스파이크 기준 완화
            if is_early_surge:
                volume_ratio = max(volume_ratio, 999)

            if volume_ratio < self.volume_spike_pct:
                continue

            # 초기 급등 우선순위 판단
            # 5~30% 구간 + 거래량 급증 = 높은 우선순위
            is_early_zone = 5.0 <= snap["change_pct"] <= 30.0
            vol_surging = volume_ratio >= 200.0

            if is_early_zone and vol_surging:
                priority = 0  # 최고 우선순위: 초기 급등 구간
            elif is_early_surge:
                priority = 1  # 높은 우선순위: 직전 스캔 대비 급등 중
            elif is_early_zone:
                priority = 2  # 중간: 초기 구간이지만 거래량 보통
            else:
                priority = 3  # 낮음: 이미 30%+ 상승

            candidates.append({
                "ticker": ticker,
                "price": snap["price"],
                "change_pct": snap["change_pct"],
                "volume": snap["volume"],
                "volume_ratio": volume_ratio,
                "prev_close": snap["prev_close"],
                "price_velocity": snap["price_velocity"],
                "market_cap": 0,  # snapshot에는 시총 없음, 별도 조회 필요 시 추가
                "_priority": priority,
            })

        # 정렬: 우선순위 → 같은 우선순위 내에서 change_pct * log(volume)
        candidates.sort(key=lambda c: (
            c["_priority"],
            -(c["change_pct"] * math.log(max(c["volume"], 1)))
        ))

        # _priority 필드 제거 (내부용)
        for c in candidates:
            del c["_priority"]

        if candidates:
            logger.info(f"🔍 Snapshot 스캔: {len(candidates)}개 후보 발견 (전체 {len(snapshot_map)}개)")
            for c in candidates:
                vel_str = f" vel:{c['price_velocity']:+.2f}%/s" if c['price_velocity'] != 0 else ""
                logger.info(f"  ✅ {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}% vol_ratio:{c['volume_ratio']:.0f}%{vel_str}")

        # 현재 가격을 다음 스캔 비교용으로 저장
        self._prev_prices = {t: s["price"] for t, s in snapshot_map.items() if s["price"] > 0}
        self._prev_scan_time = scan_time

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
        self._prev_prices.clear()
        self._prev_scan_time = 0.0
        logger.info("🔄 Snapshot 스캐너 세션 리셋")
