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
from typing import Optional

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


# ─── KIS 스캔 백그라운드 스레드 ─────────────────────────
class KISScanThread(threading.Thread):
    """KIS 현재가 API로 워치리스트를 백그라운드 스캔 (별도 스레드)"""

    def __init__(self, kis_scanner):
        super().__init__(daemon=True)
        self.scanner = kis_scanner
        self.latest_candidates: list[dict] = []
        self.lock = threading.Lock()
        self._running = True

    def run(self):
        logger.info("🚀 KIS 스캔 스레드 시작")
        while self._running:
            try:
                result = self.scanner.scan_once()
                with self.lock:
                    self.latest_candidates = result
                if result:
                    logger.info(f"🔥 KIS 스캔: {len(result)}개 후보 갱신")
            except Exception as e:
                logger.error(f"KIS 스캔 오류: {e}", exc_info=True)
            time.sleep(5)  # 스캔 사이 5초 대기

    def get_candidates(self) -> list[dict]:
        with self.lock:
            return list(self.latest_candidates)

    def stop(self):
        self._running = False


def merge_candidates(polygon_candidates: list[dict], kis_candidates: list[dict]) -> list[dict]:
    """Polygon + KIS 후보 병합 (중복 제거, KIS 우선)"""
    seen = {}
    # KIS 결과 먼저 (실시간 데이터 우선)
    for c in kis_candidates:
        seen[c["ticker"]] = c
    # Polygon 결과 (중복 아닌 것만)
    for c in polygon_candidates:
        if c["ticker"] not in seen:
            seen[c["ticker"]] = c
    return list(seen.values())


class BatchNotifier:
    """알림 메시지를 모아서 1분마다 배치 전송"""

    def __init__(self):
        self._queue: list[str] = []
        self._sent_set: set[str] = set()  # 중복 방지 (후보 알림 등)
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.FLUSH_INTERVAL = 60  # 1분

    def add(self, text: str, dedup_key: str = ""):
        """메시지 큐에 추가. dedup_key가 있으면 같은 키 중복 전송 방지"""
        with self._lock:
            if dedup_key:
                if dedup_key in self._sent_set:
                    return
                self._sent_set.add(dedup_key)
            self._queue.append(text)

    def flush_if_ready(self):
        """1분 경과 시 큐에 쌓인 메시지를 합쳐서 한번에 전송"""
        now = time.time()
        if now - self._last_flush < self.FLUSH_INTERVAL:
            return
        self._last_flush = now
        with self._lock:
            if not self._queue:
                return
            combined = "\n\n".join(self._queue)
            self._queue.clear()
        _send_telegram(combined)

    def force_flush(self):
        """즉시 전송 (세션 시작, 강제청산 등 중요 알림)"""
        with self._lock:
            if not self._queue:
                return
            combined = "\n\n".join(self._queue)
            self._queue.clear()
        self._last_flush = time.time()
        _send_telegram(combined)

    def send_immediate(self, text: str):
        """즉시 단독 전송 (5분 상태보고 등)"""
        _send_telegram(text)

    def reset_dedup(self):
        """세션 리셋 시 중복 세트 초기화"""
        with self._lock:
            self._sent_set.clear()


def _send_telegram(text: str):
    """텔레그램 실제 전송 (내부용)"""
    try:
        from notifier.telegram_bot import TelegramNotifier
        TelegramNotifier().send_sync(text)
    except Exception as e:
        logger.warning(f"알림 실패: {e}")


# 글로벌 배치 알림 인스턴스
_notifier = BatchNotifier()


def send_notification(text: str, dedup_key: str = "", immediate: bool = False):
    """텔레그램 알림 (배치 전송, immediate=True면 즉시)"""
    if immediate:
        _notifier.send_immediate(text)
    else:
        _notifier.add(text, dedup_key=dedup_key)


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
    from collector.kis_scanner import KISScanner
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

    # KIS 스캐너 (백그라운드 스레드)
    kis_scanner = KISScanner(config)
    # signaled 세트 공유 (중복 매수 방지)
    kis_scanner.share_signaled(scanner._signaled_tickers)
    kis_thread = KISScanThread(kis_scanner)
    kis_thread.start()

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
    last_status_report = 0  # 5분마다 상태 보고
    STATUS_INTERVAL = 300  # 5분
    scan_count = 0
    session_start_notified = False

    while running:
        try:
            now = now_kst()

            # ── 매매 시간 외 ─────────────────────────────
            if not is_trading_window():
                if not sleep_logged:
                    logger.info("💤 매매 시간 외 — 대기 중")
                    # 세션 리셋
                    scanner.reset_session()
                    kis_scanner.reset_session()
                    bb_trailing.reset()
                    _notifier.reset_dedup()
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

            # 세션 시작 알림 (1회)
            if not session_start_notified:
                session_start_notified = True
                send_notification(
                    f"🟢 매매 세션 시작\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"시간: {now.strftime('%H:%M KST')}\n"
                    f"거래일: {trading_date}\n"
                    f"max_positions: {max_positions}\n"
                    f"━━━━━━━━━━━━━━"
                )
                _notifier.force_flush()

            # ── 강제청산 체크 ─────────────────────────────
            remaining = minutes_until_session_end()
            if 0 < remaining <= force_close_before_min:
                logger.warning(f"🚨 장마감 {remaining:.0f}분 전 — 강제청산")
                executor.force_close_all_positions()
                send_notification(
                    f"🚨 장마감 강제청산 실행\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"잔여: {remaining:.0f}분\n"
                    f"총 스캔: {scan_count}회\n"
                    f"━━━━━━━━━━━━━━",
                    immediate=True
                )
                session_start_notified = False
                scan_count = 0
                time.sleep(60)
                continue

            # ── Snapshot 스캔 + KIS 결과 병합 ─────────────
            candidates = scanner.scan_once()
            kis_candidates = kis_thread.get_candidates()
            candidates = merge_candidates(candidates, kis_candidates)

            # ── 시장 거버넌스 업데이트 ────────────────────
            governor.update_market_data(scanner._last_snapshot)
            market_state = governor.evaluate_state()
            adjusted_cap = governor.get_adjusted_cap()
            executor.compound_cap = min(adjusted_cap, ABSOLUTE_CAP)

            if not governor.should_trade():
                logger.warning(f"🛑 급락장 감지 — 매매 중단 (SPY {governor.market_info['spy_change']:+.1f}%)")
                send_notification(f"🛑 급락장 감지 — 매매 중단\nSPY: {governor.market_info['spy_change']:+.1f}%", immediate=True)
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

            # ── 주기적 상태 보고 (5분마다) ────────────────
            scan_count += 1
            now_ts = time.time()
            if now_ts - last_status_report >= STATUS_INTERVAL:
                last_status_report = now_ts
                pos_lines = []
                for pos in positions:
                    t = pos["ticker"]
                    avg = pos.get("avg_price", 0)
                    snap_p = scanner.get_price(t) or pos.get("current_price", 0)
                    pnl = ((snap_p / avg - 1) * 100) if avg > 0 and snap_p else 0
                    trailing_info = bb_trailing.get_status(t) if hasattr(bb_trailing, 'get_status') else {}
                    peak_str = f" 고점${trailing_info.get('peak',0):.2f}" if trailing_info.get('peak') else ""
                    pos_lines.append(f"  {t}: ${snap_p:.2f} ({pnl:+.1f}%){peak_str}")

                status_text = (
                    f"📊 상태 보고 ({now.strftime('%H:%M KST')})\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"스캔 횟수: {scan_count}회\n"
                    f"시장: {market_state} (cap ₩{adjusted_cap:,.0f})\n"
                    f"보유: {current_count}/{max_positions}\n"
                )
                if pos_lines:
                    status_text += "\n".join(pos_lines) + "\n"
                status_text += f"━━━━━━━━━━━━━━\n장마감까지: {remaining:.0f}분"
                send_notification(status_text, immediate=True)

            # ── 신규 매수 평가 ────────────────────────────
            if candidates and current_count < max_positions:
                # 후보 감지 알림 (중복 제거)
                new_cands = [c for c in candidates[:5] if c['ticker'] not in _notifier._sent_set]
                if new_cands:
                    cand_text = "🔍 후보 감지\n"
                    for c in new_cands:
                        cand_text += f"  {c['ticker']}: ${c['price']:.2f} ({c['change_pct']:+.1f}%) vol:{c.get('volume_ratio', 0):.0f}%\n"
                    dedup = "|".join(c['ticker'] for c in new_cands)
                    send_notification(cand_text.strip(), dedup_key=f"cand:{dedup}")

                for cand in candidates:
                    if current_count >= max_positions:
                        break

                    ticker = cand["ticker"]

                    # 시그널 평가
                    sig = analyzer.evaluate(ticker, cand)
                    if not sig or sig["signal"] != "BUY":
                        continue

                    if sig["confidence"] < 50:
                        send_notification(f"⏭️ {ticker} 신뢰도 부족 ({sig['confidence']:.0f}%) — 패스")
                        continue

                    # 매수 실행
                    price = cand["price"]
                    logger.info(f"📈 {ticker} 매수 진입 (신뢰도 {sig['confidence']:.0f}%, ${price:.2f})")
                    send_notification(
                        f"📈 {ticker} 매수 시도\n"
                        f"가격: ${price:.2f} ({cand['change_pct']:+.1f}%)\n"
                        f"신뢰도: {sig['confidence']:.0f}%\n"
                        f"거래량비: {cand.get('volume_ratio', 0):.0f}%"
                    )

                    orders = executor.execute_buy(ticker, price)
                    # 체결 여부와 무관하게 같은 종목 반복 시도 방지
                    scanner.mark_signaled(ticker)

                    if orders:
                        current_count += 1
                        store.save_signal(sig)
                        send_notification(
                            f"✅ {ticker} 매수 완료\n"
                            f"가격: ${price:.2f}\n"
                            f"변동: {cand['change_pct']:+.1f}%\n"
                            f"신뢰도: {sig['confidence']:.0f}%"
                        )
                    else:
                        send_notification(f"❌ {ticker} 매수 실패 — 잔고 부족 또는 주문 오류")
                        logger.warning(f"⚠️ {ticker} 매수 실패 (호가 조회 실패 등) — 스킵 처리")
            elif candidates and current_count >= max_positions:
                # 포지션 풀인데 후보가 있는 경우 알림
                missed = [f"{c['ticker']}({c['change_pct']:+.0f}%)" for c in candidates[:3]]
                if missed and now_ts - last_status_report < 10:  # 상태보고 직후에만
                    send_notification(f"⚠️ 포지션 풀 ({current_count}/{max_positions}) — 후보 놓침: {', '.join(missed)}")

            # 배치 알림 플러시 (1분 경과 시)
            _notifier.flush_if_ready()

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
