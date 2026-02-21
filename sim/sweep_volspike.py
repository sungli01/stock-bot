#!/usr/bin/env python3
"""
sim/sweep_volspike.py — vol spike 임계값 최적화 스윕

고정값:
  - 배분: 20%/20%
  - 손절: -15%
  - 트레일링: 활성화 +6%, 낙폭 -2%p (최적값)

스윕 대상:
  - vol_spike_1st_pct : 1차 vol spike 기준 (1000~5000%)
  - trigger_1st_pct   : 1차 진입 트리거 (10~20%)
"""
import json
import sys
import itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"

sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

VOL_SPIKE_RANGE = [800.0, 1000.0, 1500.0, 2000.0, 3000.0]
TRIGGER_RANGE   = [10.0, 15.0, 20.0]

def run_combo(vol_spike, trigger, dates, base_cfg):
    cfg = dict(base_cfg)
    cfg["vol_spike_1st_pct"] = vol_spike
    cfg["trigger_1st_pct"]   = trigger

    krw = cfg["initial_krw"]
    day_results = []
    for d in dates:
        r = run_engine(d, krw, cfg)
        if "error" in r:
            continue
        day_results.append(r)
        krw = r["ending_krw"]

    if not day_results:
        return None

    total_wins  = sum(r["win_count"]  for r in day_results)
    total_sells = sum(r["sell_count"] for r in day_results)
    profit_days = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
    total_days  = len(day_results)
    win_rate    = total_wins / max(total_sells, 1) * 100
    total_return = (krw / cfg["initial_krw"] - 1) * 100
    total_trades = sum(r["buy_count"] for r in day_results)

    wins_pnl, losses_pnl = [], []
    for r in day_results:
        for t in r.get("trades", []):
            if t["type"] == "SELL":
                p = t.get("pnl_pct", 0)
                (wins_pnl if p > 0 else losses_pnl).append(p)

    avg_win  = sum(wins_pnl)  / max(len(wins_pnl), 1)
    avg_loss = sum(losses_pnl)/ max(len(losses_pnl), 1)
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    be_winrate = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100

    return {
        "vol_spike_1st_pct": vol_spike,
        "trigger_1st_pct":   trigger,
        "final_krw":         round(krw),
        "total_return_pct":  round(total_return, 1),
        "profit_days":       profit_days,
        "loss_days":         total_days - profit_days,
        "total_trades":      total_trades,
        "win_rate_pct":      round(win_rate, 1),
        "avg_win_pct":       round(avg_win, 1),
        "avg_loss_pct":      round(avg_loss, 1),
        "rr_ratio":          round(rr, 2),
        "be_winrate":        round(be_winrate, 1),
    }

if __name__ == "__main__":
    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))

    base_cfg = load_config()
    base_cfg["stop_loss_pct"]        = -15.0
    base_cfg["allocation_ratio"]     = [0.2, 0.2]
    base_cfg["compound_cap_krw"]     = 25_000_000
    base_cfg["trailing_activate_pct"]= 6.0
    base_cfg["trailing_drop_low"]    = 2.0

    combos = list(itertools.product(VOL_SPIKE_RANGE, TRIGGER_RANGE))
    print(f"총 {len(combos)}개 조합 × 60일 시뮬")
    print("=" * 85)
    print(f"{'볼스파이크':>8}  {'트리거':>6}  {'60일수익':>10}  {'수익일':>7}  {'거래':>5}  {'승률':>6}  {'평균수익':>7}  {'손익분기':>8}  {'R:R':>5}")
    print("-" * 85)

    results = []
    for vol_spike, trigger in combos:
        r = run_combo(vol_spike, trigger, dates, base_cfg)
        if not r:
            continue
        results.append(r)
        gap = r["win_rate_pct"] - r["be_winrate"]
        gap_str = f"{gap:+.1f}%p"
        print(f"{vol_spike:>8.0f}%  {trigger:>5.0f}%  "
              f"{r['total_return_pct']:>+9.1f}%  "
              f"{r['profit_days']:>2}/{r['profit_days']+r['loss_days']:<4}  "
              f"{r['total_trades']:>5}건  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"+{r['avg_win_pct']:>5.1f}%  "
              f"{r['be_winrate']:>6.1f}% ({gap_str})  "
              f"{r['rr_ratio']:>5.2f}")

    best = max(results, key=lambda x: x["total_return_pct"])

    print()
    print("=" * 85)
    print(f"🏆 최적: vol spike {best['vol_spike_1st_pct']:.0f}%  트리거 +{best['trigger_1st_pct']:.0f}%")
    print(f"   60일: {best['total_return_pct']:+.1f}%  (최종 ₩{best['final_krw']:,})")
    print(f"   승률: {best['win_rate_pct']}%  vs  손익분기: {best['be_winrate']}%  (격차: {best['win_rate_pct']-best['be_winrate']:+.1f}%p)")
    print(f"   평균수익: +{best['avg_win_pct']}%  /  평균손실: {best['avg_loss_pct']}%  /  R:R: {best['rr_ratio']}")

    out = SIM_DIR / "sweep_volspike_result.json"
    with open(out, "w") as f:
        json.dump({"best": best, "all": results}, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: {out}")
