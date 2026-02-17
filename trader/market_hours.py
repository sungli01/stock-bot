"""
US 정규장 시간 유틸리티
- ET 9:30~16:00 정규장 판별
- 장 마감 임박 판별
"""
from datetime import datetime, time as dtime
import pytz

ET = pytz.timezone("America/New_York")
KST = pytz.timezone("Asia/Seoul")
UTC = pytz.utc

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


def now_et() -> datetime:
    return datetime.now(ET)


def is_us_market_open() -> bool:
    """US 정규장(ET 9:30~16:00) 열려있는지 확인. 주말 제외."""
    now = now_et()
    if now.weekday() >= 5:  # 토(5), 일(6)
        return False
    return MARKET_OPEN <= now.time() < MARKET_CLOSE


def minutes_until_close() -> float:
    """장 마감까지 남은 분. 장 외 시간이면 -1 반환."""
    if not is_us_market_open():
        return -1
    now = now_et()
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return (close_dt - now).total_seconds() / 60


def get_all_timestamps() -> dict:
    """현재 시각을 UTC, ET, KST로 반환"""
    now_utc = datetime.now(UTC)
    return {
        "utc": now_utc.isoformat(),
        "et": now_utc.astimezone(ET).isoformat(),
        "kst": now_utc.astimezone(KST).isoformat(),
    }
