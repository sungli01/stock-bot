"""
매매 시간 유틸리티
- KST 18:00 ~ 익일 06:00 = 매매 가능 시간 (프리마켓+정규장+애프터마켓)
- US 정규장: ET 9:30~16:00
- 장 마감 = KST 06:00 (강제청산 기준)
"""
from datetime import datetime, time as dtime
import pytz

ET = pytz.timezone("America/New_York")
KST = pytz.timezone("Asia/Seoul")
UTC = pytz.utc

# KST 기준 매매 윈도우
TRADING_START_KST = dtime(18, 0)   # 18:00 KST
TRADING_END_KST = dtime(6, 0)     # 06:00 KST (익일)
PREMARKET_PREP_KST = dtime(17, 50) # 17:50 KST — 3분봉 사전 축적 시작

# US 정규장 (참고용)
US_MARKET_OPEN = dtime(9, 30)
US_MARKET_CLOSE = dtime(16, 0)


def now_kst() -> datetime:
    return datetime.now(KST)


def now_et() -> datetime:
    return datetime.now(ET)


def is_premarket_prep() -> bool:
    """
    KST 17:50 ~ 18:00 — 3분봉 데이터 사전 축적 구간.
    bar_scanner가 이 시간부터 동작하여 큐를 준비.
    """
    now = now_kst()
    if now.weekday() >= 5:
        return False
    return dtime(17, 50) <= now.time() < dtime(18, 0)


def is_scan_active() -> bool:
    """bar_scanner 동작 가능 여부 — 17:50부터 06:00까지"""
    now = now_kst()
    hour = now.hour
    minute = now.minute
    in_window = (hour == 17 and minute >= 50) or hour >= 18 or hour < 6
    if not in_window:
        return False
    now_eastern = now.astimezone(ET)
    if now_eastern.weekday() >= 5:
        return False
    return True


def is_trading_window() -> bool:
    """
    KST 18:00 ~ 익일 06:00 매매 가능 여부.
    주말(토요일 18시~월요일 06시는 미국장 안 열림) 제외.
    """
    now = now_kst()
    hour = now.hour

    # KST 18:00~23:59 또는 00:00~05:59
    in_window = hour >= 18 or hour < 6

    if not in_window:
        return False

    # 주말 체크: ET 기준 토/일이면 장 안 열림
    now_eastern = now.astimezone(ET)
    if now_eastern.weekday() >= 5:  # 토(5), 일(6)
        return False

    return True


def is_us_market_open() -> bool:
    """US 정규장(ET 9:30~16:00) 열려있는지 확인."""
    now = now_et()
    if now.weekday() >= 5:
        return False
    return US_MARKET_OPEN <= now.time() < US_MARKET_CLOSE


def minutes_until_session_end() -> float:
    """
    KST 06:00 (매매 세션 종료)까지 남은 분.
    매매 윈도우 밖이면 -1 반환.
    """
    if not is_trading_window():
        return -1

    now = now_kst()
    hour = now.hour

    if hour >= 18:
        # 18:xx ~ 23:xx → 다음날 06:00까지
        remaining_today = (24 - hour - 1) * 60 + (60 - now.minute)
        remaining_tomorrow = 6 * 60
        return remaining_today + remaining_tomorrow
    else:
        # 00:xx ~ 05:xx → 06:00까지
        end = now.replace(hour=6, minute=0, second=0, microsecond=0)
        return (end - now).total_seconds() / 60


def get_all_timestamps() -> dict:
    """현재 시각을 UTC, ET, KST로 반환"""
    now_utc = datetime.now(UTC)
    return {
        "utc": now_utc.isoformat(),
        "et": now_utc.astimezone(ET).isoformat(),
        "kst": now_utc.astimezone(KST).isoformat(),
    }


def get_trading_date() -> str:
    """
    현재 매매일 반환 (YYYY-MM-DD).
    KST 18:00 이후면 당일, 00:00~06:00이면 전일이 매매일.
    """
    now = now_kst()
    if now.hour < 6:
        # 자정~06시 = 전날 세션
        from datetime import timedelta
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")
