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

        # 이전 스냅샷 거래량 기억
        self._prev_volumes: dict[str, float] = {}
        # 이전 스냅샷 min.v (직전 1분봉 거래량) — 거래량 폭증 감지용
        self._prev_min_v: dict[str, float] = {}
        # 이전 스냅샷 가격
        self._prev_prices: dict[str, float] = {}
        self._prev_scan_time: float = 0.0
        # 이미 시그널 큐에 넣은 종목 (중복 방지, 세션 단위)
        self._signaled_tickers: set[str] = set()
        # ★ 모니터링 큐: 거래량 1000%+ 통과 종목 (20%+ 가격 대기)
        # {ticker: {"time": 등록시각, "price": 등록시점가격}}
        self._monitoring_queue: dict[str, dict] = {}
        # 급등 만료 로그 1회만 출력
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

        min_daily_volume = self.scanner_cfg.get("min_daily_volume", 500_000)
        queue_expire_sec = 900  # 모니터링 큐 유효기간 15분

        # ETF/인버스 제외 패턴 (레버리지/인버스 ETF는 거래량 패턴이 달라 노이즈)
        ETF_SUFFIXES = ("L", "S", "X")   # SOXS, SOXL, TQQQ, SQQQ 등 끝자리
        ETF_PATTERNS = ("SH", "SDS", "QID", "SPXS", "SPXU", "SQQQ", "TQQQ",
                        "SOXS", "SOXL", "UVXY", "SVXY", "VXX", "VIXY",
                        "ZSL", "AGQ", "JDST", "JNUG", "LABD", "LABU",
                        "DUST", "NUGT", "YANG", "YINN", "FAS", "FAZ",
                        "TZA", "TNA", "ERX", "ERY", "KOLD", "BOIL")

        def _is_etf(t: str) -> bool:
            if t in ETF_PATTERNS:
                return True
            # 3~4글자이고 S/L로 끝나는 레버리지 ETF 패턴
            if len(t) >= 4 and t[-1] in ("S", "L") and t[-2].isdigit():
                return True
            return False

        # ── STEP 1: 거래량 폭증 감지 → 모니터링 큐 등록 ──────────
        # min.v (직전 1분봉 거래량) 직전 스캔 대비 1000%+ 이면 큐 등록
        # API 호출 없음, 스냅샷 내 데이터만 사용
        for ticker, snap in snapshot_map.items():
            if ticker in self._signaled_tickers:
                continue
            if snap["price"] < self.min_price or snap["price"] > self.max_price:
                continue
            if ticker in self._monitoring_queue:
                continue  # 이미 큐에 있음
            # ETF/레버리지 제외
            if _is_etf(ticker):
                continue

            # ★ 올바른 거래량 비교: min.av(당일 누적) ÷ prevDay.v
            # min.v 직전값 비교는 봉 진행에 따른 자연 증가를 폭증으로 오인하는 버그
            cur_accum_v = snap["min"].get("av", 0) or 0   # 당일 누적 거래량
            prev_day_v_raw = snap.get("prev_day", {}).get("v", 0) or 0

            if cur_accum_v > 0 and prev_day_v_raw > 0:
                real_vol_ratio = (cur_accum_v / prev_day_v_raw) * 100
                if real_vol_ratio >= self.vol_3min_ratio_pct:
                    if snap["volume"] >= min_daily_volume or cur_accum_v >= min_daily_volume:
                        # ★ 방향성 확인: 가격이 오르는 중일 때만 등록
                        if snap["scan_delta_pct"] >= 0 or snap["change_pct"] >= 5.0:
                            self._monitoring_queue[ticker] = {
                                "time": scan_time,
                                "price": snap["price"],
                            }
                            logger.info(
                                f"📋 큐 등록: {ticker} 실거래량 {real_vol_ratio:.0f}% "
                                f"(누적:{cur_accum_v:,.0f} / 전일:{prev_day_v_raw:,.0f}) "
                                f"@${snap['price']:.2f} {snap['change_pct']:+.1f}%"
                            )

        # 큐 만료 정리
        expired = [t for t, info in self._monitoring_queue.items()
                   if scan_time - info["time"] > queue_expire_sec]
        for t in expired:
            del self._monitoring_queue[t]
            logger.debug(f"⏰ 큐 만료 제거: {t}")

        # ── STEP 2: 큐 종목 중 20%+ 가격 상승 → 즉시 매수 후보 ─
        # ★ API 호출 없음, 메모리 연산만 → ~0ms
        candidates = []

        for ticker in list(self._monitoring_queue.keys()):
            if ticker in self._signaled_tickers:
                continue
            snap = snapshot_map.get(ticker)
            if not snap:
                continue

            queue_info = self._monitoring_queue[ticker]
            queue_price = queue_info["price"]

            # ★ 20%+ 가격 상승 확인
            if snap["change_pct"] < self.price_change_pct:
                continue

            # ★ 방향성 확인: 현재 가격이 큐 등록 시점 가격 이상 (꺾이면 제외)
            if snap["price"] < queue_price * 0.97:  # 3% 여유 (일시적 눌림 허용)
                logger.debug(f"⬇️ {ticker} 가격 꺾임 — 매수 보류 (큐등록${queue_price:.2f}→현재${snap['price']:.2f})")
                continue

            prev_day_vol = snap.get("prev_day", {}).get("v", 0) or 0
            cur_accum = snap["min"].get("av", 0) or 0
            volume_ratio = (cur_accum / prev_day_vol * 100) if prev_day_vol > 0 and cur_accum > 0 \
                           else ((snap["volume"] / prev_day_vol * 100) if prev_day_vol > 0 else 999.0)
            vol_ratio_3min = volume_ratio  # 이제 동일 기준

            if ticker not in self._surge_logged_expire:
                logger.info(
                    f"🎯 매수 후보: {ticker} ${snap['price']:.2f} "
                    f"{snap['change_pct']:+.1f}% vol_ratio:{vol_ratio_3min:.0f}% "
                    f"(큐등록${queue_price:.2f} → 현재 유지)"
                )

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

        # 변동률 높은 순 정렬
        candidates.sort(key=lambda c: -c["change_pct"])

        if candidates:
            logger.info(f"🔥 최종 후보 {len(candidates)}개 — 즉시 매수 (큐→20%+ 확인, 지연 없음)")
            for c in candidates:
                logger.info(f"  ✅ {c['ticker']} ${c['price']:.2f} {c['change_pct']:+.1f}%")

        # 다음 스캔 비교용으로 저장
        self._prev_prices = {t: s["price"] for t, s in snapshot_map.items() if s["price"] > 0}
        self._prev_min_v = {t: s["min"].get("v", 0) for t, s in snapshot_map.items()
                            if s["min"].get("v", 0) > 0}
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
        self._prev_min_v.clear()
        self._prev_scan_time = 0.0
        self._monitoring_queue.clear()
        self._surge_logged_expire.clear()
        logger.info("🔄 Snapshot 스캐너 세션 리셋")
