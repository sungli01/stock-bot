#!/usr/bin/env python3
"""
sim/sweep_volspike2nd.py — 2차 vol spike 임계값 최적화 스윕

고정값 (v10.1 최적):
  - 1차: vol 800%, trigger +10%, trailing +6%/-2%p
  - 2차: trigger +10%, trailing +8%/-1%p
  - 손절: -25%, 배분: 70%/30%

스윕: 2차 vol_spike_2nd_pct × 2차 trigger_2nd_pct
"""
import json, sys, itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

VOL_2ND_RANGE     = [100.0, 150.0, 200.0, 300.0, 500.0]
TRIGGER_2ND_RANGE = [5.0, 8.0, 10.0, 15.0, 20.0]

def run_combo(vol2, trig2, dates, base_cfg):
    cfg = dict(base_cfg)
    cfg["vol_spike_2nd_pct"]  = vol2
    cfg["trigger_2nd_pct"]    = trig2

    krw = cfg["initial_krw"]
    day_results = []
    for d in dates:
        r = run_engine(d, krw, cfg)
        if "error" in r: continue
        day_results.append(r)
        krw = r["ending_krw"]

    total_sells = sum(r["sell_count"] for r in day_results)
    total_wins  = sum(r["win_count"]  for r in day_results)
    profit_days = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
    total_days  = len(day_results)
    win_rate    = total_wins / max(total_sells, 1) * 100
    total_return= (krw / cfg["initial_krw"] - 1) * 100
    total_trades= sum(r["buy_count"] for r in day_results)

    wins_pnl, losses_pnl = [], []
    for r in day_results:
        for t in r.get("trades", []):
            if t["type"] == "SELL":
                p = t.get("pnl_pct", 0)
                (wins_pnl if p > 0 else losses_pnl).append(p)

    avg_win  = sum(wins_pnl)  / max(len(wins_pnl), 1)
    avg_loss = sum(losses_pnl)/ max(len(losses_pnl), 1)
    rr = abs(avg_win / avg_loss) if avg_loss else 0

    return {
        "vol_spike_2nd_pct": vol2,
        "trigger_2nd_pct":   trig2,
        "final_krw":         round(krw),
        "total_return_pct":  round(total_return, 1),
        "profit_days":       profit_days,
        "loss_days":         total_days - profit_days,
        "total_trades":      total_trades,
        "win_rate_pct":      round(win_rate, 1),
        "avg_win_pct":       round(avg_win, 1),
        "avg_loss_pct":      round(avg_loss, 1),
        "rr_ratio":          round(rr, 2),
    }

if __name__ == "__main__":
    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))
    base_cfg = load_config()
    base_cfg.update({
        # v10.1 고정값
        "vol_spike_1st_pct":        800.0,
        "trigger_1st_pct":          10.0,
        "trailing_activate_pct":    6.0,
        "trailing_drop_low":        2.0,
        "trailing_activate_pct_2nd":8.0,
        "trailing_drop_low_2nd":    1.0,
        "stop_loss_pct":            -25.0,
        "allocation_ratio":         [0.7, 0.3],
        "compound_cap_krw":         25_000_000,
    })

    combos = list(itertools.product(VOL_2ND_RANGE, TRIGGER_2ND_RANGE))
    baseline = 3253.2
    print(f"총 {len(combos)}개 조합 × 60일 시뮬")
    print(f"베이스라인 (vol 200% / trigger +10%): +{baseline}%")
    print("=" * 82)
    print(f"{'2차볼':>8}  {'2차트리거':>8}  {'60일수익':>10}  {'최종금액':>13}  {'거래':>5}  {'승률':>6}  {'R:R':>5}  {'vs베이스':>9}")
    print("-" * 82)

    results = []
    for vol2, trig2 in combos:
        r = run_combo(vol2, trig2, dates, base_cfg)
        results.append(r)
        diff = r["total_return_pct"] - baseline
        best_mark = ""
        print(f"{vol2:>7.0f}%  +{trig2:>6.0f}%  "
              f"{r['total_return_pct']:>+9.1f}%  "
              f"₩{r['final_krw']:>12,}  "
              f"{r['total_trades']:>5}건  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"{r['rr_ratio']:>5.2f}  "
              f"{diff:>+8.1f}%p")

    best = max(results, key=lambda x: x["total_return_pct"])
    print()
    print("=" * 82)
    print(f"🏆 최적: 2차 vol {best['vol_spike_2nd_pct']:.0f}% / 트리거 +{best['trigger_2nd_pct']:.0f}%")
    print(f"   60일: {best['total_return_pct']:+.1f}%  (₩{best['final_krw']:,})")
    print(f"   베이스라인 대비: {best['total_return_pct']-baseline:+.1f}%p")

    out = SIM_DIR / "sweep_volspike2nd_result.json"
    with open(out, "w") as f:
        json.dump({"best": best, "all": results}, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: {out}")
