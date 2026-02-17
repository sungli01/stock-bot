"""
stock-bot 엔트리포인트
- Redis 있으면 multiprocessing (pub/sub), 없으면 standalone 순차 실행
- DB 없으면 JSON 파일 fallback
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


def try_redis():
    """Redis 연결 시도. 성공하면 redis.Redis 반환, 실패하면 None"""
    try:
        import redis
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            decode_responses=True,
        )
        r.ping()
        return r
    except Exception as e:
        logger.warning(f"⚠️ Redis 연결 실패: {e}")
        return None


def send_startup_notification(mode: str):
    """시작 알림 전송"""
    try:
        from notifier.telegram_bot import TelegramNotifier
        notifier = TelegramNotifier()
        notifier.send_sync(f"🤖 StockBot 시작 (모드: {mode})")
    except Exception as e:
        logger.warning(f"텔레그램 알림 실패: {e}")


# ─── Redis 모드: 모듈 프로세스 함수 ──────────────────────
def run_collector(config: dict):
    """Collector 프로세스: 전종목 스캔 + 1차 필터링"""
    from collector.scanner import StockScanner
    import redis
    logger = logging.getLogger("collector")
    logger.info("🚀 Collector 시작")

    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )
    scanner = StockScanner(r, config)
    scanner.run_loop(interval_sec=60)


def run_analyzer(config: dict):
    """Analyzer 프로세스: 추세 판단 + 시그널 생성"""
    from analyzer.signal import SignalGenerator
    import redis
    logger = logging.getLogger("analyzer")
    logger.info("🚀 Analyzer 시작")

    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )
    generator = SignalGenerator(r, config)
    generator.run_subscriber()


def run_trader(config: dict):
    """Trader 프로세스: 매매 실행"""
    from trader.executor import TradeExecutor
    import redis
    logger = logging.getLogger("trader")
    logger.info("🚀 Trader 시작")

    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )
    executor = TradeExecutor(r, config)
    executor.run_subscriber()


# ─── Standalone 모드: 순차 실행 ──────────────────────────
def run_standalone_cycle(config: dict):
    """
    Redis 없이 Collector→Analyzer→Trader 순차 실행
    매매일 기준: KST 18:00 ~ 익일 06:00 = 1세션
    KST 18:00부터 매매 가능 (프리마켓 포함)
    """
    from collector.scanner import StockScanner
    from analyzer.signal import SignalGenerator
    from trader.executor import TradeExecutor
    from knowledge.file_store import FileStore
    from trader.market_hours import get_all_timestamps, get_trading_date, minutes_until_session_end

    store = FileStore()
    trading_date = get_trading_date()
    ts = get_all_timestamps()

    scanner = StockScanner(None, config)
    analyzer = SignalGenerator(None, config)
    executor = TradeExecutor(None, config)

    # 세션 종료 임박 시 강제청산 우선 실행
    if executor.should_force_close():
        remaining = minutes_until_session_end()
        logger.warning(f"🚨 [{trading_date}] 세션 종료 {remaining:.0f}분 전 — 강제청산 실행")
        executor.force_close_all_positions()
        return

    logger.info(f"🔍 [{trading_date}] Collector 스캔 시작 (KST {ts['kst']})")
    screened = scanner.scan_once()
    logger.info(f"  → {len(screened)}개 종목 통과")

    for data in screened:
        ticker = data.get("ticker")
        if not ticker:
            continue

        sig = analyzer.evaluate(ticker, data)
        if not sig:
            continue

        sig["timestamps"] = get_all_timestamps()
        sig["trading_date"] = trading_date
        store.save_signal(sig)

        if sig["signal"] in ("BUY", "SELL", "STOP"):
            logger.info(f"📊 [{trading_date}] {ticker} → {sig['signal']} (신뢰도 {sig['confidence']:.0f}%)")

            if sig["signal"] == "BUY":
                executor.execute_buy(ticker, sig.get("price", 0))
            elif sig["signal"] == "SELL":
                executor.execute_sell(ticker)
            elif sig["signal"] == "STOP":
                executor.execute_stop_loss(ticker)

    # 보유 종목 손절/익절 체크
    executor.check_positions()


# ─── 스케줄 관리 ─────────────────────────────────────────
def is_trading_hours(config: dict) -> bool:
    """현재 매매 시간인지 확인 — trader/market_hours.py 기준"""
    from trader.market_hours import is_trading_window
    return is_trading_window()


# ─── 프로세스 관리 (Redis 모드) ───────────────────────────
class ProcessManager:
    """3모듈 프로세스 관리 — 헬스체크 + 자동 재시작"""

    def __init__(self, config: dict):
        self.config = config
        self.processes: dict[str, mp.Process] = {}
        self.running = True

    def start_all(self):
        modules = {
            "collector": run_collector,
            "analyzer": run_analyzer,
            "trader": run_trader,
        }
        for name, func in modules.items():
            self._start_process(name, func)

    def _start_process(self, name: str, func):
        p = mp.Process(target=func, args=(self.config,), name=name, daemon=True)
        p.start()
        self.processes[name] = p
        logger.info(f"  ✅ {name} 프로세스 시작 (PID: {p.pid})")

    def health_check(self):
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
        self.running = False
        for name, proc in self.processes.items():
            try:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
            except (AssertionError, Exception):
                pass
            logger.info(f"  🛑 {name} 종료")

    def run(self):
        logger.info("=" * 50)
        logger.info("🤖 stock-bot 시작 (모드: railway)")
        logger.info("=" * 50)

        def shutdown(signum, frame):
            logger.info("종료 시그널 수신")
            self.stop_all()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        self.start_all()

        while self.running:
            try:
                self.health_check()
                if not is_trading_hours(self.config):
                    logger.info("💤 매매 시간 외 — 휴면 중 (5분 간격 체크)")
                    time.sleep(300)
                else:
                    time.sleep(30)
            except KeyboardInterrupt:
                break

        self.stop_all()


# ─── Standalone 모드 루프 ─────────────────────────────────
def run_standalone(config: dict):
    """Redis 없이 단독 실행 — 순차 루프"""
    logger.info("=" * 50)
    logger.info("🤖 stock-bot 시작 (모드: standalone)")
    logger.info("=" * 50)

    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("종료 시그널 수신")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    interval = 60  # 스캔 간격 (초)

    sleep_logged = False
    while running:
        try:
            if is_trading_hours(config):
                sleep_logged = False
                run_standalone_cycle(config)
                time.sleep(interval)
            else:
                if not sleep_logged:
                    logger.info("💤 매매 시간 외 — 휴면 중 (10분 간격 체크)")
                    sleep_logged = True
                time.sleep(600)  # 10분 간격
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Standalone 루프 오류: {e}", exc_info=True)
            time.sleep(30)

    logger.info("🛑 stock-bot 종료")


# ─── 엔트리포인트 ─────────────────────────────────────────
if __name__ == "__main__":
    config = load_config()

    # Railway 환경에서는 항상 standalone 모드 사용
    # (Redis 모드는 child process 크래시 루프 발생)
    force_standalone = os.getenv("FORCE_STANDALONE", "").lower() in ("1", "true", "yes")
    
    r = None if force_standalone else try_redis()
    use_redis = r is not None

    mode = "standalone"
    if use_redis:
        mode = "redis"

    logger.info(f"🤖 stock-bot 시작 (모드: {mode})")
    
    # 시작 알림은 1회만 (크래시 루프 방지)
    startup_flag = "/tmp/stockbot_started"
    if not os.path.exists(startup_flag):
        send_startup_notification(mode)
        try:
            with open(startup_flag, "w") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass

    if use_redis:
        manager = ProcessManager(config)
        manager.run()
    else:
        run_standalone(config)
