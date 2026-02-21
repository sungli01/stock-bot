#!/usr/bin/env python3
"""
sim/sweep_final.py — 최적 조합 기반 손절 스윕 → 배분 최적화

Phase 1: 손절 스윕 (고정: vol 800%, trigger +10%, trailing +6%/-2%p, 배분 20%/20%)
Phase 2: 최적 손절 확정 후 배분 스윕
"""
import json, sys, itertools
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
sys.path.insert(0, str(Path(__file__).parent.parent))
from sim.engine import run_engine, load_config

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

    wins_pnl, losses_pnl = [], []
    for r in day_results:
        for t in r.get("trades", []):
            if t["type"] == "SELL":
                p = t.get("pnl_pct", 0)
                (wins_pnl if p > 0 else losses_pnl).append(p)

    total_sells = sum(r["sell_count"] for r in day_results)
    total_wins  = sum(r["win_count"]  for r in day_results)
    profit_days = sum(1 for r in day_results if r["day_pnl_krw"] > 0)
    total_days  = len(day_results)
    win_rate    = total_wins / max(total_sells, 1) * 100
    total_return= (krw / cfg["initial_krw"] - 1) * 100
    avg_win     = sum(wins_pnl)  / max(len(wins_pnl), 1)
    avg_loss    = sum(losses_pnl)/ max(len(losses_pnl), 1)
    rr          = abs(avg_win / avg_loss) if avg_loss else 0
    be_wr       = abs(avg_loss) / (avg_win + abs(avg_loss)) * 100 if avg_win else 100

    return dict(
        final_krw=round(krw),
        total_return_pct=round(total_return, 1),
        profit_days=profit_days,
        loss_days=total_days - profit_days,
        total_trades=sum(r["buy_count"] for r in day_results),
        win_rate_pct=round(win_rate, 1),
        avg_win_pct=round(avg_win, 1),
        avg_loss_pct=round(avg_loss, 1),
        rr_ratio=round(rr, 2),
        be_winrate=round(be_wr, 1),
        **cfg_override,
    )

if __name__ == "__main__":
    dates    = sorted(p.stem for p in STREAM_DIR.glob("*.json"))
    base_cfg = load_config()
    # 최적 고정값
    base_cfg.update({
        "vol_spike_1st_pct":     800.0,
        "trigger_1st_pct":       10.0,
        "trailing_activate_pct": 6.0,
        "trailing_drop_low":     2.0,
        "allocation_ratio":      [0.2, 0.2],
        "compound_cap_krw":      25_000_000,
    })

    # ── Phase 1: 손절 스윕 ──────────────────────────
    STOP_LOSS = [-5.0, -7.0, -10.0, -12.0, -15.0, -18.0, -20.0, -25.0, -30.0]
    print("=" * 75)
    print("📊 Phase 1 — 손절 스윕 (vol 800% / 트리거 +10% / 배분 20:20)")
    print("=" * 75)
    print(f"{'손절':>7}  {'60일수익':>10}  {'수익일':>7}  {'거래':>5}  {'승률':>6}  {'평균수익':>7}  {'R:R':>5}  {'격차':>7}")
    print("-" * 75)

    p1_results = []
    for sl in STOP_LOSS:
        r = run_combo({"stop_loss_pct": sl}, dates, base_cfg)
        r["stop_loss_pct"] = sl
        p1_results.append(r)
        gap = r["win_rate_pct"] - r["be_winrate"]
        print(f"{sl:>6.1f}%  {r['total_return_pct']:>+9.1f}%  "
              f"{r['profit_days']:>2}/{r['profit_days']+r['loss_days']:<4}  "
              f"{r['total_trades']:>5}건  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"+{r['avg_win_pct']:>5.1f}%  "
              f"{r['rr_ratio']:>5.2f}  "
              f"{gap:>+6.1f}%p")

    best_sl = max(p1_results, key=lambda x: x["total_return_pct"])
    print(f"\n✅ 최적 손절: {best_sl['stop_loss_pct']}%  →  {best_sl['total_return_pct']:+.1f}%  (₩{best_sl['final_krw']:,})")

    # ── Phase 2: 배분 스윕 ──────────────────────────
    ALLOCS = [
        [0.10, 0.10], [0.15, 0.15], [0.20, 0.20],
        [0.25, 0.25], [0.30, 0.30], [0.40, 0.40],
        [0.50, 0.50], [0.70, 0.30],
    ]
    optimal_sl = best_sl["stop_loss_pct"]
    base_cfg["stop_loss_pct"] = optimal_sl

    print()
    print("=" * 75)
    print(f"📊 Phase 2 — 배분 스윕 (손절 {optimal_sl}% 고정)")
    print("=" * 75)
    print(f"{'배분':>8}  {'60일수익':>10}  {'최종금액':>12}  {'수익일':>7}  {'승률':>6}  {'R:R':>5}")
    print("-" * 75)

    p2_results = []
    for alloc in ALLOCS:
        r = run_combo({"allocation_ratio": alloc}, dates, base_cfg)
        r["allocation_ratio"] = alloc
        p2_results.append(r)
        print(f"{int(alloc[0]*100):>3}%/{int(alloc[1]*100):<3}%  "
              f"{r['total_return_pct']:>+9.1f}%  "
              f"₩{r['final_krw']:>11,}  "
              f"{r['profit_days']:>2}/{r['profit_days']+r['loss_days']:<4}  "
              f"{r['win_rate_pct']:>5.1f}%  "
              f"{r['rr_ratio']:>5.2f}")

    best_alloc = max(p2_results, key=lambda x: x["total_return_pct"])
    print(f"\n✅ 최적 배분: {int(best_alloc['allocation_ratio'][0]*100)}%/{int(best_alloc['allocation_ratio'][1]*100)}%  →  "
          f"{best_alloc['total_return_pct']:+.1f}%  (₩{best_alloc['final_krw']:,})")

    # 저장
    out = SIM_DIR / "sweep_final_result.json"
    with open(out, "w") as f:
        json.dump({"phase1_best": best_sl, "phase2_best": best_alloc,
                   "phase1": p1_results, "phase2": p2_results}, f, indent=2, ensure_ascii=False)
    print(f"\n전체 결과 저장: {out}")

    print()
    print("=" * 75)
    print("🏆 최종 최적 파라미터")
    print("=" * 75)
    print(f"  vol spike 1차:   800%")
    print(f"  진입 트리거:     +10%")
    print(f"  트레일링 활성:   +6%")
    print(f"  트레일링 낙폭:   -2%p")
    print(f"  손절:            {optimal_sl}%")
    print(f"  배분:            {int(best_alloc['allocation_ratio'][0]*100)}%/{int(best_alloc['allocation_ratio'][1]*100)}%")
    print(f"  60일 수익률:     {best_alloc['total_return_pct']:+.1f}%")
    print(f"  최종 금액:       ₩{best_alloc['final_krw']:,}")
