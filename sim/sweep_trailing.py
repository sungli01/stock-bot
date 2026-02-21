#!/usr/bin/env python3
"""
sim/sweep_trailing.py — 트레일링 파라미터 최적화 스윕

고정값:
  - 배분: 20%/20%
  - 손절: -15%
  - 나머지: sim_config.json 기본값

스윕 대상:
  - trailing_activate_pct : 트레일링 활성화 시점 (6~12%)
  - trailing_drop_low      : +8~15% 구간 허용 낙폭 (-2%p ~ -6%p)
"""
import json
import sys
import itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"

sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

# 스윕 범위
ACTIVATE_RANGE = [6.0, 8.0, 10.0, 12.0]
DROP_LOW_RANGE  = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]   # +8~15% 구간

def run_combo(activate, drop_low, dates, base_cfg):
    cfg = dict(base_cfg)
    cfg["trailing_activate_pct"] = activate
    cfg["trailing_drop_low"]     = drop_low

    krw = cfg["initial_krw"]
    day_results = []
    for d in dates:
        r = run_engine(d, krw, cfg)
        if "error" in r:
            continue
        day_results.append(r)
        krw = r["ending_krw"]

    total_trades = sum(r["buy_count"] for r in day_results)
    total_wins   = sum(r["win_count"] for r in day_results)
    total_sells  = sum(r["sell_count"] for r in day_results)
    profit_days  = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
    total_days   = len(day_results)
    win_rate     = total_wins / max(total_sells, 1) * 100
    total_return = (krw / cfg["initial_krw"] - 1) * 100

    # 평균 수익/손실
    wins_pnl   = []
    losses_pnl = []
    for r in day_results:
        for t in r.get("trades", []):
            if t["type"] == "SELL":
                p = t.get("pnl_pct", 0)
                if p > 0:  wins_pnl.append(p)
                else:      losses_pnl.append(p)

    avg_win  = sum(wins_pnl)  / max(len(wins_pnl),  1)
    avg_loss = sum(losses_pnl)/ max(len(losses_pnl), 1)

    return {
        "trailing_activate_pct": activate,
        "trailing_drop_low":     drop_low,
        "final_krw":             round(krw),
        "total_return_pct":      round(total_return, 1),
        "profit_days":           profit_days,
        "loss_days":             total_days - profit_days,
        "win_rate_pct":          round(win_rate, 1),
        "avg_win_pct":           round(avg_win, 1),
        "avg_loss_pct":          round(avg_loss, 1),
        "rr_ratio":              round(abs(avg_win / avg_loss) if avg_loss != 0 else 0, 2),
    }

if __name__ == "__main__":
    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))

    base_cfg = load_config()
    base_cfg["stop_loss_pct"]    = -15.0
    base_cfg["allocation_ratio"] = [0.2, 0.2]
    base_cfg["compound_cap_krw"] = 25_000_000

    combos = list(itertools.product(ACTIVATE_RANGE, DROP_LOW_RANGE))
    print(f"총 {len(combos)}개 조합 × 60일 시뮬")
    print("=" * 80)

    results = []
    for activate, drop_low in combos:
        r = run_combo(activate, drop_low, dates, base_cfg)
        results.append(r)
        print(f"활성화 {activate:>4.0f}%  낙폭 {drop_low:>4.0f}%p  →  "
              f"{r['total_return_pct']:>+8.1f}%  "
              f"수익일 {r['profit_days']}/{r['profit_days']+r['loss_days']}  "
              f"승률 {r['win_rate_pct']:>5.1f}%  "
              f"평균수익 +{r['avg_win_pct']:.1f}%  "
              f"평균손실 {r['avg_loss_pct']:.1f}%  "
              f"R:R {r['rr_ratio']:.2f}")

    best = max(results, key=lambda x: x["total_return_pct"])

    print()
    print("=" * 80)
    print("🏆 최적 조합")
    print(f"  트레일링 활성화: +{best['trailing_activate_pct']}%")
    print(f"  초기 낙폭 허용:  -{best['trailing_drop_low']}%p")
    print(f"  60일 수익률:     {best['total_return_pct']:+.1f}%  (최종 ₩{best['final_krw']:,})")
    print(f"  승률:            {best['win_rate_pct']:.1f}%")
    print(f"  평균수익/손실:   +{best['avg_win_pct']:.1f}% / {best['avg_loss_pct']:.1f}%")
    print(f"  R:R:             {best['rr_ratio']:.2f}")

    out = SIM_DIR / "sweep_trailing_result.json"
    with open(out, "w") as f:
        json.dump({"best": best, "all": results}, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: {out}")
