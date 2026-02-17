"""
stock-bot 엔트리포인트
- multiprocessing으로 3모듈 실행 (Collector, Analyzer, Trader)
- 스케줄러 (18:00 시작, 06:00 종료)
- 헬스체크 + 자동 재시작
"""
import os
import sys
import time
import signal
import logging
import multiprocessing as mp
from datetime import datetime

import redis
import yaml
from dotenv import load_dotenv

# .env 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def load_config() -> dict:
    with open("config/config.yaml", "r") as f:
        return yaml.safe_load(f)


def get_redis() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )


# ─── 모듈 프로세스 함수 ──────────────────────────────────
def run_collector(config: dict):
    """Collector 프로세스: 전종목 스캔 + 1차 필터링"""
    from collector.scanner import StockScanner
    logger = logging.getLogger("collector")
    logger.info("🚀 Collector 시작")

    r = get_redis()
    scanner = StockScanner(r, config)
    scanner.run_loop(interval_sec=60)


def run_analyzer(config: dict):
    """Analyzer 프로세스: 추세 판단 + 시그널 생성"""
    from analyzer.signal import SignalGenerator
    logger = logging.getLogger("analyzer")
    logger.info("🚀 Analyzer 시작")

    r = get_redis()
    generator = SignalGenerator(r, config)
    generator.run_subscriber()


def run_trader(config: dict):
    """Trader 프로세스: 매매 실행"""
    from trader.executor import TradeExecutor
    logger = logging.getLogger("trader")
    logger.info("🚀 Trader 시작")

    r = get_redis()
    executor = TradeExecutor(r, config)
    executor.run_subscriber()


# ─── 스케줄 관리 ─────────────────────────────────────────
def is_trading_hours(config: dict) -> bool:
    """현재 매매 시간인지 확인 (KST 기준 18:00~06:00)"""
    import pytz
    tz = pytz.timezone(config.get("schedule", {}).get("timezone", "Asia/Seoul"))
    now = datetime.now(tz)
    hour = now.hour

    # 18:00 ~ 23:59 또는 00:00 ~ 06:00
    start = int(config.get("schedule", {}).get("start_time", "18:00").split(":")[0])
    end = int(config.get("schedule", {}).get("market_close", "06:00").split(":")[0])

    if start > end:  # 18~06 (자정 넘김)
        return hour >= start or hour < end
    else:
        return start <= hour < end


# ─── 프로세스 관리 ────────────────────────────────────────
class ProcessManager:
    """3모듈 프로세스 관리 — 헬스체크 + 자동 재시작"""

    def __init__(self, config: dict):
        self.config = config
        self.processes: dict[str, mp.Process] = {}
        self.running = True

    def start_all(self):
        """모든 모듈 시작"""
        modules = {
            "collector": run_collector,
            "analyzer": run_analyzer,
            "trader": run_trader,
        }
        for name, func in modules.items():
            self._start_process(name, func)

    def _start_process(self, name: str, func):
        """개별 프로세스 시작"""
        p = mp.Process(target=func, args=(self.config,), name=name, daemon=True)
        p.start()
        self.processes[name] = p
        logger.info(f"  ✅ {name} 프로세스 시작 (PID: {p.pid})")

    def health_check(self):
        """헬스체크 — 죽은 프로세스 자동 재시작"""
        module_funcs = {
            "collector": run_collector,
            "analyzer": run_analyzer,
            "trader": run_trader,
        }
        for name, proc in list(self.processes.items()):
            if not proc.is_alive():
                logger.warning(f"⚠️ {name} 프로세스 사망 — 재시작")
                self._start_process(name, module_funcs[name])

    def stop_all(self):
        """모든 프로세스 종료"""
        self.running = False
        for name, proc in self.processes.items():
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
                logger.info(f"  🛑 {name} 종료")

    def run(self):
        """메인 루프 — 스케줄 관리 + 헬스체크"""
        logger.info("=" * 50)
        logger.info("🤖 stock-bot 시작")
        logger.info("=" * 50)

        # 시그널 핸들러
        def shutdown(signum, frame):
            logger.info("종료 시그널 수신")
            self.stop_all()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        self.start_all()

        while self.running:
            try:
                # 헬스체크 (30초 간격)
                self.health_check()

                # 매매 시간 외에는 휴면
                if not is_trading_hours(self.config):
                    logger.info("💤 매매 시간 외 — 휴면 중 (5분 간격 체크)")
                    time.sleep(300)
                else:
                    time.sleep(30)

            except KeyboardInterrupt:
                break

        self.stop_all()


# ─── 엔트리포인트 ─────────────────────────────────────────
if __name__ == "__main__":
    config = load_config()

    # Redis 연결 테스트
    try:
        r = get_redis()
        r.ping()
        logger.info("✅ Redis 연결 성공")
    except Exception as e:
        logger.error(f"❌ Redis 연결 실패: {e}")
        logger.info("💡 docker-compose up -d 로 Redis를 먼저 실행하세요")
        sys.exit(1)

    manager = ProcessManager(config)
    manager.run()
