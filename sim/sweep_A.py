#!/usr/bin/env python3
"""
Sweep A: stop_loss × max_hold_min
-15/-20/-25% × 60/90/120분 = 9 combos × 60일
"""
import json
from pathlib import Path
from sim.engine import run_engine, load_config

SIM_DIR = Path(__file__).parent

def run_sweep():
    base_cfg = load_config()
    dates = sorted(p.stem for p in (SIM_DIR / "stream").glob("*.json"))

    stop_losses = [-15.0, -20.0, -25.0]
    hold_mins   = [60, 90, 120]
    INITIAL = 1_000_000

    results = []
    print(f"Sweep A: stop_loss × max_hold_min ({len(stop_losses)*len(hold_mins)} combos × {len(dates)}일)")
    print(f"{'stop':>6} {'hold':>5} | {'수익률':>9} {'수익일':>7} {'승률':>7} {'PF':>6} {'avg_w':>7} {'avg_l':>7}")
    print("-" * 70)

    for sl in stop_losses:
        for hold in hold_mins:
            cfg = dict(base_cfg)
            cfg["stop_loss_pct"] = sl
            cfg["max_hold_min"] = hold
            CAP = cfg.get("compound_cap_krw", 25_000_000)

            portfolio = INITIAL
            cap_hit = False
            all_wins, all_losses = [], []
            profit_days = 0

            for date in dates:
                r = run_engine(date, portfolio, cfg)
                if "error" in r:
                    continue
                trades = r.get("trades", [])
                for t in trades:
                    if t["type"] == "SELL":
                        if t.get("pnl_pct", 0) > 0:
                            all_wins.append(t["pnl_pct"])
                        else:
                            all_losses.append(t["pnl_pct"])
                if r["day_pnl_pct"] > 0:
                    profit_days += 1
                ending = r["ending_krw"]
                if ending >= CAP and not cap_hit:
                    cap_hit = True
                portfolio = min(ending, CAP)

            final = portfolio
            total_ret = (final / INITIAL - 1) * 100
            wr = len(all_wins) / max(len(all_wins)+len(all_losses), 1) * 100
            avg_w = sum(all_wins)/len(all_wins) if all_wins else 0
            avg_l = sum(all_losses)/len(all_losses) if all_losses else 0
            pf = sum(all_wins)/max(abs(sum(all_losses)), 0.001)

            cap_mark = "🎯" if cap_hit else "  "
            print(f"{sl:>6.0f}% {hold:>4}분 | {total_ret:>+8.1f}% {profit_days:>5}/{len(dates)} {wr:>6.1f}% {pf:>5.2f} {avg_w:>+6.1f}% {avg_l:>+6.1f}% {cap_mark}")

            results.append({
                "stop_loss": sl, "max_hold_min": hold,
                "total_return_pct": round(total_ret, 2),
                "profit_days": profit_days,
                "win_rate": round(wr, 1),
                "profit_factor": round(pf, 2),
                "avg_win": round(avg_w, 2),
                "avg_loss": round(avg_l, 2),
                "final_krw": final,
                "cap_hit": cap_hit,
            })

    results.sort(key=lambda x: -x["total_return_pct"])
    print(f"\n🏆 Best: stop={results[0]['stop_loss']}% / hold={results[0]['max_hold_min']}분 → {results[0]['total_return_pct']:+.1f}%")

    with open(SIM_DIR / "sweep_A_result.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"저장: sim/sweep_A_result.json")
    return results[0]

if __name__ == "__main__":
    run_sweep()
