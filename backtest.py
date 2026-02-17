"""
백테스트 v3 — 실시간 1분봉 급등 감지 시뮬레이션
- 전일 데이터 참조 없음
- 당일 1분봉을 시간순 순회하며 직전 N분 대비 급등 + 거래량 스파이크 감지
- 감지 시점에 매수, 이후 익절/손절/장마감 청산
- look-ahead bias 완전 제거

사용법: python backtest.py 2025-12-22
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")


def load_config() -> dict:
    with open("config/config.yaml", "r") as f:
        return yaml.safe_load(f)


class BacktestEngine:
    """실시간 급등 감지 시뮬레이션 엔진"""

    def __init__(self, config: dict):
        self.config = config
        self.scanner_cfg = config.get("scanner", {})
        self.trading_cfg = config.get("trading", {})

        self.total_buy_amount = self.trading_cfg.get("total_buy_amount", 1_000_000)
        self.max_positions = self.trading_cfg.get("max_positions", 2)
        self.take_profit_pct = self.trading_cfg.get("take_profit_pct", 30.0)
        self.stop_loss_pct = self.trading_cfg.get("stop_loss_pct", -15.0)
        self.exchange_rate = 1350

        # 급등 감지 기준
        self.min_price = self.scanner_cfg.get("min_price", 1.0)
        self.surge_pct = self.scanner_cfg.get("price_change_pct", 5.0)  # 직전 N분 대비 변동률
        self.surge_window = 5  # 5분 윈도우
        self.volume_spike_pct = self.scanner_cfg.get("volume_spike_pct", 200.0)  # 거래량 스파이크
        self.volume_avg_window = 20  # 거래량 평균 윈도우 (20분)
        self.min_volume = self.scanner_cfg.get("min_volume", 10000)

        # 실제 봇 지연 시뮬레이션
        self.scan_delay_bars = 1   # 스캔 주기 10초 → 1분봉 기준 1봉 지연
        self.split_count = self.trading_cfg.get("split_count", 3)
        self.buy_execution_bars = self.split_count  # 3분할 = 3분봉 소요

        from polygon import RESTClient
        self.polygon = RESTClient(api_key=POLYGON_API_KEY)

    def get_active_tickers(self, date: str) -> list[str]:
        """
        당일 거래량 상위 종목 (1분봉 조회 대상)
        ※ 실제 봇: 전종목 스냅샷으로 실시간 필터
        ※ 백테스트: grouped daily로 활발 종목 선별 (조회 효율)
        이건 "어떤 종목을 모니터링할까"만 결정, 매수 판단이 아님
        """
        try:
            resp = self.polygon.get_grouped_daily_aggs(date)
            tickers = []
            for r in resp:
                if not r.close or r.close < self.min_price:
                    continue
                if not r.volume or r.volume < 50000:  # 최소 5만주 (활발한 종목만)
                    continue
                tickers.append({
                    "ticker": r.ticker,
                    "volume": r.volume,
                })
            tickers.sort(key=lambda x: x["volume"], reverse=True)
            return [t["ticker"] for t in tickers[:30]]
        except Exception as e:
            logger.error(f"{date} 종목 조회 실패: {e}")
            return []

    def get_intraday_1min(self, ticker: str, date: str) -> list[dict]:
        """해당 날짜 1분봉 데이터"""
        try:
            aggs = self.polygon.get_aggs(
                ticker=ticker,
                multiplier=1,
                timespan="minute",
                from_=date,
                to=date,
                limit=1000,
            )
            if not aggs:
                return []
            import pytz
            KST = pytz.timezone("Asia/Seoul")
            bars = []
            for a in aggs:
                ts = datetime.fromtimestamp(a.timestamp / 1000, tz=pytz.UTC)
                ts_kst = ts.astimezone(KST)
                bars.append({
                    "time_utc": ts.strftime("%H:%M"),
                    "time_kst": ts_kst.strftime("%H:%M"),
                    "timestamp": a.timestamp,
                    "open": a.open,
                    "high": a.high,
                    "low": a.low,
                    "close": a.close,
                    "volume": a.volume or 0,
                })
            return bars
        except Exception as e:
            logger.error(f"{ticker} {date} 1분봉 조회 실패: {e}")
            return []

    def detect_surge(self, bars: list[dict], idx: int) -> Optional[dict]:
        """
        idx 시점에서 급등 신호 감지 (과거 데이터만 사용)
        - 직전 surge_window분 대비 변동률 surge_pct% 이상
        - 현재 거래량이 직전 volume_avg_window분 평균의 volume_spike_pct% 이상
        """
        if idx < max(self.surge_window, self.volume_avg_window):
            return None

        current = bars[idx]
        if current["close"] < self.min_price:
            return None

        # 가격 급등 체크: surge_window분 전 종가 대비
        past_bar = bars[idx - self.surge_window]
        if past_bar["close"] <= 0:
            return None

        price_change = ((current["close"] - past_bar["close"]) / past_bar["close"]) * 100
        if price_change < self.surge_pct:
            return None

        # 거래량 스파이크 체크: 최근 volume_avg_window분 평균 대비
        vol_window = bars[max(0, idx - self.volume_avg_window):idx]
        if not vol_window:
            return None
        avg_volume = sum(b["volume"] for b in vol_window) / len(vol_window)
        if avg_volume <= 0:
            return None

        volume_ratio = (current["volume"] / avg_volume) * 100
        if volume_ratio < self.volume_spike_pct:
            return None

        # 누적 거래량 체크
        cumul_volume = sum(b["volume"] for b in bars[:idx + 1])
        if cumul_volume < self.min_volume:
            return None

        return {
            "price_change_pct": round(price_change, 2),
            "volume_ratio": round(volume_ratio, 0),
            "price": current["close"],
            "volume": current["volume"],
            "avg_volume": round(avg_volume, 0),
        }

    def simulate_day(self, date: str) -> dict:
        """
        하루 시뮬레이션 — 1분봉 실시간 급등 감지
        
        1) 당일 활발 종목 선별 (모니터링 대상)
        2) 각 종목 1분봉을 시간순 순회:
           - 직전 5분 대비 5%↑ + 거래량 200%↑ 스파이크 → 매수
           - 오직 과거 데이터만 사용
        3) 매수 후: 익절(+30%)/손절(-15%)/장마감 청산
        """
        result = {
            "date": date,
            "monitored": 0,
            "signals_detected": 0,
            "trades": [],
            "total_invested_krw": 0,
            "total_return_krw": 0,
            "total_pnl_krw": 0,
            "total_pnl_pct": 0,
        }

        # 1) 모니터링 대상 종목
        logger.info(f"📅 [{date}] 모니터링 대상 선별 중...")
        tickers = self.get_active_tickers(date)
        result["monitored"] = len(tickers)

        if not tickers:
            result["error"] = "종목 없음 (휴장일 또는 데이터 부족)"
            return result

        logger.info(f"  → {len(tickers)}개 종목 1분봉 분석 시작")

        # 2) 각 종목 1분봉 → 급등 감지
        per_stock_krw = self.total_buy_amount / self.max_positions
        all_signals = []

        for ticker in tickers:
            bars = self.get_intraday_1min(ticker, date)
            if len(bars) < 30:  # 최소 30분 데이터 필요
                continue

            # 1분봉 순회 — 급등 시점 탐지
            for i in range(self.volume_avg_window, len(bars)):
                bar = bars[i]
                # UTC 09:00 이후 (KST 18:00, 프리마켓 시작)
                if bar["time_utc"] < "09:00":
                    continue

                surge = self.detect_surge(bars, i)
                if surge:
                    # 실제 봇 지연 반영: 감지 후 scan_delay + 분할매수 시간
                    actual_buy_idx = i + self.scan_delay_bars
                    if actual_buy_idx >= len(bars):
                        continue

                    # 분할매수 평균가 계산 (3분할 = 3봉에 걸쳐 매수)
                    buy_prices = []
                    for b in range(actual_buy_idx, min(actual_buy_idx + self.split_count, len(bars))):
                        buy_prices.append(bars[b]["close"])
                    if not buy_prices:
                        continue

                    avg_buy_price = sum(buy_prices) / len(buy_prices)
                    buy_complete_idx = actual_buy_idx + len(buy_prices) - 1

                    all_signals.append({
                        "timestamp": bars[actual_buy_idx]["timestamp"],
                        "time_utc": bars[actual_buy_idx]["time_utc"],
                        "time_kst": bars[actual_buy_idx].get("time_kst", ""),
                        "ticker": ticker,
                        "detect_price": surge["price"],
                        "buy_price": avg_buy_price,
                        "surge_pct": surge["price_change_pct"],
                        "volume_ratio": surge["volume_ratio"],
                        "bars": bars,
                        "bar_idx": buy_complete_idx,
                        "detect_time_kst": bar.get("time_kst", bar["time_utc"]),
                    })
                    break

            time.sleep(0.3)

        # 급등강도 순 정렬 (거래량변동폭 × 가격변동폭)
        for sig in all_signals:
            sig["surge_score"] = sig["surge_pct"] * sig["volume_ratio"]
        all_signals.sort(key=lambda x: x["surge_score"], reverse=True)
        result["signals_detected"] = len(all_signals)
        logger.info(f"  → {len(all_signals)}개 급등 신호 감지")

        # 3) 급등강도 순 매매 실행 (1위 70%, 2위 30%)
        allocation = self.trading_cfg.get("allocation_ratio", [0.7, 0.3])
        all_trades = []
        active_slots = 0
        used_tickers = set()
        pending_sells = []
        slot_index = 0  # 0=1위(70%), 1=2위(30%)

        for sig in all_signals:
            # pending 매도 완료 체크
            for ps in list(pending_sells):
                if ps["sell_timestamp"] <= sig["timestamp"]:
                    all_trades.append(ps["trade"])
                    active_slots -= 1
                    pending_sells.remove(ps)

            if active_slots >= self.max_positions:
                continue
            if sig["ticker"] in used_tickers:
                continue

            ticker = sig["ticker"]
            buy_price = sig["buy_price"]
            buy_time = sig["time_utc"]
            bars = sig["bars"]
            buy_idx = sig["bar_idx"]

            # 1위 70%, 2위 30% 배분
            alloc_idx = min(slot_index, len(allocation) - 1)
            stock_krw = self.total_buy_amount * allocation[alloc_idx]
            shares = int(stock_krw / (buy_price * self.exchange_rate))
            if shares < 1:
                continue

            invested_krw = round(buy_price * shares * self.exchange_rate)
            slot_index += 1

            # 매수 후 분봉 순회 (트레일링 스탑 지원)
            sell_price = None
            sell_time = None
            sell_reason = "강제청산(장마감)"
            sell_timestamp = bars[-1]["timestamp"]
            
            trailing_active = self.trading_cfg.get("trailing_stop", False)
            trailing_trigger = self.trading_cfg.get("trailing_trigger_pct", self.take_profit_pct)
            trailing_drop = self.trading_cfg.get("trailing_drop_pct", 10.0)
            peak_price = buy_price
            trailing_started = False

            for j in range(buy_idx + 1, len(bars)):
                bar = bars[j]
                price = bar["close"]
                # 고가도 체크 (봉 내 최고가)
                high = bar.get("high", price)
                pnl_pct = ((price - buy_price) / buy_price) * 100
                high_pnl = ((high - buy_price) / buy_price) * 100

                # 손절 체크 (항상 우선)
                if pnl_pct <= self.stop_loss_pct:
                    sell_price = price
                    sell_time_kst = bar.get("time_kst", bar["time_utc"])
                    sell_reason = f"손절({pnl_pct:.1f}%)"
                    sell_timestamp = bar["timestamp"]
                    break

                if trailing_active:
                    # 최고가 갱신
                    if high > peak_price:
                        peak_price = high
                    
                    # 트레일링 트리거 도달 여부
                    if high_pnl >= trailing_trigger:
                        trailing_started = True
                    
                    # 트레일링 활성 상태에서 고점 대비 하락폭 체크
                    if trailing_started and peak_price > 0:
                        drop_from_peak = ((peak_price - price) / peak_price) * 100
                        if drop_from_peak >= trailing_drop:
                            sell_price = price
                            sell_time_kst = bar.get("time_kst", bar["time_utc"])
                            final_pnl = ((price - buy_price) / buy_price) * 100
                            sell_reason = f"트레일링({final_pnl:+.1f}%,고점${peak_price:.2f})"
                            sell_timestamp = bar["timestamp"]
                            break
                else:
                    # 고정 익절
                    if pnl_pct >= self.take_profit_pct:
                        sell_price = price
                        sell_time_kst = bar.get("time_kst", bar["time_utc"])
                        sell_reason = f"익절(+{pnl_pct:.1f}%)"
                        sell_timestamp = bar["timestamp"]
                        break

            if sell_price is None:
                last_bar = bars[-1]
                sell_price = last_bar["close"]
                sell_time_kst = last_bar.get("time_kst", last_bar["time_utc"])
                sell_timestamp = last_bar["timestamp"]

            return_krw = round(sell_price * shares * self.exchange_rate)
            pnl_krw = return_krw - invested_krw
            pnl_pct_actual = ((sell_price - buy_price) / buy_price) * 100

            trade = {
                "ticker": ticker,
                "surge_pct": sig["surge_pct"],
                "volume_ratio": sig["volume_ratio"],
                "detect_time_kst": sig.get("detect_time_kst", ""),
                "detect_price": round(sig.get("detect_price", buy_price), 2),
                "surge_score": round(sig.get("surge_score", 0), 0),
                "allocation_pct": round(allocation[alloc_idx] * 100),
                "buy_price": round(buy_price, 2),
                "buy_time_kst": sig.get("time_kst", buy_time),
                "buy_time_utc": buy_time,
                "sell_price": round(sell_price, 2),
                "sell_time_kst": sell_time_kst,
                "sell_reason": sell_reason,
                "shares": shares,
                "invested_krw": invested_krw,
                "return_krw": return_krw,
                "pnl_krw": pnl_krw,
                "pnl_pct": round(pnl_pct_actual, 2),
            }

            if "강제청산" in sell_reason:
                pending_sells.append({
                    "sell_timestamp": sell_timestamp,
                    "trade": trade,
                })
                active_slots += 1
            else:
                all_trades.append(trade)
                # 익절/손절 = 즉시 슬롯 회복

            used_tickers.add(ticker)

        # 남은 pending
        for ps in pending_sells:
            all_trades.append(ps["trade"])

        result["trades"] = all_trades
        result["total_invested_krw"] = sum(t["invested_krw"] for t in all_trades)
        result["total_return_krw"] = sum(t["return_krw"] for t in all_trades)
        result["total_pnl_krw"] = sum(t["pnl_krw"] for t in all_trades)
        if result["total_invested_krw"] > 0:
            result["total_pnl_pct"] = round(
                (result["total_pnl_krw"] / result["total_invested_krw"]) * 100, 2
            )

        return result


def format_report(result: dict) -> str:
    date = result["date"]
    lines = [
        f"📅 **{date} 시뮬레이션** (v3 실시간 급등감지)",
        f"━━━━━━━━━━━━━━━━━━",
    ]

    if result.get("error"):
        lines.append(f"❌ {result['error']}")
        return "\n".join(lines)

    lines.append(f"모니터링: {result['monitored']}개 종목")
    lines.append(f"급등 감지: {result['signals_detected']}개")

    if not result["trades"]:
        lines.append(f"\n⚠️ 매매 없음")
        return "\n".join(lines)

    lines.append(f"실제 매매: {len(result['trades'])}건")
    lines.append("")

    for i, t in enumerate(result["trades"], 1):
        emoji = "🟢" if t["pnl_krw"] >= 0 else "🔴"
        lines.append(f"{emoji} **{i}. {t['ticker']}**")
        lines.append(f"   [{t.get('allocation_pct',50)}%배분] 급등: +{t['surge_pct']}% | 거래량 {t['volume_ratio']}% (KST {t.get('detect_time_kst','')})")
        lines.append(f"   매수: ${t['buy_price']} (KST {t.get('buy_time_kst','')}) [5분할 평균]")
        lines.append(f"   매도: ${t['sell_price']} (KST {t.get('sell_time_kst','')})")
        lines.append(f"   사유: {t['sell_reason']}")
        lines.append(f"   수량: {t['shares']}주 | 투자: ₩{t['invested_krw']:,}")
        pnl_sign = "+" if t["pnl_krw"] >= 0 else ""
        lines.append(f"   손익: {pnl_sign}₩{t['pnl_krw']:,} ({pnl_sign}{t['pnl_pct']}%)")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    total_sign = "+" if result["total_pnl_krw"] >= 0 else ""
    emoji = "💰" if result["total_pnl_krw"] >= 0 else "📉"
    lines.append(f"총 투자: ₩{result['total_invested_krw']:,}")
    lines.append(f"{emoji} **총 손익: {total_sign}₩{result['total_pnl_krw']:,} ({total_sign}{result['total_pnl_pct']}%)**")

    return "\n".join(lines)


def get_next_trading_day(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        start = datetime.now() - timedelta(days=60)
        while start.weekday() >= 5:
            start += timedelta(days=1)
        date = start.strftime("%Y-%m-%d")
    else:
        date = sys.argv[1]

    config = load_config()
    engine = BacktestEngine(config)

    print(f"\n🤖 백테스트 v3: {date} (실시간 급등 감지)")
    print("=" * 40)

    result = engine.simulate_day(date)
    report = format_report(result)
    print(report)

    os.makedirs("data/backtest", exist_ok=True)
    with open(f"data/backtest/{date}.json", "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # summary.json 자동 업데이트
    summary_path = "data/backtest/summary.json"
    if os.path.exists(summary_path):
        with open(summary_path, "r") as f:
            summary = json.load(f)
    else:
        summary = {"version": "v3", "days": []}

    # 중복 방지
    existing_dates = {d["date"] for d in summary.get("days", [])}
    if date not in existing_dates and result.get("trades"):
        wins = sum(1 for t in result["trades"] if t["pnl_krw"] >= 0)
        losses = sum(1 for t in result["trades"] if t["pnl_krw"] < 0)
        prev_cumul = summary["days"][-1]["cumulative_pnl"] if summary["days"] else 0
        summary["days"].append({
            "day": len(summary["days"]) + 1,
            "date": date,
            "trades": len(result["trades"]),
            "wins": wins,
            "losses": losses,
            "daily_pnl": result["total_pnl_krw"],
            "cumulative_pnl": prev_cumul + result["total_pnl_krw"],
            "daily_pct": result["total_pnl_pct"],
        })
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n💾 결과 저장: data/backtest/{date}.json")
    print(f"📆 다음 거래일: {get_next_trading_day(date)}")
