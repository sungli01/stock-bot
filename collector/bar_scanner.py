"""
3분봉 거래량 폭증 감지 스레드 (v8.3)
- 30초마다 스냅샷 후보 종목의 실제 완성된 3분봉 조회
- 완성봉[N-1].v ÷ 완성봉[N-2].v >= 1000% → 모니터링 큐 등록
- 스냅샷 내 파생 데이터(min.av 등) 미사용 — aggs API 직접 조회
"""
import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/3/minute/{from_date}/{to_date}"


class BarScanner(threading.Thread):
    """
    3분봉 기반 거래량 폭증 스캐너
    - snapshot_scanner로부터 후보 종목 수신
    - 완성 3분봉 2개 비교 → 모니터링 큐 등록
    """

    def __init__(self, config: dict, monitoring_queue: dict, queue_lock: threading.Lock):
        super().__init__(daemon=True)
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.vol_ratio_threshold = self.scanner_cfg.get("vol_3min_ratio_pct", 1000.0)
        self.scan_interval = self.scanner_cfg.get("bar_scan_interval_sec", 30)
        self.queue_expire_sec = 900  # 큐 유효기간 15분

        # 공유 객체
        self.monitoring_queue = monitoring_queue  # {ticker: {"time", "price"}}
        self.queue_lock = queue_lock

        # 스냅샷에서 전달받은 후보 종목 {ticker: price}
        self._candidates: dict[str, float] = {}
        self._candidates_lock = threading.Lock()

        # ETF/레버리지 제외 목록
        self._etf_blacklist = {
            "SOXS", "SOXL", "TQQQ", "SQQQ", "UVXY", "SVXY", "VXX", "VIXY",
            "ZSL", "AGQ", "JDST", "JNUG", "LABD", "LABU", "DUST", "NUGT",
            "YANG", "YINN", "FAS", "FAZ", "TZA", "TNA", "ERX", "ERY",
            "KOLD", "BOIL", "SDS", "SH", "QID", "SPXS", "SPXU",
        }

        self._running = True

    def set_candidates(self, candidates: dict[str, float]):
        """스냅샷 스레드가 5%+ 후보 종목 전달 {ticker: current_price}"""
        with self._candidates_lock:
            self._candidates = candidates.copy()

    def _is_etf(self, ticker: str) -> bool:
        if ticker in self._etf_blacklist:
            return True
        if len(ticker) >= 4 and ticker[-1] in ("S", "L") and ticker[-2].isdigit():
            return True
        return False

    def _get_completed_3min_bars(self, ticker: str) -> tuple[float, float]:
        """
        Polygon aggs API로 완성된 3분봉 2개 거래량 반환
        Returns: (최신완성봉.v, 직전완성봉.v)
        sort=desc, limit=3 → [0]=현재진행중(미완성), [1]=최신완성, [2]=직전완성
        """
        try:
            now_utc = datetime.now(timezone.utc)
            # 오늘 날짜 (ET 기준 장 시작일)
            today = now_utc.strftime("%Y-%m-%d")
            yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

            url = AGGS_URL.format(ticker=ticker, from_date=yesterday, to_date=today)
            resp = requests.get(url, params={
                "adjusted": "true",
                "sort": "desc",
                "limit": 3,
                "apiKey": POLYGON_API_KEY,
            }, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            bars = data.get("results", [])

            if len(bars) >= 3:
                # bars[0]: 현재 진행 중 (미완성) → 제외
                # bars[1]: 최신 완성봉 (N-1)
                # bars[2]: 직전 완성봉 (N-2)
                return float(bars[1]["v"]), float(bars[2]["v"])
            elif len(bars) == 2:
                # 봉이 2개뿐이면 둘 다 완성봉으로 처리
                return float(bars[0]["v"]), float(bars[1]["v"])
            return 0.0, 0.0

        except Exception as e:
            logger.debug(f"{ticker} 3분봉 조회 오류: {e}")
            return 0.0, 0.0

    def _scan(self):
        """1회 스캔 실행"""
        with self._candidates_lock:
            candidates = dict(self._candidates)

        if not candidates:
            return

        now = time.time()
        scanned = 0

        for ticker, price in candidates.items():
            if self._is_etf(ticker):
                continue

            # 이미 큐에 있으면 스킵
            with self.queue_lock:
                if ticker in self.monitoring_queue:
                    continue

            # 완성된 3분봉 2개 조회
            cur_v, prev_v = self._get_completed_3min_bars(ticker)
            scanned += 1

            if prev_v <= 0 or cur_v <= 0:
                continue

            vol_ratio = (cur_v / prev_v) * 100

            if vol_ratio >= self.vol_ratio_threshold:
                with self.queue_lock:
                    self.monitoring_queue[ticker] = {
                        "time": now,
                        "price": price,          # 큐 등록 시점 가격
                        "vol_ratio": vol_ratio,  # 거래량 폭증 비율
                        "cur_v": cur_v,
                        "prev_v": prev_v,
                    }
                logger.info(
                    f"📋 [BarScanner] 큐 등록: {ticker} "
                    f"3분봉 {vol_ratio:.0f}% "
                    f"(완성봉:{cur_v:.0f} / 직전봉:{prev_v:.0f}) "
                    f"@${price:.2f}"
                )
            else:
                logger.debug(
                    f"  ❌ {ticker} 3분봉 거래량 미달: {vol_ratio:.0f}% "
                    f"(기준 {self.vol_ratio_threshold:.0f}%)"
                )

        # 만료된 큐 항목 정리
        with self.queue_lock:
            expired = [t for t, info in self.monitoring_queue.items()
                       if now - info["time"] > self.queue_expire_sec]
            for t in expired:
                del self.monitoring_queue[t]
                logger.debug(f"⏰ 큐 만료 제거: {t}")

        if scanned > 0:
            logger.debug(f"[BarScanner] {scanned}개 종목 3분봉 체크 완료")

    def run(self):
        logger.info(f"🕯️ BarScanner 시작 — 30초마다 3분봉 완성봉 비교")
        while self._running:
            try:
                self._scan()
            except Exception as e:
                logger.error(f"[BarScanner] 스캔 오류: {e}", exc_info=True)
            time.sleep(self.scan_interval)

    def stop(self):
        self._running = False

    def reset_session(self):
        """새 세션 시작 시 초기화"""
        with self._candidates_lock:
            self._candidates.clear()
        with self.queue_lock:
            self.monitoring_queue.clear()
        logger.info("🔄 BarScanner 세션 리셋")
