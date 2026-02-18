"""
E2E 파이프라인 테스트 (가상 데이터)
스캔 → 시그널 → 매수 판단 → BB 트레일링 → 매도 판단 → 텔레그램 알림
"""
import os
import sys
import json
import logging
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("test_e2e")

# ── 가상 스냅샷 데이터 ──────────────────────────────
FAKE_SNAPSHOT = {
    "tickers": [
        {
            "ticker": "TEST1",
            "todaysChangePerc": 8.5,
            "day": {"c": 5.20, "v": 500000, "vw": 5.15},
            "prevDay": {"c": 4.80, "v": 50000},
            "min": {"c": 5.18, "v": 12000},
        },
        {
            "ticker": "TEST2",
            "todaysChangePerc": 12.3,
            "day": {"c": 15.80, "v": 800000, "vw": 15.50},
            "prevDay": {"c": 14.07, "v": 80000},
            "min": {"c": 15.75, "v": 25000},
        },
        {
            "ticker": "SPY",
            "todaysChangePerc": 0.5,
            "day": {"c": 520.0, "v": 50000000, "vw": 519.0},
            "prevDay": {"c": 517.4, "v": 45000000},
            "min": {},
        },
        {
            "ticker": "QQQ",
            "todaysChangePerc": 0.8,
            "day": {"c": 450.0, "v": 30000000, "vw": 449.0},
            "prevDay": {"c": 446.4, "v": 28000000},
            "min": {},
        },
    ]
}

results = {"passed": 0, "failed": 0, "tests": []}

def test(name):
    def decorator(fn):
        def wrapper():
            try:
                fn()
                results["passed"] += 1
                results["tests"].append(f"✅ {name}")
                logger.info(f"✅ {name}")
            except Exception as e:
                results["failed"] += 1
                results["tests"].append(f"❌ {name}: {e}")
                logger.error(f"❌ {name}: {e}")
        return wrapper
    return decorator

import yaml
def load_config():
    with open("config/config.yaml") as f:
        return yaml.safe_load(f)

config = load_config()

# ── Test 1: Snapshot Scanner 필터링 ──────────────────
@test("Snapshot Scanner 필터링")
def test_scanner():
    from collector.snapshot_scanner import SnapshotScanner
    scanner = SnapshotScanner(config)
    
    with patch("collector.snapshot_scanner.requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: FAKE_SNAPSHOT,
            raise_for_status=lambda: None,
        )
        candidates = scanner.scan_once()
    
    tickers = [c["ticker"] for c in candidates]
    assert "TEST1" in tickers, f"TEST1 not found in {tickers}"
    assert "TEST2" in tickers, f"TEST2 not found in {tickers}"
    assert "SPY" not in tickers, "SPY should be filtered out (change < 5%)"
    logger.info(f"  후보: {tickers}")

test_scanner()

# ── Test 2: Signal Generator 평가 ────────────────────
@test("Signal Generator 평가")
def test_signal():
    from analyzer.signal import SignalGenerator
    analyzer = SignalGenerator(None, config)
    
    candidate = {
        "ticker": "TEST1",
        "price": 5.20,
        "change_pct": 8.5,
        "volume": 500000,
        "volume_ratio": 1000,
        "prev_close": 4.80,
    }
    
    sig = analyzer.evaluate("TEST1", candidate)
    assert sig is not None, "Signal should not be None"
    assert "signal" in sig, f"Missing 'signal' key: {sig}"
    assert "confidence" in sig, f"Missing 'confidence' key: {sig}"
    logger.info(f"  시그널: {sig['signal']}, 신뢰도: {sig.get('confidence', 'N/A')}")

test_signal()

# ── Test 3: Market Governor 상태 판단 ────────────────
@test("Market Governor 상태 판단")
def test_governor():
    from trader.market_governor import MarketGovernor, ABSOLUTE_CAP
    gov = MarketGovernor(config)
    
    # 보합 상태 (SPY +0.5%)
    from collector.snapshot_scanner import SnapshotScanner
    scanner = SnapshotScanner(config)
    with patch("collector.snapshot_scanner.requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: FAKE_SNAPSHOT,
            raise_for_status=lambda: None,
        )
        scanner.scan_once()
    
    gov.update_market_data(scanner._last_snapshot)
    state = gov.evaluate_state()
    cap = gov.get_adjusted_cap()
    
    assert state == "neutral", f"Expected neutral, got {state}"
    assert cap <= ABSOLUTE_CAP, f"Cap {cap} exceeds absolute {ABSOLUTE_CAP}"
    logger.info(f"  상태: {state}, 캡: ₩{cap:,}")
    
    # 하락 시뮬레이션
    gov._market_changes = {"SPY": -2.0, "QQQ": -2.5}
    state2 = gov.evaluate_state()
    cap2 = gov.get_adjusted_cap()
    assert state2 == "bear", f"Expected bear, got {state2}"
    logger.info(f"  하락장: {state2}, 캡: ₩{cap2:,}")
    
    # 급락 시뮬레이션
    gov._market_changes = {"SPY": -4.0, "QQQ": -5.0}
    state3 = gov.evaluate_state()
    assert state3 == "crash", f"Expected crash, got {state3}"
    assert not gov.should_trade(), "Should NOT trade in crash"
    logger.info(f"  급락장: {state3}, 매매중단: {not gov.should_trade()}")

test_governor()

# ── Test 4: BB Trailing Stop 로직 ────────────────────
@test("BB Trailing Stop 로직")
def test_bb_trailing():
    from trader.bb_trailing import BBTrailingStop
    bb = BBTrailingStop(config)
    
    # 진입가 $5.00, 현재가 $5.50 (+10%) — 아직 홀드
    result = bb.check_exit("TEST1", 5.50, 5.00)
    logger.info(f"  +10%: {result}")
    
    # 현재가 $4.20 (-16%) — 손절
    result2 = bb.check_exit("TEST1", 4.20, 5.00)
    assert result2 is not None, "Should trigger stop loss at -16%"
    assert result2["action"] == "STOP", f"Expected STOP, got {result2['action']}"
    logger.info(f"  -16%: {result2['reason']}")

test_bb_trailing()

# ── Test 5: Trade Executor (mock KIS) ────────────────
@test("Trade Executor 매수/매도 (mock)")
def test_executor():
    from trader.executor import TradeExecutor
    executor = TradeExecutor(None, config)
    
    # Mock KIS client
    executor.kis = MagicMock()
    executor.kis.get_balance.return_value = {"positions": [], "total_eval": 100000}
    executor.kis.buy_split.return_value = [{"order_id": "test001", "qty": 10}]
    executor.kis.get_current_price.return_value = 5.20
    
    with patch("trader.executor.is_trading_window", return_value=True), \
         patch("trader.executor.minutes_until_session_end", return_value=120):
        orders = executor.execute_buy("TEST1", 5.20)
    
    assert len(orders) > 0, "Should have executed buy orders"
    logger.info(f"  매수 주문: {orders}")
    
    # 매도 테스트
    executor.kis.get_balance.return_value = {
        "positions": [{"ticker": "TEST1", "quantity": 10, "avg_price": 5.00}]
    }
    executor.kis.sell_market.return_value = {"order_id": "sell001", "qty": 10}
    executor.kis.sell_split.return_value = [{"order_id": "sell001", "qty": 10}]
    
    with patch("trader.executor.is_trading_window", return_value=True):
        result = executor.execute_sell("TEST1")
    
    assert result is not None, "Should have executed sell"
    logger.info(f"  매도 주문: {result}")
    
    # 절대 상한 테스트
    assert executor.total_buy_amount <= 25_000_000, f"Cap exceeded: {executor.total_buy_amount}"
    logger.info(f"  투자금: ₩{executor.total_buy_amount:,} (상한 ₩25,000,000)")

test_executor()

# ── Test 6: Telegram 알림 ────────────────────────────
@test("Telegram 알림 전송")
def test_telegram():
    from notifier.telegram_bot import TelegramNotifier
    notifier = TelegramNotifier()
    
    msg = (
        "🧪 E2E 테스트 결과\n"
        f"통과: {results['passed']}/5\n"
        f"실패: {results['failed']}/5\n\n"
        + "\n".join(results["tests"])
    )
    notifier.send_sync(msg)
    logger.info("  텔레그램 전송 완료")

test_telegram()

# ── 최종 결과 ────────────────────────────────────────
print("\n" + "="*50)
print(f"E2E 테스트 결과: {results['passed']}/{results['passed']+results['failed']}")
for t in results["tests"]:
    print(f"  {t}")
print("="*50)
