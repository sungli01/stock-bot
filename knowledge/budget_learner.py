"""
knowledge/budget_learner.py — 실전 데이터 기반 매수 한도 학습기

동작 원리:
  1. 매 거래마다 (종목, 가격, 3분봉거래량, 진입시도액, 실제체결액, 슬리피지) 기록
  2. 종목별 + 가격대/거래량 구간별 통계 누적
  3. 다음 진입 시 학습된 한도 추천
  4. 2차/3차는 모멘텀 확인됐으므로 1차 대비 배수 확대 (기본 1.5x / 2.0x)

저장 경로: data/budget_knowledge.json
"""
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean, median

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
BUDGET_FILE = DATA_DIR / "budget_knowledge.json"

# 가격 구간 정의
PRICE_BUCKETS = [
    (0.7,  2.0,  "tier1"),   # 소형 저가
    (2.0,  5.0,  "tier2"),   # 소형 중가
    (5.0, 10.0,  "tier3"),   # 중형 저가
    (10.0, 30.0, "tier4"),   # 중형 고가
]

# 거래량(3분봉) 구간 정의 (주)
VOL_BUCKETS = [
    (0,      50_000,  "low"),
    (50_000, 200_000, "mid"),
    (200_000, 1_000_000, "high"),
    (1_000_000, 999_999_999, "ultra"),
]

# 2차/3차 예산 배수
ENTRY_MULTIPLIER = {
    "1차": 1.0,
    "2차": 1.5,   # 모멘텀 확인 → 50% 증액
    "3차": 2.0,   # 강한 모멘텀 → 2배
}

# 초기 기본값 (데이터 없을 때 fallback)
DEFAULT_BUDGET_KRW = {
    "tier1": 200_000,   # $0.7~$2: ₩20만
    "tier2": 300_000,   # $2~$5: ₩30만
    "tier3": 500_000,   # $5~$10: ₩50만
    "tier4": 700_000,   # $10~$30: ₩70만
}


def _price_tier(price: float) -> str:
    for lo, hi, label in PRICE_BUCKETS:
        if lo <= price < hi:
            return label
    return "tier4"


def _vol_tier(vol_3min: float) -> str:
    for lo, hi, label in VOL_BUCKETS:
        if lo <= vol_3min < hi:
            return label
    return "ultra"


class BudgetLearner:
    """실전 매매 데이터 기반 매수 한도 학습기"""

    def __init__(self):
        self._data = self._load()
        logger.info(f"💰 BudgetLearner 로드: {len(self._data['tickers'])}개 종목 데이터")

    def _load(self) -> dict:
        if BUDGET_FILE.exists():
            try:
                with open(BUDGET_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"budget_knowledge.json 로드 실패: {e}")
        return {
            "version": "1.0",
            "updated": "",
            "tickers": {},          # ticker → 종목별 통계
            "categories": {},       # "tier1_low" → 카테고리별 통계
            "total_trades": 0,
        }

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data["updated"] = datetime.now(timezone.utc).isoformat()
        with open(BUDGET_FILE, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def record_trade(
        self,
        ticker: str,
        price: float,
        vol_3min: float,           # 3분봉 거래량 (주)
        intended_krw: int,         # 진입 시도 금액 (₩)
        filled_krw: int,           # 실제 체결 금액 (₩, paper=intended, real=실측)
        slippage_pct: float,       # 슬리피지 (%, 양수=불리)
        entry_type: str,           # "1차" / "2차" / "3차"
        date_str: str = "",
    ):
        """매매 완료 후 호출 — 데이터 기록"""
        tier = _price_tier(price)
        vtier = _vol_tier(vol_3min)
        cat_key = f"{tier}_{vtier}"
        now_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        record = {
            "date": now_str,
            "entry_type": entry_type,
            "price": round(price, 2),
            "vol_3min": int(vol_3min),
            "intended_krw": int(intended_krw),
            "filled_krw": int(filled_krw),
            "fill_rate": round(filled_krw / max(intended_krw, 1), 3),
            "slippage_pct": round(slippage_pct, 2),
        }

        # ── 종목별 기록 ──
        if ticker not in self._data["tickers"]:
            self._data["tickers"][ticker] = {
                "price_tier": tier,
                "trades": [],
                "stats": {},
            }
        self._data["tickers"][ticker]["trades"].append(record)
        # 최신 50건만 유지
        self._data["tickers"][ticker]["trades"] = \
            self._data["tickers"][ticker]["trades"][-50:]
        self._update_ticker_stats(ticker)

        # ── 카테고리별 기록 ──
        if cat_key not in self._data["categories"]:
            self._data["categories"][cat_key] = {"trades": [], "stats": {}}
        self._data["categories"][cat_key]["trades"].append({
            **record, "ticker": ticker
        })
        # 최신 200건만 유지
        self._data["categories"][cat_key]["trades"] = \
            self._data["categories"][cat_key]["trades"][-200:]
        self._update_category_stats(cat_key)

        self._data["total_trades"] += 1
        self.save()

        logger.info(
            f"💾 [BudgetLearner] {entry_type} {ticker} 기록: "
            f"시도 ₩{intended_krw:,.0f} / 체결 ₩{filled_krw:,.0f} "
            f"(fill {filled_krw/max(intended_krw,1)*100:.0f}%, slip {slippage_pct:+.2f}%)"
        )

    def _update_ticker_stats(self, ticker: str):
        trades = self._data["tickers"][ticker]["trades"]
        if not trades:
            return
        # 1차 기준으로 통계
        first_trades = [t for t in trades if t["entry_type"] == "1차"]
        all_filled = [t["filled_krw"] for t in trades]
        self._data["tickers"][ticker]["stats"] = {
            "trade_count": len(trades),
            "avg_filled_1st": int(mean(t["filled_krw"] for t in first_trades)) if first_trades else 0,
            "median_filled_1st": int(median(t["filled_krw"] for t in first_trades)) if first_trades else 0,
            "max_filled": max(all_filled),
            "avg_fill_rate": round(mean(t["fill_rate"] for t in trades), 3),
            "avg_slippage_pct": round(mean(t["slippage_pct"] for t in trades), 2),
        }

    def _update_category_stats(self, cat_key: str):
        trades = self._data["categories"][cat_key]["trades"]
        if not trades:
            return
        first_trades = [t for t in trades if t["entry_type"] == "1차"]
        self._data["categories"][cat_key]["stats"] = {
            "trade_count": len(trades),
            "avg_filled_1st": int(mean(t["filled_krw"] for t in first_trades)) if first_trades else 0,
            "median_filled_1st": int(median(t["filled_krw"] for t in first_trades)) if first_trades else 0,
            "avg_fill_rate": round(mean(t["fill_rate"] for t in trades), 3),
            "avg_slippage_pct": round(mean(t["slippage_pct"] for t in trades), 2),
            "p25_filled": int(sorted(t["filled_krw"] for t in trades)[len(trades)//4]) if len(trades) >= 4 else 0,
            "p75_filled": int(sorted(t["filled_krw"] for t in trades)[len(trades)*3//4]) if len(trades) >= 4 else 0,
        }

    def get_budget(
        self,
        ticker: str,
        price: float,
        vol_3min: float,
        entry_type: str = "1차",
        current_cash_krw: int = 0,
    ) -> int:
        """
        학습된 데이터 기반 추천 매수 한도 반환 (₩)

        우선순위:
          1. 종목별 통계 (5건+ 데이터 있을 때)
          2. 카테고리별 통계 (10건+ 데이터 있을 때)
          3. 기본값 (DEFAULT_BUDGET_KRW)

        2차/3차는 1차 대비 배수 적용
        """
        tier = _price_tier(price)
        vtier = _vol_tier(vol_3min)
        cat_key = f"{tier}_{vtier}"
        multiplier = ENTRY_MULTIPLIER.get(entry_type, 1.0)

        # 1. 종목별 데이터
        ticker_data = self._data["tickers"].get(ticker, {})
        ticker_stats = ticker_data.get("stats", {})
        ticker_count = ticker_stats.get("trade_count", 0)

        if ticker_count >= 5:
            base = ticker_stats.get("median_filled_1st") or ticker_stats.get("avg_filled_1st", 0)
            if base > 0:
                budget = int(base * multiplier)
                logger.info(
                    f"💰 {ticker} 예산 추천 [{entry_type}]: ₩{budget:,.0f} "
                    f"(종목데이터 {ticker_count}건 × {multiplier}x)"
                )
                return min(budget, current_cash_krw) if current_cash_krw > 0 else budget

        # 2. 카테고리 데이터
        cat_data = self._data["categories"].get(cat_key, {})
        cat_stats = cat_data.get("stats", {})
        cat_count = cat_stats.get("trade_count", 0)

        if cat_count >= 10:
            base = cat_stats.get("median_filled_1st") or cat_stats.get("avg_filled_1st", 0)
            if base > 0:
                budget = int(base * multiplier)
                logger.info(
                    f"💰 {ticker} 예산 추천 [{entry_type}]: ₩{budget:,.0f} "
                    f"(카테고리 {cat_key} {cat_count}건 × {multiplier}x)"
                )
                return min(budget, current_cash_krw) if current_cash_krw > 0 else budget

        # 3. 기본값
        base = DEFAULT_BUDGET_KRW.get(tier, 300_000)
        budget = int(base * multiplier)
        logger.info(
            f"💰 {ticker} 예산 추천 [{entry_type}]: ₩{budget:,.0f} "
            f"(기본값 {tier} × {multiplier}x, 데이터 없음)"
        )
        return min(budget, current_cash_krw) if current_cash_krw > 0 else budget

    def get_summary(self) -> dict:
        """현재 학습 현황 요약"""
        cats = {}
        for key, val in self._data["categories"].items():
            stats = val.get("stats", {})
            if stats:
                cats[key] = {
                    "count": stats.get("trade_count", 0),
                    "avg_1st": stats.get("avg_filled_1st", 0),
                    "slip": stats.get("avg_slippage_pct", 0),
                }
        return {
            "total_trades": self._data["total_trades"],
            "ticker_count": len(self._data["tickers"]),
            "categories": cats,
        }
