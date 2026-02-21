#!/usr/bin/env python3
"""
sim/sweep_trailing2nd.py — 2차 전용 트레일링 최적화 스윕

고정값 (v10 최적):
  - vol_spike_1st: 800%,  trigger: +10%
  - 1차 트레일링: 활성화 +6%, 낙폭 -2%p
  - 손절: -25%,  배분: 70%/30%

스윕: 2차 트레일링 활성화 × 낙폭
  - trailing_activate_pct_2nd : 3% ~ 8%
  - trailing_drop_low_2nd     : 1.0%p ~ 3.0%p
"""
import json, sys, itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

ACTIVATE_2ND = [3.0, 4.0, 5.0, 6.0, 8.0]
DROP_LOW_2ND = [1.0, 1.5, 2.0, 2.5, 3.0]

def run_combo(act2, drop2, dates, base_cfg):
    cfg = dict(base_cfg)
    cfg["trailing_activate_pct_2nd"] = act2
    cfg["trailing_drop_low_2nd"]     = drop2

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
        "trailing_activate_pct_2nd": act2,
        "trailing_drop_low_2nd":     drop2,
        "final_krw":         round(krw),
        "total_return_pct":  round(total_return, 1),
        "profit_days":       profit_days,
        "loss_days":         total_days - profit_days,
        "win_rate_pct":      round(win_rate, 1),
        "avg_win_pct":       round(avg_win, 1),
        "avg_loss_pct":      round(avg_loss, 1),
        "rr_ratio":          round(rr, 2),
    }

if __name__ == "__main__":
    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))

    base_cfg = load_config()
    base_cfg.update({
        "vol_spike_1st_pct":      800.0,
        "trigger_1st_pct":        10.0,
        "trailing_activate_pct":  6.0,    # 1차 고정
        "trailing_drop_low":      2.0,    # 1차 고정
        "stop_loss_pct":          -25.0,
        "allocation_ratio":       [0.7, 0.3],
        "compound_cap_krw":       25_000_000,
    })

    combos = list(itertools.product(ACTIVATE_2ND, DROP_LOW_2ND))
    print(f"총 {len(combos)}개 조합 × 60일 시뮬")
    print(f"(1차 트레일링 고정: 활성화 +6% / 낙폭 -2%p)")
    print("=" * 80)
    print(f"{'2차활성화':>8}  {'2차낙폭':>7}  {'60일수익':>10}  "
          f"{'수익일':>7}  {'승률':>6}  {'평균수익':>7}  {'R:R':>5}")
    print("-" * 80)

    results = []
    for act2, drop2 in combos:
        r = run_combo(act2, drop2, dates, base_cfg)
        results.append(r)
        print(f"  +{act2:>4.1f}%    -{drop2:>4.1f}%p  "
              f"{r['total_return_pct']:>+9.1f}%  "
              f"{r['profit_days']:>2}/{r['profit_days']+r['loss_days']:<4}  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"+{r['avg_win_pct']:>5.1f}%  "
              f"{r['rr_ratio']:>5.2f}")

    best = max(results, key=lambda x: x["total_return_pct"])

    print()
    print("=" * 80)
    print("🏆 최적 2차 트레일링")
    print(f"  활성화: +{best['trailing_activate_pct_2nd']}%  /  낙폭: -{best['trailing_drop_low_2nd']}%p")
    print(f"  60일:   {best['total_return_pct']:+.1f}%  (₩{best['final_krw']:,})")
    print(f"  승률:   {best['win_rate_pct']}%  /  R:R: {best['rr_ratio']}")
    print()
    print("[ 베이스라인 비교 ]")
    print(f"  1차=2차 동일 (활성 +6% / 낙폭 -2%p): +739.5%")
    print(f"  최적 2차 분리:                         {best['total_return_pct']:+.1f}%")

    out = SIM_DIR / "sweep_trailing2nd_result.json"
    with open(out, "w") as f:
        json.dump({"best": best, "all": results}, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: {out}")
