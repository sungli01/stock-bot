"""
stock-bot 실전 엔트리포인트
- Snapshot 기반 실시간 스캔 (2초 간격)
- BB 트레일링 스탑 기반 매도
- Post-trade 추적
- Railway 안정 배포
"""
import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    with open(cfg_path, "r") as f:
        return yaml.safe_load(f)


# ─── 헬스체크 서버 (Railway용) ────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        pass  # suppress logs


def start_health_server(port: int = 8080):
    """비동기 헬스체크 HTTP 서버"""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"🏥 헬스체크 서버 시작 (port {port})")
    except Exception as e:
        logger.warning(f"헬스체크 서버 실패: {e}")


def send_notification(text: str):
    """텔레그램 알림 (실패해도 무시)"""
    try:
        from notifier.telegram_bot import TelegramNotifier
        TelegramNotifier().send_sync(text)
    except Exception as e:
        logger.warning(f"알림 실패: {e}")


# ─── 메인 트레이딩 루프 ──────────────────────────────────
def run_live(config: dict):
    """
    실전 트레이딩 메인 루프
    - Snapshot 스캔 (2초 간격)
    - 시그널 평가 → 매수
    - 보유종목 BB 트레일링 모니터링 → 매도
    - 장마감 15분전 강제청산
    """
    from collector.snapshot_scanner import SnapshotScanner
    from analyzer.signal import SignalGenerator
    from trader.executor import TradeExecutor
    from trader.bb_trailing import BBTrailingStop
    from trader.market_governor import MarketGovernor, ABSOLUTE_CAP
    from trader.market_hours import (
        is_trading_window, minutes_until_session_end,
        get_all_timestamps, get_trading_date, now_kst,
    )
    from knowledge.file_store import FileStore
    from knowledge.post_trade_tracker import PostTradeTracker

    scanner = SnapshotScanner(config)
    analyzer = SignalGenerator(None, config)
    executor = TradeExecutor(None, config)
    bb_trailing = BBTrailingStop(config)
    governor = MarketGovernor(config)
    store = FileStore()
    tracker = PostTradeTracker()

    trading_cfg = config.get("trading", {})
    max_positions = trading_cfg.get("max_positions", 2)
    allocation_ratio = trading_cfg.get("allocation_ratio", [0.7, 0.3])
    force_close_before_min = trading_cfg.get("force_close_before_min", 15)

    SCAN_INTERVAL = 2  # seconds
    SLEEP_CHECK_INTERVAL = 300  # 5min when outside trading hours

    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("종료 시그널 수신")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    sleep_logged = False
    last_post_trade_update = None

    while running:
        try:
            now = now_kst()

            # ── 매매 시간 외 ─────────────────────────────
            if not is_trading_window():
                if not sleep_logged:
                    logger.info("💤 매매 시간 외 — 대기 중")
                    # 세션 리셋
                    scanner.reset_session()
                    bb_trailing.reset()
                    sleep_logged = True

                    # 장 마감 후 post-trade 업데이트 (1일 1회)
                    today = now.strftime("%Y-%m-%d")
                    if last_post_trade_update != today:
                        try:
                            tracker.update_all()
                            last_post_trade_update = today
                        except Exception as e:
                            logger.error(f"Post-trade 업데이트 실패: {e}")

                time.sleep(SLEEP_CHECK_INTERVAL)
                continue

            sleep_logged = False
            trading_date = get_trading_date()

            # ── 강제청산 체크 ─────────────────────────────
            remaining = minutes_until_session_end()
            if 0 < remaining <= force_close_before_min:
                logger.warning(f"🚨 장마감 {remaining:.0f}분 전 — 강제청산")
                executor.force_close_all_positions()
                send_notification(f"🚨 장마감 강제청산 실행 (잔여 {remaining:.0f}분)")
                time.sleep(60)
                continue

            # ── Snapshot 스캔 ─────────────────────────────
            candidates = scanner.scan_once()

            # ── 시장 거버넌스 업데이트 ────────────────────
            governor.update_market_data(scanner._last_snapshot)
            market_state = governor.evaluate_state()
            adjusted_cap = governor.get_adjusted_cap()
            executor.compound_cap = min(adjusted_cap, ABSOLUTE_CAP)

            if not governor.should_trade():
                logger.warning(f"🛑 급락장 감지 — 매매 중단 (SPY {governor.market_info['spy_change']:+.1f}%)")
                time.sleep(30)
                continue

            # ── 보유종목 모니터링 (BB 트레일링) ───────────
            balance = executor.kis.get_balance()
            positions = balance.get("positions", [])
            current_count = len(positions)

            for pos in positions:
                ticker = pos["ticker"]
                avg_price = pos["avg_price"]
                # snapshot에서 실시간 가격 가져오기
                snap_price = scanner.get_price(ticker)
                current_price = snap_price or pos.get("current_price") or executor.kis.get_current_price(ticker)

                if not current_price:
                    continue

                exit_signal = bb_trailing.check_exit(ticker, current_price, avg_price)
                if exit_signal:
                    action = exit_signal["action"]
                    reason = exit_signal["reason"]
                    pnl_pct = exit_signal["pnl_pct"]

                    logger.info(f"{'🚨' if action == 'STOP' else '💰'} {ticker} {reason}")

                    if action == "STOP":
                        executor.execute_stop_loss(ticker)
                    else:
                        executor.execute_sell(ticker)

                    # Post-trade 기록
                    try:
                        tracker.record_trade(ticker, trading_date, {
                            "side": "SELL",
                            "reason": reason,
                            "pnl_pct": pnl_pct,
                            "avg_price": avg_price,
                            "exit_price": current_price,
                            "quantity": pos.get("quantity", 0),
                        })
                    except Exception as e:
                        logger.error(f"Post-trade 기록 실패: {e}")

                    send_notification(
                        f"{'🚨' if action == 'STOP' else '💰'} {ticker} 매도\n"
                        f"사유: {reason}\n"
                        f"수익률: {pnl_pct:+.1f}%"
                    )
                    current_count -= 1

            # ── 신규 매수 평가 ────────────────────────────
            if candidates and current_count < max_positions:
                for cand in candidates:
                    if current_count >= max_positions:
                        break

                    ticker = cand["ticker"]

                    # 시그널 평가
                    sig = analyzer.evaluate(ticker, cand)
                    if not sig or sig["signal"] != "BUY":
                        continue

                    if sig["confidence"] < 65:
                        continue

                    # 매수 실행
                    price = cand["price"]
                    logger.info(f"📈 {ticker} 매수 진입 (신뢰도 {sig['confidence']:.0f}%, ${price:.2f})")

                    orders = executor.execute_buy(ticker, price)
                    if orders:
                        scanner.mark_signaled(ticker)
                        current_count += 1

                        store.save_signal(sig)
                        send_notification(
                            f"✅ {ticker} 매수 완료\n"
                            f"가격: ${price:.2f}\n"
                            f"변동: {cand['change_pct']:+.1f}%\n"
                            f"신뢰도: {sig['confidence']:.0f}%"
                        )

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"루프 오류: {e}", exc_info=True)
            time.sleep(10)

    logger.info("🛑 stock-bot 종료")


# ─── 엔트리포인트 ─────────────────────────────────────────
if __name__ == "__main__":
    config = load_config()

    is_railway = os.getenv("RAILWAY", "").lower() in ("1", "true", "yes") or os.getenv("RAILWAY_ENVIRONMENT", "")
    port = int(os.getenv("PORT", "8080"))

    # Railway: 헬스체크 서버 시작
    if is_railway:
        start_health_server(port)

    # 시작 로그 (텔레그램 알림 제거 — 재배포마다 반복 방지)
    mode = "railway" if is_railway else "local"
    logger.info(f"🤖 stock-bot 시작 (모드: {mode})")

    run_live(config)
