#!/usr/bin/env python3
"""
sim/sweep_3rd.py — 3차 전용 파라미터 최적화 스윕

고정값 (v10.2):
  - 1차: vol 800%, trigger +10%, trailing +6%/-2%p
  - 2차: vol 300%, trigger +10%, trailing +8%/-1%p
  - 손절: -25%, 배분: 70%/30%

스윕: 3차 vol spike × 3차 trigger × 3차 trailing
"""
import json, sys, itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

# 3차 vol spike & trigger
VOL_3RD     = [200.0, 300.0, 500.0, 800.0]
TRIGGER_3RD = [5.0, 8.0, 10.0, 15.0]
# 3차 트레일링 (활성화 × 낙폭)
ACTIVATE_3RD = [5.0, 8.0, 10.0]
DROP_3RD     = [0.5, 1.0, 1.5]

def run_combo(cfg_override, dates, base_cfg):
    cfg = dict(base_cfg)
    cfg.update(cfg_override)
    krw = cfg["initial_krw"]
    day_results = []
    for d in dates:
        r = run_engine(d, krw, cfg)
        if "error" in r: continue
        day_results.append(r)
        krw = r["ending_krw"]

    total_sells  = sum(r["sell_count"] for r in day_results)
    total_wins   = sum(r["win_count"]  for r in day_results)
    profit_days  = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
    total_days   = len(day_results)
    win_rate     = total_wins / max(total_sells, 1) * 100
    total_return = (krw / cfg["initial_krw"] - 1) * 100
    total_trades = sum(r["buy_count"] for r in day_results)

    # 3차 거래만 분리
    trades_3rd_win = trades_3rd_loss = 0
    for r in day_results:
        for t in r.get("trades", []):
            if t["type"] == "SELL" and t.get("entry_type") == "3차":
                if t.get("pnl_krw", 0) > 0: trades_3rd_win += 1
                else:                         trades_3rd_loss += 1

    return dict(
        final_krw=round(krw),
        total_return_pct=round(total_return, 1),
        profit_days=profit_days,
        loss_days=total_days - profit_days,
        total_trades=total_trades,
        win_rate_pct=round(win_rate, 1),
        trades_3rd_win=trades_3rd_win,
        trades_3rd_loss=trades_3rd_loss,
        **cfg_override,
    )

if __name__ == "__main__":
    dates    = sorted(p.stem for p in STREAM_DIR.glob("*.json"))
    base_cfg = load_config()
    base_cfg.update({
        "vol_spike_1st_pct":         800.0,
        "trigger_1st_pct":           10.0,
        "trailing_activate_pct":     6.0,
        "trailing_drop_low":         2.0,
        "vol_spike_2nd_pct":         300.0,
        "trigger_2nd_pct":           10.0,
        "trailing_activate_pct_2nd": 8.0,
        "trailing_drop_low_2nd":     1.0,
        "stop_loss_pct":             -25.0,
        "allocation_ratio":          [0.7, 0.3],
        "compound_cap_krw":          25_000_000,
    })

    baseline = 5837.4

    # Phase 1: vol × trigger 스윕
    print("=" * 80)
    print(f"Phase 1 — 3차 vol spike × trigger (trailing: +8%/-1%p 고정)")
    print(f"베이스라인: +{baseline}%")
    print("=" * 80)
    combos1 = list(itertools.product(VOL_3RD, TRIGGER_3RD))
    p1_results = []
    for vol3, trig3 in combos1:
        r = run_combo({
            "vol_spike_3rd_pct": vol3, "trigger_3rd_pct": trig3,
            "trailing_activate_pct_3rd": 8.0, "trailing_drop_low_3rd": 1.0,
        }, dates, base_cfg)
        r.update(vol_spike_3rd_pct=vol3, trigger_3rd_pct=trig3)
        p1_results.append(r)
        diff = r["total_return_pct"] - baseline
        w3 = r["trades_3rd_win"]; l3 = r["trades_3rd_loss"]
        print(f"3차vol {vol3:>5.0f}%  +{trig3:>4.0f}%  {r['total_return_pct']:>+9.1f}%  "
              f"₩{r['final_krw']:>13,}  3차({w3}승/{l3}패)  {diff:>+8.1f}%p")

    best1 = max(p1_results, key=lambda x: x["total_return_pct"])
    print(f"\n✅ Phase1 최적: vol {best1['vol_spike_3rd_pct']:.0f}% / +{best1['trigger_3rd_pct']:.0f}%  →  {best1['total_return_pct']:+.1f}%")

    # Phase 2: trailing 스윕 (vol/trigger 최적값 고정)
    print()
    print("=" * 80)
    print(f"Phase 2 — 3차 트레일링 (vol {best1['vol_spike_3rd_pct']:.0f}% / +{best1['trigger_3rd_pct']:.0f}% 고정)")
    print("=" * 80)
    combos2 = list(itertools.product(ACTIVATE_3RD, DROP_3RD))
    p2_results = []
    for act3, drop3 in combos2:
        r = run_combo({
            "vol_spike_3rd_pct":         best1["vol_spike_3rd_pct"],
            "trigger_3rd_pct":           best1["trigger_3rd_pct"],
            "trailing_activate_pct_3rd": act3,
            "trailing_drop_low_3rd":     drop3,
        }, dates, base_cfg)
        r.update(trailing_activate_pct_3rd=act3, trailing_drop_low_3rd=drop3)
        p2_results.append(r)
        diff = r["total_return_pct"] - baseline
        print(f"3차trailing +{act3:>4.1f}% / -{drop3:>3.1f}%p  {r['total_return_pct']:>+9.1f}%  "
              f"₩{r['final_krw']:>13,}  {r['win_rate_pct']:>5.1f}%  {diff:>+8.1f}%p")

    best2 = max(p2_results, key=lambda x: x["total_return_pct"])
    print(f"\n✅ Phase2 최적: +{best2['trailing_activate_pct_3rd']}% / -{best2['trailing_drop_low_3rd']}%p  →  {best2['total_return_pct']:+.1f}%")

    out = SIM_DIR / "sweep_3rd_result.json"
    with open(out, "w") as f:
        json.dump({"phase1_best": best1, "phase2_best": best2,
                   "phase1": p1_results, "phase2": p2_results}, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: {out}")
    print()
    print("=" * 80)
    print(f"🏆 3차 최종 최적")
    print(f"  vol spike  : {best1['vol_spike_3rd_pct']:.0f}%")
    print(f"  trigger    : +{best1['trigger_3rd_pct']:.0f}%")
    print(f"  trailing   : +{best2['trailing_activate_pct_3rd']}% / -{best2['trailing_drop_low_3rd']}%p")
    print(f"  60일 수익  : {best2['total_return_pct']:+.1f}%  (₩{best2['final_krw']:,})")
    print(f"  vs 베이스  : {best2['total_return_pct']-baseline:+.1f}%p")
