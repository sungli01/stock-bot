"""
볼린저밴드 트레일링 스탑 모듈
- 15분봉(delayed) 기반 BB 계산
- BB 상단 이탈 → 최고점에서 -10% 트레일링
- BB 미이탈 → +35% 즉시 익절
- 기본 TP/SL(+30%/-15%)도 병행
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")


class BBTrailingStop:
    """볼린저밴드 기반 트레일링 스탑 관리"""

    def __init__(self, config: dict):
        trading_cfg = config.get("trading", {})
        self.stop_loss_pct = trading_cfg.get("stop_loss_pct", -15.0)
        self.take_profit_pct = trading_cfg.get("take_profit_pct", 30.0)
        self.trailing_drop_pct = trading_cfg.get("trailing_drop_pct", 10.0)
        self.force_close_before_min = trading_cfg.get("force_close_before_min", 15)

        # BB 이탈 시 트레일링: 최고점에서 -10%
        self.bb_trailing_drop = 10.0
        # BB 미이탈 시 즉시 익절 기준
        self.bb_no_breakout_tp = 35.0

        # 종목별 상태
        self._peak_prices: dict[str, float] = {}  # ticker → 최고가
        self._bb_breakout: dict[str, bool] = {}    # ticker → BB 상단 이탈 여부
        self._bb_cache: dict[str, dict] = {}       # ticker → {upper, mid, lower, updated_at}

    def fetch_15min_bars(self, ticker: str) -> Optional[pd.DataFrame]:
        """Polygon에서 15분봉 조회 (최근 2일)"""
        try:
            end = datetime.utcnow()
            start = end - timedelta(days=2)
            url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/15/minute/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
            resp = requests.get(url, params={
                "apiKey": POLYGON_API_KEY,
                "limit": 100,
                "sort": "asc",
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None

            df = pd.DataFrame(results)
            df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"})
            return df
        except Exception as e:
            logger.error(f"{ticker} 15분봉 조회 실패: {e}")
            return None

    def update_bb(self, ticker: str) -> Optional[dict]:
        """BB 값 업데이트 (캐시: 5분 간격)"""
        cached = self._bb_cache.get(ticker)
        if cached and (datetime.utcnow() - cached["updated_at"]).seconds < 300:
            return cached

        df = self.fetch_15min_bars(ticker)
        if df is None or len(df) < 20:
            return cached  # 이전 캐시 반환

        bbands = ta.bbands(df["close"], length=20, std=2)
        if bbands is None or bbands.empty:
            return cached

        cols = bbands.columns
        bb_data = {
            "lower": float(bbands[cols[0]].iloc[-1]),
            "mid": float(bbands[cols[1]].iloc[-1]),
            "upper": float(bbands[cols[2]].iloc[-1]),
            "updated_at": datetime.utcnow(),
        }
        self._bb_cache[ticker] = bb_data
        return bb_data

    def check_exit(self, ticker: str, current_price: float, avg_price: float) -> Optional[dict]:
        """
        종목의 현재가로 매도 조건 체크
        Returns: {"action": "SELL", "reason": "...", "pnl_pct": float} or None
        """
        if not current_price or not avg_price or avg_price <= 0:
            return None

        pnl_pct = ((current_price - avg_price) / avg_price) * 100

        # 1. 손절 체크 (최우선)
        if pnl_pct <= self.stop_loss_pct:
            self._cleanup(ticker)
            return {"action": "STOP", "reason": f"손절 {pnl_pct:.1f}%", "pnl_pct": pnl_pct}

        # 2. BB 기반 로직
        bb = self.update_bb(ticker)
        if bb:
            bb_upper = bb["upper"]

            # BB 상단 이탈 여부 체크
            if current_price > bb_upper:
                if not self._bb_breakout.get(ticker):
                    self._bb_breakout[ticker] = True
                    self._peak_prices[ticker] = current_price
                    logger.info(f"📊 {ticker} BB 상단 이탈! upper=${bb_upper:.2f} price=${current_price:.2f}")

            if self._bb_breakout.get(ticker):
                # BB 이탈 상태: 최고가 추적 + 트레일링
                peak = self._peak_prices.get(ticker, current_price)
                if current_price > peak:
                    self._peak_prices[ticker] = current_price
                    logger.debug(f"📈 {ticker} 최고가 갱신: ${current_price:.2f}")
                else:
                    drop_from_peak = ((peak - current_price) / peak) * 100
                    if drop_from_peak >= self.bb_trailing_drop:
                        self._cleanup(ticker)
                        return {
                            "action": "SELL",
                            "reason": f"BB 트레일링 (고점${peak:.2f} → -${drop_from_peak:.1f}%)",
                            "pnl_pct": pnl_pct,
                        }
            else:
                # BB 미이탈: +35% 도달 시 즉시 익절
                if pnl_pct >= self.bb_no_breakout_tp:
                    self._cleanup(ticker)
                    return {
                        "action": "SELL",
                        "reason": f"BB 미이탈 즉시익절 +{pnl_pct:.1f}%",
                        "pnl_pct": pnl_pct,
                    }

        # 3. 기본 TP (BB 데이터 없을 때 fallback)
        if not bb and pnl_pct >= self.take_profit_pct:
            self._cleanup(ticker)
            return {"action": "SELL", "reason": f"기본 익절 +{pnl_pct:.1f}%", "pnl_pct": pnl_pct}

        return None

    def _cleanup(self, ticker: str):
        """종목 상태 정리"""
        self._peak_prices.pop(ticker, None)
        self._bb_breakout.pop(ticker, None)
        self._bb_cache.pop(ticker, None)

    def reset(self):
        """세션 리셋"""
        self._peak_prices.clear()
        self._bb_breakout.clear()
        self._bb_cache.clear()
