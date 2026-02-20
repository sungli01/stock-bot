#!/usr/bin/env python3
"""
백테스트 시뮬레이터 (에이전트 2)
v8.5 알고리즘으로 60일 데이터 트레이딩 시뮬레이션

분석 목표:
1. 현재 알고리즘으로 실제 데이터에서 매수시점을 명확히 실행할 수 있는가?
2. 페이크 데이터들로 엔진이 중요 포인트를 놓치는 것은 없는가?
3. 매도시점을 명확히 맞출 수 있는가?
4. 최종 승률과 결과는?

초기 자본: ₩1,000,000 (복리 모드)
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime

PROCESSED_DIR = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/processed")
RESULTS_DIR = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/results")
READY_FLAG = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/READY.flag")
SUMMARY_PATH = Path("/home/ubuntu/.openclaw/workspace/stock-bot/backtest_sim/summary.json")

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 알고리즘 파라미터 (v8.5 + 버그수정)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIG = {
    # 매수 조건
    "min_price": 0.70,
    "max_price": 30.0,
    "candidate_change_pct": 1.0,         # 버그#6: 5% → 1%
    "vol_3min_ratio_pct": 200.0,         # 3분봉 vol 200%+
    "min_daily_volume": 300000,          # $10 미만
    "min_daily_volume_highprice": 50000, # $10 이상
    "highprice_threshold": 10.0,
    "price_change_pct": 20.0,            # 큐 기준 +20%
    "max_pct_from_queue": 40.0,          # 버그#3: 상단 제한 40%
    "queue_expiry_min": 60,              # 버그#1: 큐 만료 60분

    # 매도 조건 (bb_trailing)
    "stop_loss_pct": -7.0,               # 하드 스탑
    "partial_sell_pct": 5.0,             # +5% 부분매도 (50%)
    "trailing_activate_pct": 8.0,        # +8% 트레일링 활성화
    "absolute_sell_pct": 10.0,           # +10% 전량매도
    "max_hold_minutes": 45,              # 최대 보유시간

    # 포트폴리오
    "initial_krw": 1_000_000,
    "total_buy_amount": 100_000,        # 회당 매수금액 (복리로 조정)
    "max_positions": 2,
    "allocation_ratio": [0.7, 0.3],
    "split_count": 10,

    # 환율 (시뮬 고정값)
    "usd_krw_rate": 1450.0,

    # 2차 진입 허용 (버그#1 수정 시 활성화)
    "allow_reentry": True,
    "reentry_cooldown_min": 30,
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# bb_trailing 매도 로직
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_trailing_width(pnl_pct, elapsed_min):
    """현재 수익률과 경과시간에 따른 트레일링 폭 계산"""
    if pnl_pct >= 80:
        width = 30.0
    elif pnl_pct >= 50:
        width = 8.0
    elif pnl_pct >= 15:
        width = 5.0
    else:
        width = 3.0

    # 30분 이상 보유 시 폭 20% 타이트하게
    if elapsed_min >= 30:
        width *= 0.8

    return width


class Position:
    def __init__(self, ticker, entry_price, shares, krw_invested, entry_time_ms, queue_entry_price):
        self.ticker = ticker
        self.entry_price = entry_price
        self.shares = shares           # 주수 (USD 기준)
        self.krw_invested = krw_invested
        self.entry_time_ms = entry_time_ms
        self.queue_entry_price = queue_entry_price

        self.peak_price = entry_price
        self.trailing_active = False
        self.trailing_stop = None
        self.partial_sold = False
        self.partial_ratio = 1.0      # 남은 비율 (부분매도 후 0.5)

        # 매도 추적
        self.sell_price = None
        self.sell_reason = None
        self.sell_time_ms = None
        self.pnl_pct = 0.0
        self.pnl_krw = 0.0

    def update_trailing(self, current_price, elapsed_min):
        """트레일링 스탑 업데이트. 반환: (should_sell, sell_reason)"""
        pnl_pct = (current_price / self.entry_price - 1) * 100

        # 고점 갱신
        if current_price > self.peak_price:
            self.peak_price = current_price

        # 하드 스탑
        if pnl_pct <= CONFIG["stop_loss_pct"]:
            return True, f"STOP_LOSS ({pnl_pct:.1f}%)"

        # 최대 보유시간
        if elapsed_min >= CONFIG["max_hold_minutes"]:
            return True, f"TIME_LIMIT ({pnl_pct:.1f}%)"

        # +10% 전량매도
        if pnl_pct >= CONFIG["absolute_sell_pct"] and not self.trailing_active:
            return True, f"PROFIT_TARGET +10% ({pnl_pct:.1f}%)"

        # 트레일링 활성화
        if pnl_pct >= CONFIG["trailing_activate_pct"]:
            self.trailing_active = True

        if self.trailing_active:
            peak_pnl = (self.peak_price / self.entry_price - 1) * 100
            width = get_trailing_width(peak_pnl, elapsed_min)
            self.trailing_stop = self.peak_price * (1 - width / 100)

            if current_price <= self.trailing_stop:
                return True, f"TRAILING ({pnl_pct:.1f}%, peak={peak_pnl:.1f}%)"

        return False, None

    def check_partial_sell(self, current_price):
        """부분매도 체크 (+5% 도달 시 50% 청산)"""
        if not self.partial_sold:
            pnl_pct = (current_price / self.entry_price - 1) * 100
            if pnl_pct >= CONFIG["partial_sell_pct"]:
                return True
        return False

    def close(self, sell_price, sell_time_ms, reason, ratio=1.0):
        self.sell_price = sell_price
        self.sell_time_ms = sell_time_ms
        self.sell_reason = reason
        self.pnl_pct = (sell_price / self.entry_price - 1) * 100
        sold_krw = self.krw_invested * ratio * (sell_price / self.entry_price)
        self.pnl_krw = (sold_krw - self.krw_invested * ratio)
        return self.pnl_krw


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 큐 관리
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MonitorQueue:
    def __init__(self):
        self.queue = {}  # ticker → {price, time_ms, vol_spike_time}

    def add(self, ticker, price, time_ms):
        self.queue[ticker] = {
            "price": price,
            "time_ms": time_ms,
        }

    def expire(self, current_time_ms):
        """버그#1 수정: 만료 처리를 항상 실행"""
        expiry_ms = CONFIG["queue_expiry_min"] * 60 * 1000
        expired = [t for t, v in self.queue.items()
                   if current_time_ms - v["time_ms"] > expiry_ms]
        for t in expired:
            del self.queue[t]
        return expired

    def check_buy_trigger(self, ticker, current_price, daily_volume, daily_open):
        """v8.5 매수 조건 3가지 체크"""
        if ticker not in self.queue:
            return False, None

        entry = self.queue[ticker]
        queue_price = entry["price"]

        # 조건 1: price +20%+ from queue
        price_change = (current_price / queue_price - 1) * 100
        if price_change < CONFIG["price_change_pct"]:
            return False, f"price_change {price_change:.1f}% < {CONFIG['price_change_pct']}%"

        # 조건 2 (버그#3): 너무 멀리 간 경우 제외
        if price_change > CONFIG["max_pct_from_queue"]:
            return False, f"price_change {price_change:.1f}% > max {CONFIG['max_pct_from_queue']}%"

        # 조건 3: daily volume 기준
        threshold = CONFIG["min_daily_volume_highprice"] if current_price >= CONFIG["highprice_threshold"] else CONFIG["min_daily_volume"]
        if daily_volume < threshold:
            return False, f"daily_vol {daily_volume} < {threshold}"

        return True, f"TRIGGER: queue={queue_price:.2f} cur={current_price:.2f} +{price_change:.1f}%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 하루치 시뮬레이션
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def simulate_day(date_data, portfolio_krw, traded_tickers_global):
    """단일 날짜 시뮬레이션
    반환: {trades, pnl_krw, pnl_pct, missed_entries, fake_signals, ending_portfolio}
    """
    date_str = date_data["date"]
    tickers_data = date_data.get("tickers", {})

    monitor_queue = MonitorQueue()
    open_positions = {}   # ticker → Position
    closed_trades = []
    traded_today = set()

    # 페이크 신호 추적
    fake_signals = []     # vol spike 했지만 이후 가격이 안 오른 것
    missed_entries = []   # 알고리즘이 놓친 실제 급등
    signals_generated = 0
    buys_executed = 0
    vol_spikes_detected = 0

    running_krw = portfolio_krw

    # ── 모든 종목을 시간 순으로 이벤트 정렬 ──
    all_events = []
    for ticker, tdata in tickers_data.items():
        if ticker in traded_tickers_global and not CONFIG["allow_reentry"]:
            continue
        for evt in tdata.get("events", []):
            all_events.append((evt["time_ms"], ticker, evt, tdata))

    all_events.sort(key=lambda x: x[0])

    # ── 시간순 이벤트 처리 ──
    for time_ms, ticker, evt, tdata in all_events:
        # 큐 만료 처리 (버그#1 수정: 항상 실행)
        monitor_queue.expire(time_ms)

        daily_volume = tdata["daily_volume"]
        daily_open = tdata["daily_open"]

        # ━━ 볼륨 스파이크 → 큐 추가 ━━
        if evt["is_vol_spike"] and evt["is_candidate"]:
            vol_spikes_detected += 1
            signals_generated += 1
            cur_price = evt["bar_close"]

            # 큐에 없으면 추가 (reentry 허용 시 cooldown 체크)
            if ticker not in monitor_queue.queue:
                monitor_queue.add(ticker, cur_price, time_ms)
            else:
                # 큐에 이미 있으면 갱신은 하지 않음 (원본 큐 가격 유지)
                pass

            # ── 페이크 신호 감지: 이후 최고가 기준 ──
            # (나중에 후처리에서 계산)

        # ━━ 큐에 있는 종목의 가격 상승 체크 → 매수 트리거 ━━
        if ticker in monitor_queue.queue and ticker not in open_positions:
            # 재진입 쿨다운 체크
            if ticker in traded_today and CONFIG["allow_reentry"]:
                pass  # 허용

            cur_price = evt["bar_close"]
            should_buy, reason = monitor_queue.check_buy_trigger(
                ticker, cur_price, daily_volume, daily_open
            )

            if should_buy:
                # 최대 포지션 체크
                if len(open_positions) >= CONFIG["max_positions"]:
                    pass
                else:
                    # 매수금액 계산 (포지션 수에 따라 배분)
                    pos_idx = len(open_positions)
                    alloc = CONFIG["allocation_ratio"][pos_idx] if pos_idx < len(CONFIG["allocation_ratio"]) else 0.3

                    buy_krw = min(
                        running_krw * alloc,
                        CONFIG["total_buy_amount"] * (running_krw / CONFIG["initial_krw"])
                    )
                    buy_usd = buy_krw / CONFIG["usd_krw_rate"]
                    shares = buy_usd / cur_price

                    pos = Position(
                        ticker=ticker,
                        entry_price=cur_price,
                        shares=shares,
                        krw_invested=buy_krw,
                        entry_time_ms=time_ms,
                        queue_entry_price=monitor_queue.queue[ticker]["price"],
                    )
                    open_positions[ticker] = pos
                    traded_today.add(ticker)
                    buys_executed += 1

                    del monitor_queue.queue[ticker]  # 큐에서 제거

        # ━━ 오픈 포지션 매도 체크 ━━
        if ticker in open_positions:
            pos = open_positions[ticker]
            cur_price = evt["bar_close"]
            elapsed_ms = time_ms - pos.entry_time_ms
            elapsed_min = elapsed_ms / 60000

            # 부분매도 체크
            if pos.check_partial_sell(cur_price) and not pos.partial_sold:
                pos.partial_sold = True
                pos.partial_ratio = 0.5
                partial_pnl = pos.close(cur_price, time_ms, "PARTIAL_SELL_50pct", ratio=0.5)
                running_krw += (pos.krw_invested * 0.5) * (cur_price / pos.entry_price)
                closed_trades.append({
                    "ticker": ticker,
                    "type": "partial",
                    "entry_price": pos.entry_price,
                    "sell_price": cur_price,
                    "pnl_pct": pos.pnl_pct,
                    "pnl_krw": partial_pnl,
                    "reason": "PARTIAL_SELL_50pct",
                    "hold_min": round(elapsed_min, 1),
                    "entry_time_ms": pos.entry_time_ms,
                    "sell_time_ms": time_ms,
                })
                # 잔여 50%만 보유
                pos.krw_invested *= 0.5

            # 전체 청산 체크
            should_sell, sell_reason = pos.update_trailing(cur_price, elapsed_min)
            if should_sell:
                pnl_krw = pos.close(cur_price, time_ms, sell_reason, ratio=1.0)
                running_krw += pos.krw_invested * (cur_price / pos.entry_price)

                closed_trades.append({
                    "ticker": ticker,
                    "type": "full",
                    "entry_price": pos.entry_price,
                    "sell_price": cur_price,
                    "pnl_pct": round(pos.pnl_pct, 2),
                    "pnl_krw": round(pnl_krw, 0),
                    "reason": sell_reason,
                    "hold_min": round(elapsed_min, 1),
                    "queue_price": pos.queue_entry_price,
                    "queue_to_entry_pct": round((pos.entry_price / pos.queue_entry_price - 1) * 100, 1),
                    "entry_time_ms": pos.entry_time_ms,
                    "sell_time_ms": time_ms,
                    "daily_high": tdata["daily_high"],
                    "max_possible_pct": round((tdata["daily_high"] / pos.entry_price - 1) * 100, 1),
                })
                del open_positions[ticker]

    # ━━ 장 종료 후 미청산 포지션 강제 청산 ━━
    for ticker, pos in list(open_positions.items()):
        tdata = tickers_data.get(ticker, {})
        final_price = tdata.get("daily_close", pos.entry_price)
        elapsed_min = (tdata.get("bars_1m", [{}])[-1].get("t", pos.entry_time_ms) - pos.entry_time_ms) / 60000
        pnl_krw = pos.close(final_price, 0, "FORCE_CLOSE_EOD", ratio=1.0)
        running_krw += pos.krw_invested * (final_price / pos.entry_price)
        closed_trades.append({
            "ticker": ticker,
            "type": "force_close",
            "entry_price": pos.entry_price,
            "sell_price": final_price,
            "pnl_pct": round(pos.pnl_pct, 2),
            "pnl_krw": round(pnl_krw, 0),
            "reason": "FORCE_CLOSE_EOD",
            "hold_min": round(elapsed_min, 1),
            "queue_price": pos.queue_entry_price,
        })

    # ━━ 페이크 신호 분석: 볼스파이크 후 최고가 기준 ━━
    for ticker, tdata in tickers_data.items():
        for evt in tdata.get("events", []):
            if not evt["is_vol_spike"]:
                continue
            spike_price = evt["bar_close"]
            spike_time = evt["time_ms"]

            # 이후 최고가 계산
            later_bars = [b for b in tdata.get("bars_3m", []) if b["t"] > spike_time]
            if later_bars:
                max_later_price = max(b.get("h", b.get("c", 0)) for b in later_bars)
                max_gain = (max_later_price / spike_price - 1) * 100 if spike_price > 0 else 0
                if max_gain < 5.0:  # 이후 5% 미만 상승 → 페이크
                    fake_signals.append({
                        "ticker": ticker,
                        "time_ms": spike_time,
                        "price": spike_price,
                        "max_later_gain_pct": round(max_gain, 1),
                    })

    # ━━ 놓친 기회 분석 ━━
    # 일중 30%+ 상승했지만 매수 안 된 종목
    for ticker, tdata in tickers_data.items():
        if tdata["daily_change_pct"] >= 30.0:
            was_bought = any(t["ticker"] == ticker for t in closed_trades)
            if not was_bought:
                # 왜 안 됐는지 분석
                has_vol_spike = any(e["is_vol_spike"] for e in tdata.get("events", []))
                meets_daily_vol = tdata["daily_volume"] >= (
                    CONFIG["min_daily_volume_highprice"] if tdata["daily_open"] >= CONFIG["highprice_threshold"]
                    else CONFIG["min_daily_volume"]
                )
                missed_entries.append({
                    "ticker": ticker,
                    "daily_change_pct": tdata["daily_change_pct"],
                    "daily_volume": tdata["daily_volume"],
                    "had_vol_spike": has_vol_spike,
                    "meets_daily_vol": meets_daily_vol,
                    "reason": (
                        "no_vol_spike" if not has_vol_spike
                        else "low_daily_vol" if not meets_daily_vol
                        else "price_not_triggered"
                    ),
                })

    day_pnl = running_krw - portfolio_krw
    day_pnl_pct = (day_pnl / portfolio_krw) * 100 if portfolio_krw > 0 else 0

    return {
        "date": date_str,
        "starting_portfolio": round(portfolio_krw, 0),
        "ending_portfolio": round(running_krw, 0),
        "day_pnl_krw": round(day_pnl, 0),
        "day_pnl_pct": round(day_pnl_pct, 2),
        "vol_spikes_detected": vol_spikes_detected,
        "signals_generated": signals_generated,
        "buys_executed": buys_executed,
        "trades": closed_trades,
        "fake_signals": fake_signals,
        "missed_entries": missed_entries,
        "fake_rate": round(len(fake_signals) / max(vol_spikes_detected, 1) * 100, 1),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    # READY 플래그 대기
    wait_count = 0
    while not READY_FLAG.exists():
        if wait_count == 0:
            print("[Simulator] 데이터 수집기 대기 중...")
        time.sleep(5)
        wait_count += 1
        if wait_count > 120:  # 10분 타임아웃
            print("[Simulator] 타임아웃: READY 플래그 없음")
            sys.exit(1)

    print(f"[Simulator] 데이터 준비 확인. 시뮬레이션 시작...")

    # processed 파일 로드
    processed_files = sorted(PROCESSED_DIR.glob("*.json"))
    print(f"[Simulator] {len(processed_files)}거래일 처리 예정")

    portfolio_krw = CONFIG["initial_krw"]
    all_day_results = []
    traded_tickers_global = set()  # 당일 리셋됨 (날짜별 분리)

    # 전체 통계
    total_trades = 0
    winning_trades = 0
    total_pnl_krw = 0
    total_fake_signals = 0
    total_vol_spikes = 0
    total_missed = 0

    for pfile in processed_files:
        with open(pfile) as f:
            date_data = json.load(f)

        result = simulate_day(date_data, portfolio_krw, traded_tickers_global)

        # 날짜별 traded_tickers 리셋 (날짜가 바뀌면 새 세션)
        traded_tickers_global = set()

        # 결과 저장
        out_path = RESULTS_DIR / f"{result['date']}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        portfolio_krw = result["ending_portfolio"]

        # 통계 집계
        day_trades = [t for t in result["trades"] if t["type"] == "full"]
        day_wins = [t for t in day_trades if t["pnl_krw"] > 0]
        total_trades += len(day_trades)
        winning_trades += len(day_wins)
        total_pnl_krw += result["day_pnl_krw"]
        total_fake_signals += len(result["fake_signals"])
        total_vol_spikes += result["vol_spikes_detected"]
        total_missed += len(result["missed_entries"])

        all_day_results.append(result)

        # 진행상황 출력
        win_rate = len(day_wins) / max(len(day_trades), 1) * 100
        print(f"  {result['date']}: "
              f"포트폴리오 ₩{portfolio_krw:,.0f} "
              f"({'+' if result['day_pnl_pct'] >= 0 else ''}{result['day_pnl_pct']:.1f}%) | "
              f"매수{result['buys_executed']}건 | "
              f"볼스파이크{result['vol_spikes_detected']}건 | "
              f"페이크{len(result['fake_signals'])}건")

    # ━━ 전체 요약 ━━
    final_return_pct = (portfolio_krw / CONFIG["initial_krw"] - 1) * 100
    win_rate = winning_trades / max(total_trades, 1) * 100
    fake_rate = total_fake_signals / max(total_vol_spikes, 1) * 100

    # 수익팩터 계산
    win_pnl = sum(t["pnl_krw"] for r in all_day_results
                  for t in r["trades"] if t["type"] == "full" and t["pnl_krw"] > 0)
    loss_pnl = abs(sum(t["pnl_krw"] for r in all_day_results
                       for t in r["trades"] if t["type"] == "full" and t["pnl_krw"] < 0))
    profit_factor = win_pnl / max(loss_pnl, 1)

    # 매도 이유 분석
    sell_reasons = {}
    for r in all_day_results:
        for t in r["trades"]:
            if t["type"] == "full":
                reason_key = t["reason"].split(" ")[0]
                sell_reasons[reason_key] = sell_reasons.get(reason_key, 0) + 1

    # 놓친 기회 이유 분석
    missed_reasons = {}
    for r in all_day_results:
        for m in r["missed_entries"]:
            missed_reasons[m["reason"]] = missed_reasons.get(m["reason"], 0) + 1

    summary = {
        "시뮬레이션_기간": f"{processed_files[0].stem} ~ {processed_files[-1].stem}",
        "거래일수": len(processed_files),
        "초기자본_KRW": CONFIG["initial_krw"],
        "최종자본_KRW": round(portfolio_krw, 0),
        "총수익률_pct": round(final_return_pct, 2),
        "총수익_KRW": round(portfolio_krw - CONFIG["initial_krw"], 0),

        "총거래건수": total_trades,
        "승리거래": winning_trades,
        "패배거래": total_trades - winning_trades,
        "승률_pct": round(win_rate, 1),
        "수익팩터": round(profit_factor, 2),

        "총볼스파이크감지": total_vol_spikes,
        "총페이크신호": total_fake_signals,
        "페이크신호율_pct": round(fake_rate, 1),
        "놓친_30pct이상_종목": total_missed,
        "놓친기회_이유": missed_reasons,

        "매도_이유별_건수": sell_reasons,

        "분석목표_답변": {
            "Q1_매수시점_실행가능": f"승률 {win_rate:.1f}%, 총 {total_trades}건 매수 실행",
            "Q2_페이크로_놓친_포인트": f"페이크율 {fake_rate:.1f}% ({total_fake_signals}/{total_vol_spikes}), 30%+종목 중 {total_missed}건 미진입",
            "Q3_매도시점_정확도": str(sell_reasons),
            "Q4_최종결과": f"₩{CONFIG['initial_krw']:,} → ₩{portfolio_krw:,.0f} ({final_return_pct:+.1f}%), PF {profit_factor:.2f}",
        }
    }

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n" + "="*60)
    print("📊 백테스트 시뮬레이션 완료")
    print("="*60)
    print(f"기간: {summary['시뮬레이션_기간']} ({summary['거래일수']}거래일)")
    print(f"자본: ₩{CONFIG['initial_krw']:,} → ₩{portfolio_krw:,.0f} ({final_return_pct:+.1f}%)")
    print(f"거래: {total_trades}건 | 승률 {win_rate:.1f}% | PF {profit_factor:.2f}")
    print(f"볼스파이크: {total_vol_spikes}건 | 페이크: {total_fake_signals}건 ({fake_rate:.1f}%)")
    print(f"30%+ 놓친 종목: {total_missed}건")
    print(f"매도 이유: {sell_reasons}")
    print(f"\n결과 저장: {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
