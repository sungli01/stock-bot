#!/usr/bin/env python3
"""
Sweep B: 손절 후 재진입 차단 (block_after_stoploss) × stop_loss
손절 발생 시 해당 종목 2차/3차 완전 차단 → 재진입 손절 방지
"""
import json
from pathlib import Path
from collections import defaultdict
from sim.engine import load_config

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
RESULTS_DIR = SIM_DIR / "results"


def compute_3min_vol(bar_buffer):
    if len(bar_buffer) < 6:
        return 0.0, 0.0
    cur_vol = sum(b["v"] for b in bar_buffer[-3:])
    prev_vol = sum(b["v"] for b in bar_buffer[-6:-3])
    return float(cur_vol), float(prev_vol)


def get_trailing_drop(peak_pct, elapsed_min, cfg, is_second=False, is_third=False):
    if is_third:
        if peak_pct >= 80: base = cfg.get("trailing_drop_rocket", 30.0)
        elif peak_pct >= 50: base = cfg.get("trailing_drop_high", 8.0)
        elif peak_pct >= 15: base = cfg.get("trailing_drop_mid", 5.0)
        else: base = cfg.get("trailing_drop_low_3rd", 0.5)
    elif is_second:
        if peak_pct >= 80: base = cfg.get("trailing_drop_rocket", 30.0)
        elif peak_pct >= 50: base = cfg.get("trailing_drop_high", 8.0)
        elif peak_pct >= 15: base = cfg.get("trailing_drop_mid", 5.0)
        else: base = cfg.get("trailing_drop_low_2nd", 1.0)
    else:
        if peak_pct >= 80: base = cfg.get("trailing_drop_rocket", 30.0)
        elif peak_pct >= 50: base = cfg.get("trailing_drop_high", 8.0)
        elif peak_pct >= 15: base = cfg.get("trailing_drop_mid", 5.0)
        else: base = cfg.get("trailing_drop_low", 2.0)
    if elapsed_min >= 30:
        base *= cfg.get("trailing_time_multiplier", 0.8)
    return base


class Position:
    def __init__(self, ticker, entry_price, buy_krw, entry_time_ms, queue_price, is_second, is_third=False):
        self.ticker = ticker
        self.entry_price = entry_price
        self.buy_krw = buy_krw
        self.entry_time_ms = entry_time_ms
        self.queue_price = queue_price
        self.is_second = is_second
        self.is_third = is_third
        self.peak_price = entry_price
        self.trailing_active = False

    def elapsed_min(self, ts): return (ts - self.entry_time_ms) / 60000
    def pnl_pct(self, p): return (p / self.entry_price - 1) * 100
    def pnl_krw(self, p): return self.buy_krw * (p / self.entry_price - 1)

    def check_exit(self, price, ts, cfg):
        pnl = self.pnl_pct(price)
        elapsed = self.elapsed_min(ts)
        if price > self.peak_price: self.peak_price = price
        if pnl <= cfg["stop_loss_pct"]: return True, f"STOP_LOSS({pnl:.1f}%)"
        if elapsed >= cfg["max_hold_min"]: return True, f"TIME_LIMIT({pnl:.1f}%)"
        peak_pnl = self.pnl_pct(self.peak_price)
        if self.is_third:
            act = cfg.get("trailing_activate_pct_3rd", 10.0)
        elif self.is_second:
            act = cfg.get("trailing_activate_pct_2nd", 8.0)
        else:
            act = cfg.get("trailing_activate_pct", 6.0)
        if peak_pnl >= act: self.trailing_active = True
        if self.trailing_active:
            drop = get_trailing_drop(peak_pnl, elapsed, cfg, self.is_second, self.is_third)
            if peak_pnl - pnl >= drop: return True, f"TRAILING(+{peak_pnl:.1f}%→+{pnl:.1f}%)"
        return False, ""


def run_engine_with_block(date_str, portfolio_krw, cfg, block_after_stop=True):
    stream_path = STREAM_DIR / f"{date_str}.json"
    if not stream_path.exists():
        return {"error": "no stream"}

    with open(stream_path) as f:
        stream_data = json.load(f)
    events = stream_data.get("stream", [])
    if not events:
        return {"error": "empty stream"}

    queue = {}
    positions = {}
    trades = []
    bar_buffers = defaultdict(list)

    traded_once   = set()
    traded_twice  = set()
    traded_thrice = set()
    stop_lossed   = set()  # ← 손절 종목 추적 (block_after_stop용)

    running_krw = portfolio_krw

    for event in events:
        ticker = event["ticker"]
        ts = event["time_ms"]
        cur_price = event["c"]
        daily_open = event["daily_open"]
        daily_vol = event["daily_volume_so_far"]

        if cur_price <= 0:
            continue

        expire_ms = cfg["queue_expire_min"] * 60 * 1000
        expired = [t for t, q in queue.items() if ts - q["time_ms"] > expire_ms]
        for t in expired:
            del queue[t]

        bar_buffers[ticker].append(event)
        if len(bar_buffers[ticker]) > 6:
            bar_buffers[ticker] = bar_buffers[ticker][-6:]

        cur_vol, prev_vol = compute_3min_vol(bar_buffers[ticker])
        if cur_vol > 0 and prev_vol > 0:
            vol_ratio = (cur_vol / prev_vol) * 100

            if ticker in traded_thrice:
                continue
            # [NEW] 손절 후 재진입 차단
            if block_after_stop and ticker in stop_lossed:
                continue

            is_second = ticker in traded_once  and ticker not in traded_twice
            is_third  = ticker in traded_twice and ticker not in traded_thrice
            is_additional = is_second or is_third

            change_from_open = (cur_price / daily_open - 1) * 100
            if not is_additional:
                is_candidate = cfg["candidate_change_pct"] <= change_from_open < cfg["candidate_max_change_pct"]
            else:
                is_candidate = True

            if is_third:
                threshold = cfg.get("vol_spike_3rd_pct", cfg["vol_spike_2nd_pct"])
            elif is_additional:
                threshold = cfg["vol_spike_2nd_pct"]
            else:
                threshold = cfg["vol_spike_1st_pct"]

            if vol_ratio >= threshold and is_candidate and ticker not in queue:
                queue[ticker] = {
                    "price": cur_price, "time_ms": ts,
                    "is_second": is_additional, "is_third": is_third,
                    "vol_ratio": vol_ratio, "vol_at_queue": daily_vol,
                }

        if ticker in queue and ticker not in positions:
            q = queue[ticker]
            is_additional = q["is_second"]
            is_third = q.get("is_third", False)
            q_price = q["price"]
            pct_from_q = (cur_price / q_price - 1) * 100

            if pct_from_q > cfg["max_pct_from_queue"]:
                del queue[ticker]
                continue

            if is_third: trigger = cfg.get("trigger_3rd_pct", cfg["trigger_2nd_pct"])
            elif is_additional: trigger = cfg["trigger_2nd_pct"]
            else: trigger = cfg["trigger_1st_pct"]

            if pct_from_q >= trigger:
                if not is_additional:
                    req = 50000 if cur_price >= 10 else 300000
                    if daily_vol < req: continue
                if len(positions) >= cfg["max_positions"]: continue

                usd_krw = cfg.get("usd_krw_rate", 1450.0)
                vol_at_queue = q.get("vol_at_queue", 0)
                vol_since_queue = max(daily_vol - vol_at_queue, 1)
                vol_cap = (cfg.get("vol_cap_2nd_pct", 10.0) if is_additional else cfg.get("vol_cap_1st_pct", 30.0)) / 100.0
                max_krw_by_vol = vol_since_queue * vol_cap * cur_price * usd_krw

                cap = cfg.get("compound_cap_krw", 25_000_000)
                deployed = sum(p.buy_krw for p in positions.values())
                base_krw = min(running_krw + deployed, cap)
                max_single = cfg.get("max_single_buy_krw", 50_000_000)

                if is_additional:
                    buy_krw = min(running_krw, max_krw_by_vol, max_single)
                else:
                    alloc = cfg["allocation_ratio"]
                    pos_idx = len(positions)
                    alloc_pct = alloc[pos_idx] if pos_idx < len(alloc) else alloc[-1]
                    buy_krw = min(base_krw * alloc_pct, max_krw_by_vol)

                buy_krw = min(buy_krw, running_krw)
                if buy_krw <= 0: continue

                pos = Position(ticker, cur_price, buy_krw, ts, q_price, is_additional, is_third)
                positions[ticker] = pos
                running_krw -= buy_krw
                del queue[ticker]
                trades.append({"type": "BUY", "ticker": ticker, "entry_type": "3차" if is_third else ("2차" if is_additional else "1차"), "buy_krw": buy_krw})

        if ticker in positions:
            pos = positions[ticker]
            sell, reason = pos.check_exit(cur_price, ts, cfg)
            if sell:
                pnl_k = pos.pnl_krw(cur_price)
                running_krw += pos.buy_krw + pnl_k
                et = "3차" if pos.is_third else ("2차" if pos.is_second else "1차")

                is_stop = "STOP_LOSS" in reason
                if is_stop and block_after_stop:
                    stop_lossed.add(ticker)  # 손절 → 이후 재진입 차단
                    traded_thrice.add(ticker)  # 완전 차단

                if et == "3차": traded_thrice.add(ticker)
                elif et == "2차": traded_twice.add(ticker)
                else: traded_once.add(ticker)

                trades.append({"type": "SELL", "ticker": ticker, "entry_type": et,
                               "pnl_pct": round(pos.pnl_pct(cur_price), 2), "pnl_krw": round(pnl_k),
                               "buy_krw": round(pos.buy_krw), "reason": reason.split("(")[0]})
                del positions[ticker]

    for ticker, pos in positions.items():
        last_price = bar_buffers[ticker][-1]["c"] if bar_buffers[ticker] else pos.entry_price
        pnl_k = pos.pnl_krw(last_price)
        running_krw += pos.buy_krw + pnl_k
        et = "3차" if pos.is_third else ("2차" if pos.is_second else "1차")
        trades.append({"type": "SELL", "ticker": ticker, "entry_type": et,
                       "pnl_pct": round(pos.pnl_pct(last_price), 2), "pnl_krw": round(pnl_k),
                       "buy_krw": round(pos.buy_krw), "reason": "FORCE_CLOSE_EOD"})

    sells = [t for t in trades if t["type"] == "SELL"]
    wins = [t for t in sells if t.get("pnl_krw", 0) > 0]
    losses = [t for t in sells if t.get("pnl_krw", 0) <= 0]
    win_sum = sum(t["pnl_pct"] for t in wins)
    loss_sum = abs(sum(t["pnl_pct"] for t in losses))
    return {
        "ending_krw": round(running_krw),
        "day_pnl_pct": round((running_krw / portfolio_krw - 1) * 100, 2),
        "buys": len([t for t in trades if t["type"]=="BUY"]),
        "wins": len(wins), "losses": len(losses),
        "pf": round(win_sum / max(loss_sum, 0.001), 2),
        "win_pnls": [t["pnl_pct"] for t in wins],
        "loss_pnls": [t["pnl_pct"] for t in losses],
    }


def run_sweep():
    base_cfg = load_config()
    dates = sorted(p.stem for p in (SIM_DIR / "stream").glob("*.json"))
    INITIAL = 1_000_000

    combos = [
        (False, -25.0), (True,  -25.0),
        (False, -20.0), (True,  -20.0),
        (False, -15.0), (True,  -15.0),
        (False, -10.0), (True,  -10.0),
    ]

    results = []
    print(f"Sweep B: 손절 재진입 차단 ON/OFF × stop_loss (8 combos × {len(dates)}일)")
    print(f"{'block':>6} {'stop':>6} | {'수익률':>9} {'수익일':>7} {'승률':>7} {'PF':>6} {'avg_w':>7} {'avg_l':>7}")
    print("-" * 70)

    for block, sl in combos:
        cfg = dict(base_cfg)
        cfg["stop_loss_pct"] = sl

        portfolio = INITIAL
        cap_hit = False
        CAP = cfg.get("compound_cap_krw", 25_000_000)
        all_wins, all_losses = [], []
        profit_days = 0

        for date in dates:
            r = run_engine_with_block(date, portfolio, cfg, block_after_stop=block)
            if "error" in r: continue
            all_wins.extend(r["win_pnls"])
            all_losses.extend(r["loss_pnls"])
            if r["day_pnl_pct"] > 0: profit_days += 1
            ending = r["ending_krw"]
            if ending >= CAP and not cap_hit: cap_hit = True
            portfolio = min(ending, CAP)

        final = portfolio
        total_ret = (final / INITIAL - 1) * 100
        wr = len(all_wins) / max(len(all_wins)+len(all_losses), 1) * 100
        avg_w = sum(all_wins)/len(all_wins) if all_wins else 0
        avg_l = sum(all_losses)/len(all_losses) if all_losses else 0
        pf = sum(all_wins)/max(abs(sum(all_losses)), 0.001)
        cap_mark = "🎯" if cap_hit else "  "

        block_str = "ON " if block else "OFF"
        print(f"{block_str:>6} {sl:>5.0f}% | {total_ret:>+8.1f}% {profit_days:>5}/{len(dates)} {wr:>6.1f}% {pf:>5.2f} {avg_w:>+6.1f}% {avg_l:>+6.1f}% {cap_mark}")

        results.append({
            "block_after_stop": block, "stop_loss": sl,
            "total_return_pct": round(total_ret, 2),
            "profit_days": profit_days, "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2), "avg_win": round(avg_w, 2),
            "avg_loss": round(avg_l, 2), "final_krw": final, "cap_hit": cap_hit,
        })

    results.sort(key=lambda x: -x["total_return_pct"])
    print(f"\n🏆 Best: block={results[0]['block_after_stop']} / stop={results[0]['stop_loss']}% → {results[0]['total_return_pct']:+.1f}%")
    with open(SIM_DIR / "sweep_B_result.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"저장: sim/sweep_B_result.json")
    return results[0]

if __name__ == "__main__":
    run_sweep()
