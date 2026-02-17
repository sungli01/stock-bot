"""
피처 엔지니어링 모듈
- 시그널/마켓 데이터에서 ML 피처 추출
- 정규화, NaN 처리
"""
import numpy as np
from typing import Dict, List, Optional

FEATURE_NAMES = [
    "rsi",
    "macd_histogram",
    "ema_ratio",        # EMA5 / EMA20
    "bollinger_pos",    # (price - lower) / (upper - lower)
    "volume_ratio",     # volume / avg_volume_20
    "volatility",       # (high - low) / close
]


def extract_features(signal_data: Dict) -> Optional[np.ndarray]:
    """
    시그널/마켓 데이터 dict에서 피처 배열 추출.
    
    Args:
        signal_data: dict with keys matching FEATURE_NAMES or raw OHLCV+indicator data
        
    Returns:
        numpy array of shape (len(FEATURE_NAMES),) or None if insufficient data
    """
    try:
        features = []
        
        # RSI (0-100 → 0-1)
        rsi = signal_data.get("rsi", signal_data.get("RSI"))
        features.append(_normalize(rsi, 0, 100))
        
        # MACD Histogram (보통 -2 ~ +2 범위, tanh로 정규화)
        macd_hist = signal_data.get("macd_histogram", signal_data.get("MACD_histogram", signal_data.get("MACDh_12_26_9")))
        features.append(_tanh_normalize(macd_hist))
        
        # EMA5/EMA20 비율
        ema5 = signal_data.get("ema5", signal_data.get("EMA_5"))
        ema20 = signal_data.get("ema20", signal_data.get("EMA_20"))
        if ema5 is not None and ema20 is not None and ema20 != 0:
            features.append((ema5 / ema20) - 1.0)  # 0 중심
        else:
            features.append(0.0)
        
        # 볼린저 밴드 위치 (0-1)
        bb_upper = signal_data.get("bb_upper", signal_data.get("BBU_20_2.0"))
        bb_lower = signal_data.get("bb_lower", signal_data.get("BBL_20_2.0"))
        price = signal_data.get("close", signal_data.get("price"))
        if all(v is not None for v in [bb_upper, bb_lower, price]) and bb_upper != bb_lower:
            features.append((price - bb_lower) / (bb_upper - bb_lower))
        else:
            features.append(0.5)
        
        # 거래량 비율
        volume = signal_data.get("volume")
        avg_volume = signal_data.get("avg_volume_20", signal_data.get("volume_sma_20"))
        if volume is not None and avg_volume is not None and avg_volume > 0:
            features.append(min(volume / avg_volume, 5.0) / 5.0)  # cap at 5x, normalize
        else:
            features.append(0.2)
        
        # 변동률
        high = signal_data.get("high")
        low = signal_data.get("low")
        close = signal_data.get("close", signal_data.get("price"))
        if all(v is not None for v in [high, low, close]) and close > 0:
            features.append((high - low) / close)
        else:
            features.append(0.0)
        
        arr = np.array(features, dtype=np.float32)
        # NaN → 0
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
        return arr
        
    except Exception:
        return None


def _normalize(value, min_val, max_val):
    """Min-max 정규화"""
    if value is None:
        return 0.5
    return max(0.0, min(1.0, (float(value) - min_val) / (max_val - min_val)))


def _tanh_normalize(value, scale=1.0):
    """Tanh 정규화 (-1 ~ 1)"""
    if value is None:
        return 0.0
    return float(np.tanh(float(value) * scale))


def get_feature_names() -> List[str]:
    return FEATURE_NAMES.copy()
