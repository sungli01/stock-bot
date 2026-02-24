"""
Feature Engine for Penny Stock AI
- BB, RSI, VWAP, OFI 피처 생성
"""
import pandas as pd
import numpy as np


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def compute_bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    upper = mid + std * sigma
    lower = mid - std * sigma
    pct_b = (series - lower) / (upper - lower + 1e-9)
    return mid, upper, lower, pct_b


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """일중 VWAP (누적)"""
    tp = (df['high'] + df['low'] + df['close']) / 3
    cum_tp_vol = (tp * df['volume']).cumsum()
    cum_vol = df['volume'].cumsum()
    return cum_tp_vol / (cum_vol + 1e-9)


def compute_ofi(df: pd.DataFrame) -> pd.Series:
    """
    Order Flow Imbalance (OFI)
    bid_vol ≈ volume when close < open (매도 압력)
    ask_vol ≈ volume when close > open (매수 압력)
    OFI = (ask_vol - bid_vol) / (ask_vol + bid_vol)
    """
    ask_vol = df['volume'].where(df['close'] >= df['open'], 0)
    bid_vol = df['volume'].where(df['close'] < df['open'], 0)
    ofi = (ask_vol - bid_vol) / (ask_vol + bid_vol + 1e-9)
    return ofi.rolling(5).mean()


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    1분봉 OHLCV → 피처 DataFrame 생성
    """
    df = df.copy().sort_index()

    # 기본 수익률
    df['returns'] = df['close'].pct_change()
    df['log_returns'] = np.log(df['close'] / df['close'].shift(1))

    # RSI
    df['rsi'] = compute_rsi(df['close'], 14)

    # 볼린저 밴드
    df['bb_mid'], df['bb_upper'], df['bb_lower'], df['bb_pct'] = compute_bollinger_bands(df['close'])

    # VWAP
    df['vwap'] = compute_vwap(df)
    df['vwap_ratio'] = df['close'] / (df['vwap'] + 1e-9)

    # OFI (거래량 불균형)
    df['ofi'] = compute_ofi(df)

    # 거래량 관련
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    df['vol_ratio'] = df['volume'] / (df['vol_ma20'] + 1e-9)

    # 가격 모멘텀
    df['momentum_5'] = df['close'].pct_change(5)
    df['momentum_10'] = df['close'].pct_change(10)

    # 변동성
    df['volatility'] = df['returns'].rolling(10).std()

    # 결측값 제거
    df.dropna(inplace=True)

    return df


FEATURE_COLS = [
    'returns', 'log_returns',
    'rsi',
    'bb_pct',
    'vwap_ratio',
    'ofi',
    'vol_ratio',
    'momentum_5', 'momentum_10',
    'volatility',
]
