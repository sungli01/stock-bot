"""
utils/fx_rate.py
매시간 1회 USD/KRW 환율 자동 조회 및 캐싱
무료 API: https://open.er-api.com/v6/latest/USD
"""
import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

_cache = {
    "rate": float(os.getenv("USD_KRW_RATE", "1450.0")),
    "updated_at": 0.0
}

CACHE_TTL = 3600  # 1시간 (초)

def get_usd_krw() -> float:
    """
    USD/KRW 환율 반환.
    캐시가 1시간 이내면 캐시값 사용, 만료되면 API 재조회.
    실패 시 이전 캐시값 또는 환경변수 기본값 반환.
    """
    now = time.time()
    if now - _cache["updated_at"] < CACHE_TTL:
        return _cache["rate"]
    
    try:
        resp = requests.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=5
        )
        data = resp.json()
        if data.get("result") == "success":
            rate = float(data["rates"]["KRW"])
            _cache["rate"] = rate
            _cache["updated_at"] = now
            logger.info(f"[FX] USD/KRW 환율 갱신: {rate:,.1f}원")
            return rate
        else:
            logger.warning(f"[FX] API 응답 오류: {data}")
    except Exception as e:
        logger.warning(f"[FX] 환율 조회 실패 (캐시값 사용): {e}")
    
    return _cache["rate"]
