"""
파일 기반 저장소 — PostgreSQL fallback
- JSON 파일로 매매 기록, 포지션, 시그널 저장
- DB 연결 성공 시 DB 사용, 실패 시 파일 fallback
"""
import json
import os
import logging
from datetime import datetime
from typing import Optional
import pytz

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load(filename: str) -> list:
    _ensure_dir()
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save(filename: str, data: list):
    _ensure_dir()
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _add_timestamps(record: dict):
    """UTC, ET, KST 타임스탬프를 레코드에 추가"""
    utc_now = datetime.now(pytz.utc)
    record["timestamp_utc"] = utc_now.isoformat()
    record["timestamp_et"] = utc_now.astimezone(pytz.timezone("America/New_York")).isoformat()
    record["timestamp_kst"] = utc_now.astimezone(pytz.timezone("Asia/Seoul")).isoformat()
    record.setdefault("timestamp", utc_now.isoformat())


class FileStore:
    """JSON 파일 기반 저장소"""

    def save_trade(self, trade: dict):
        _add_timestamps(trade)
        trades = _load("trades.json")
        trades.append(trade)
        _save("trades.json", trades)
        logger.debug(f"Trade saved: {trade.get('ticker')}")

    def get_trades(self, ticker: Optional[str] = None) -> list:
        trades = _load("trades.json")
        if ticker:
            return [t for t in trades if t.get("ticker") == ticker]
        return trades

    def save_position(self, position: dict):
        position.setdefault("updated_at", datetime.now().isoformat())
        positions = _load("positions.json")
        # upsert by ticker
        positions = [p for p in positions if p.get("ticker") != position.get("ticker")]
        positions.append(position)
        _save("positions.json", positions)

    def remove_position(self, ticker: str):
        positions = _load("positions.json")
        positions = [p for p in positions if p.get("ticker") != ticker]
        _save("positions.json", positions)

    def get_positions(self) -> list:
        return _load("positions.json")

    def save_signal(self, signal: dict):
        _add_timestamps(signal)
        signals = _load("signals.json")
        signals.append(signal)
        # keep last 1000
        if len(signals) > 1000:
            signals = signals[-1000:]
        _save("signals.json", signals)

    def get_signals(self, ticker: Optional[str] = None) -> list:
        signals = _load("signals.json")
        if ticker:
            return [s for s in signals if s.get("ticker") == ticker]
        return signals
