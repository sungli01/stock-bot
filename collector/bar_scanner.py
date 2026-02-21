"""
3분봉 거래량 폭증 감지 스레드 (v9)
- 30초마다 스냅샷 후보 종목의 실제 완성된 3분봉 조회
- 1차: 완성봉[N-1].v ÷ 완성봉[N-2].v >= 1000% → 모니터링 큐 등록
- 2차: 1차 완료 종목에 대해 200%+ → 2차 큐 등록 (is_second=True)
- Bug #1 수정: 큐 만료 cleanup을 early return 앞으로 이동
- Bug #5 수정: .WS 워런트 종목 필터 추가
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
    3분봉 기반 거래량 폭증 스캐너 (v9)
    - snapshot_scanner로부터 후보 종목 수신
    - 완성 3분봉 2개 비교 → 모니터링 큐 등록
    - 1차 완료 종목은 200% threshold로 2차 큐 등록 허용
    """

    def __init__(self, config: dict, monitoring_queue: dict, queue_lock: threading.Lock):
        super().__init__(daemon=True)
        self.config = config
        self.scanner_cfg = config.get("scanner", {})

        # [v9] 1차/2차 threshold 분리
        self.vol_ratio_threshold_1st = self.scanner_cfg.get("vol_3min_ratio_pct", 1000.0)
        self.vol_ratio_threshold_2nd = self.scanner_cfg.get("vol_3min_ratio_pct_2nd", 200.0)

        self.scan_interval = self.scanner_cfg.get("bar_scan_interval_sec", 30)
        self.queue_expire_sec = self.scanner_cfg.get("queue_expire_sec", 3600)  # 큐 유효기간 (기본 60분)

        # 공유 객체
        self.monitoring_queue = monitoring_queue  # {ticker: {"time", "price", "is_second"}}
        self.queue_lock = queue_lock

        # 스냅샷에서 전달받은 후보 종목 {ticker: price}
        self._candidates: dict[str, float] = {}
        self._candidates_lock = threading.Lock()

        # [v9] 거래 이력 (1차→2차→3차→완전차단)
        self._traded_once:  set[str] = set()
        self._traded_twice: set[str] = set()
        self._traded_once_lock = threading.Lock()

        # ETF/레버리지 제외 목록
        self._etf_blacklist = {
            "SOXS", "SOXL", "TQQQ", "SQQQ", "UVXY", "SVXY", "VXX", "VIXY",
            "ZSL", "AGQ", "JDST", "JNUG", "LABD", "LABU", "DUST", "NUGT",
            "YANG", "YINN", "FAS", "FAZ", "TZA", "TNA", "ERX", "ERY",
            "KOLD", "BOIL", "SDS", "SH", "QID", "SPXS", "SPXU",
        }

        self._running = True

    def set_candidates(self, candidates: dict[str, float]):
        """스냅샷 스레드가 후보 종목 전달 {ticker: current_price}"""
        with self._candidates_lock:
            self._candidates = candidates.copy()

    def set_traded_twice(self, ticker: str):
        """2차 완료 등록 (3차 허용)"""
        with self._traded_once_lock:
            self._traded_twice.add(ticker)

    def set_traded_once(self, ticker: str):
        """[v9] 1차 매수 완료 종목 등록 → 이후 2차 vol spike 감지 허용"""
        with self._traded_once_lock:
            self._traded_once.add(ticker)
        logger.info(f"🔁 [BarScanner] 1차 완료 등록: {ticker} → 2차 진입 대기")

    def _is_etf(self, ticker: str) -> bool:
        if ticker in self._etf_blacklist:
            return True
        # [Bug #5] 워런트 필터 추가
        if ticker.endswith(".WS") or ticker.endswith("-WS"):
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
                return float(bars[1]["v"]), float(bars[2]["v"])
            elif len(bars) == 2:
                return float(bars[0]["v"]), float(bars[1]["v"])
            return 0.0, 0.0

        except Exception as e:
            logger.debug(f"{ticker} 3분봉 조회 오류: {e}")
            return 0.0, 0.0

    def _scan(self):
        """1회 스캔 실행"""
        now = time.time()

        # ★ [Bug #1 수정] 만료 cleanup 먼저 실행 — candidates 없어도 반드시 실행
        with self.queue_lock:
            expired = [t for t, info in self.monitoring_queue.items()
                       if now - info["time"] > self.queue_expire_sec]
            for t in expired:
                del self.monitoring_queue[t]
                logger.info(f"⏰ 큐 만료 제거: {t}")

        with self._candidates_lock:
            candidates = dict(self._candidates)

        with self._traded_once_lock:
            traded_once  = set(self._traded_once)
            traded_twice = set(self._traded_twice)

        if not candidates:
            return

        scanned = 0

        # [v9] 1차 후보: candidates 중 traded_once가 아닌 것 (1000% threshold)
        # [v9] 2차 후보: candidates 중 traded_once인 것 (200% threshold)
        for ticker, price in candidates.items():
            if self._is_etf(ticker):
                continue

            is_second = ticker in traded_once and ticker not in traded_twice
            is_third  = ticker in traded_twice   # 3차 허용
            is_additional = is_second or is_third

            # 이미 큐에 있으면 스킵 (1차 큐에 있는 동안은 2차 등록 안 함)
            with self.queue_lock:
                if ticker in self.monitoring_queue:
                    continue

            threshold = self.vol_ratio_threshold_2nd if is_additional else self.vol_ratio_threshold_1st

            cur_v, prev_v = self._get_completed_3min_bars(ticker)
            scanned += 1

            if prev_v <= 0 or cur_v <= 0:
                continue

            vol_ratio = (cur_v / prev_v) * 100

            if vol_ratio >= threshold:
                with self.queue_lock:
                    self.monitoring_queue[ticker] = {
                        "time": now,
                        "price": price,
                        "vol_ratio": vol_ratio,
                        "cur_v": cur_v,
                        "prev_v": prev_v,
                        "is_second": is_additional,  # [v9] 2·3차 플래그
                        "is_third":  is_third,
                    }
                entry_type = "3차" if is_third else ("2차" if is_second else "1차")
                logger.info(
                    f"📋 [BarScanner] {entry_type} 큐 등록: {ticker} "
                    f"3분봉 {vol_ratio:.0f}% (기준 {threshold:.0f}%) "
                    f"(완성봉:{cur_v:.0f} / 직전봉:{prev_v:.0f}) "
                    f"@${price:.2f}"
                )
            else:
                logger.debug(
                    f"  ❌ {ticker} 3분봉 거래량 미달: {vol_ratio:.0f}% "
                    f"(기준 {threshold:.0f}%)"
                )

        if scanned > 0:
            logger.debug(f"[BarScanner] {scanned}개 종목 3분봉 체크 완료")

    def run(self):
        logger.info(f"🕯️ BarScanner v9 시작 — 30초마다 3분봉 완성봉 비교 (1차:{self.vol_ratio_threshold_1st:.0f}% / 2차:{self.vol_ratio_threshold_2nd:.0f}%)")
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
        with self._traded_once_lock:
            self._traded_once.clear()
            self._traded_twice.clear()
        logger.info("🔄 BarScanner 세션 리셋")
