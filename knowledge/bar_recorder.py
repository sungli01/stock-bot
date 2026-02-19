"""
매매 종목 1분봉 데이터 기록 시스템
- 매수 시: 진입 전 30분 1분봉 즉시 수집
- 매도 시: 진입 후 봉 추가 저장
- Polygon.io API 사용
"""
import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger("bar_recorder")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bar_records")


class BarRecorder:
    def __init__(self):
        self.api_key = os.getenv("POLYGON_API_KEY", "")
        self._active_entries: dict[str, dict] = {}  # ticker -> entry record
        self._skip_count_today: int = 0
        self._skip_date: str = ""
        self._lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)

    def _fetch_bars(self, ticker: str, from_ts: str, to_ts: str) -> list[dict]:
        """Polygon.io에서 1분봉 조회. from_ts/to_ts: YYYY-MM-DD or ms timestamp."""
        if not self.api_key:
            logger.warning("POLYGON_API_KEY 미설정 — 1분봉 수집 불가")
            return []
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute"
            f"/{from_ts}/{to_ts}"
            f"?adjusted=true&sort=asc&limit=5000&apiKey={self.api_key}"
        )
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()
            results = data.get("results", [])
            return [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in results]
        except Exception as e:
            logger.error(f"1분봉 조회 실패 ({ticker}): {e}")
            return []

    def record_entry(self, ticker: str, entry_price: float, signal_info: dict):
        """매수 시 호출 — 진입 전 30분 1분봉을 백그라운드로 수집/저장."""
        now_utc = datetime.now(timezone.utc)
        entry_time = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
        date_str = now_utc.strftime("%Y-%m-%d")

        record = {
            "ticker": ticker,
            "date": date_str,
            "event": "BUY",
            "entry_price": entry_price,
            "entry_time": entry_time,
            "signal": signal_info,
            "bars_context": {"pre_entry_30min": [], "post_entry": []},
        }

        with self._lock:
            self._active_entries[ticker] = record

        # 백그라운드에서 진입 전 30분 봉 수집
        def _fetch():
            try:
                from_dt = now_utc - timedelta(minutes=35)
                from_ms = int(from_dt.timestamp() * 1000)
                to_ms = int(now_utc.timestamp() * 1000)
                bars = self._fetch_bars(ticker, str(from_ms), str(to_ms))
                # 진입 시각 전 봉만 (최대 30개)
                entry_ms = int(now_utc.timestamp() * 1000)
                pre_bars = [b for b in bars if b["t"] < entry_ms][-30:]
                with self._lock:
                    if ticker in self._active_entries:
                        self._active_entries[ticker]["bars_context"]["pre_entry_30min"] = pre_bars
                        self._save_record(self._active_entries[ticker])
            except Exception as e:
                logger.error(f"bar_recorder entry fetch 실패 ({ticker}): {e}")

        threading.Thread(target=_fetch, daemon=True).start()

    def record_exit(self, ticker: str, exit_price: float, exit_reason: str, pnl_pct: float):
        """매도 시 호출 — 진입 후 봉 추가 수집 후 최종 저장."""
        now_utc = datetime.now(timezone.utc)
        exit_time = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")

        with self._lock:
            record = self._active_entries.pop(ticker, None)

        if not record:
            # entry 기록 없이 exit만 온 경우 (수동 매수 등)
            record = {
                "ticker": ticker,
                "date": now_utc.strftime("%Y-%m-%d"),
                "event": "SELL",
                "entry_price": 0,
                "entry_time": "",
                "signal": {},
                "bars_context": {"pre_entry_30min": [], "post_entry": []},
            }

        record["event"] = "SELL"
        record["exit_price"] = exit_price
        record["exit_time"] = exit_time
        record["exit_reason"] = exit_reason
        record["pnl_pct"] = pnl_pct

        # 백그라운드에서 진입 후 봉 수집
        def _fetch():
            try:
                entry_time_str = record.get("entry_time", "")
                if entry_time_str:
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                    from_ms = int(entry_dt.timestamp() * 1000)
                else:
                    from_ms = int((now_utc - timedelta(minutes=60)).timestamp() * 1000)
                to_ms = int(now_utc.timestamp() * 1000)
                bars = self._fetch_bars(ticker, str(from_ms), str(to_ms))
                record["bars_context"]["post_entry"] = bars
                self._save_record(record)
            except Exception as e:
                logger.error(f"bar_recorder exit fetch 실패 ({ticker}): {e}")

        threading.Thread(target=_fetch, daemon=True).start()

    def record_candidate_skip(self, ticker: str, reason: str, signal_info: dict):
        """후보 탈락 종목 간단 기록 (분봉 없이). 일 최대 20건."""
        now_utc = datetime.now(timezone.utc)
        date_str = now_utc.strftime("%Y-%m-%d")

        with self._lock:
            if self._skip_date != date_str:
                self._skip_date = date_str
                self._skip_count_today = 0
            if self._skip_count_today >= 20:
                return
            self._skip_count_today += 1

        record = {
            "ticker": ticker,
            "date": date_str,
            "event": "CANDIDATE_SKIP",
            "reason": reason,
            "signal": signal_info,
            "timestamp": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        self._save_record(record)

    def _save_record(self, record: dict):
        """레코드를 JSON 파일로 저장."""
        ticker = record["ticker"]
        date_str = record.get("date", "unknown")
        ts = int(time.time())
        event = record.get("event", "UNKNOWN")
        filename = f"{ticker}_{date_str}_{event}_{ts}.json"
        filepath = os.path.join(DATA_DIR, filename)
        try:
            with open(filepath, "w") as f:
                json.dump(record, f, indent=2)
        except Exception as e:
            logger.error(f"bar_record 저장 실패: {e}")

    def reset_session(self):
        """세션 리셋 시 호출."""
        with self._lock:
            self._active_entries.clear()
            self._skip_count_today = 0
