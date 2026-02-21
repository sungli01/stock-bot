#!/usr/bin/env python3
"""
sim/sweep_stoploss.py — 손절 비율 최적화 스윕
배분: [0.5, 0.5] (5:5 고정)
손절: -5% ~ -25% 스윕
각 설정으로 60일 전체 시뮬 → 최적값 도출
"""
import json
import sys
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"

# engine import
sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

STOP_LOSS_RANGE = [-5.0, -7.0, -10.0, -12.0, -15.0, -18.0, -20.0, -25.0, -30.0]

def run_sweep(stop_loss_vals, alloc_ratio):
    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))

    results = []
    for sl in stop_loss_vals:
        cfg = load_config()
        cfg["stop_loss_pct"]     = sl
        cfg["allocation_ratio"]  = alloc_ratio
        cfg["compound_cap_krw"]  = 25_000_000
        cfg["compound_mode"]     = True

        krw = cfg["initial_krw"]
        cap = cfg["compound_cap_krw"]

        day_results = []
        for d in dates:
            r = run_engine(d, krw, cfg)
            if "error" in r:
                continue
            day_results.append(r)
            # 복리 누적
            krw = r["ending_krw"]

        total_days    = len(day_results)
        profit_days   = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
        loss_days     = total_days - profit_days
        total_trades  = sum(r["buy_count"] for r in day_results)
        total_wins    = sum(r["win_count"] for r in day_results)
        total_sells   = sum(r["sell_count"] for r in day_results)
        win_rate      = total_wins / max(total_sells, 1) * 100
        total_return  = (krw / cfg["initial_krw"] - 1) * 100

        # 손절/트레일링/시간 집계
        reasons = {}
        for r in day_results:
            for k, v in r.get("sell_reasons", {}).items():
                k2 = k.split("(")[0]
                reasons[k2] = reasons.get(k2, 0) + v

        results.append({
            "stop_loss_pct":   sl,
            "alloc_ratio":     alloc_ratio,
            "final_krw":       round(krw),
            "total_return_pct": round(total_return, 1),
            "profit_days":     profit_days,
            "loss_days":       loss_days,
            "total_trades":    total_trades,
            "win_rate_pct":    round(win_rate, 1),
            "sell_reasons":    reasons,
        })
        print(f"  손절 {sl:>6.1f}%  →  {total_return:>+9.1f}%  "
              f"(수익일 {profit_days}/{total_days}  승률 {win_rate:.1f}%  "
              f"손절{reasons.get('STOP_LOSS',0)}건 트레일{reasons.get('TRAILING',0)}건)")

    return results

if __name__ == "__main__":
    print("=" * 70)
    print(f"📊 손절 최적화 스윕 — 배분 20:20")
    print("=" * 70)
    alloc = [0.2, 0.2]
    results = run_sweep(STOP_LOSS_RANGE, alloc)

    # 최적값: total_return 기준
    best = max(results, key=lambda x: x["total_return_pct"])

    print()
    print("=" * 70)
    print("🏆 결과 요약")
    print("=" * 70)
    print(f"{'손절':>8}  {'60일수익':>10}  {'수익일':>6}  {'승률':>6}  {'손절건':>6}  {'트레일':>6}")
    print("-" * 70)
    for r in results:
        mark = " ◀ 최적" if r["stop_loss_pct"] == best["stop_loss_pct"] else ""
        print(f"{r['stop_loss_pct']:>7.1f}%  {r['total_return_pct']:>+9.1f}%  "
              f"{r['profit_days']:>3}/{r['profit_days']+r['loss_days']:<3}  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"{r['sell_reasons'].get('STOP_LOSS',0):>6}  "
              f"{r['sell_reasons'].get('TRAILING',0):>6}{mark}")

    print()
    print(f"✅ 최적 손절: {best['stop_loss_pct']}%  →  60일 {best['total_return_pct']:+.1f}%")

    # 저장
    out = SIM_DIR / "sweep_stoploss_20_20_result.json"
    with open(out, "w") as f:
        json.dump({"best": best, "all": results}, f, indent=2, ensure_ascii=False)
    print(f"결과 저장: {out}")
