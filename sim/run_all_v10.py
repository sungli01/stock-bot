#!/usr/bin/env python3
"""
sim/run_all_v10.py — 60일 연속 누적 시뮬 (버그수정 버전 d23e0ae 기준)

규칙:
- 첫날: ₩1,000,000 출발
- 매일 ending_krw → 다음 날 starting_krw (복리)
- compound_cap 2500만 초과 달성 시: 이후 days ₩25,000,000 고정 출발
- 각 날 내부는 복리 캡 2500만 그대로 유지
- stream 파일: sim/stream/YYYY-MM-DD.json
"""
import json, sys
from pathlib import Path
from sim.engine import run_engine, load_config

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
RESULTS_DIR = SIM_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

INITIAL = 1_000_000

def main():
    cfg = load_config()
    CAP = cfg.get("compound_cap_krw", 25_000_000)

    dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))
    print(f"총 {len(dates)}거래일 | compound_cap ₩{CAP:,.0f} | 연속 누적 시뮬")
    print(f"시작: {dates[0]}  종료: {dates[-1]}")
    print(f"파라미터: vol1={cfg['vol_spike_1st_pct']:.0f}% / vol2={cfg['vol_spike_2nd_pct']:.0f}% / vol3={cfg.get('vol_spike_3rd_pct', 300):.0f}%")
    print(f"         trigger1=+{cfg['trigger_1st_pct']:.0f}% / trigger2=+{cfg['trigger_2nd_pct']:.0f}% / trigger3=+{cfg.get('trigger_3rd_pct', 5):.0f}%")
    print(f"         stop={cfg['stop_loss_pct']:.0f}% / trail1=+{cfg['trailing_activate_pct']:.0f}%/-{cfg['trailing_drop_low']:.1f}%p")
    print(f"         trail2=+{cfg.get('trailing_activate_pct_2nd', 8):.0f}%/-{cfg.get('trailing_drop_low_2nd', 1):.1f}%p")
    print(f"         trail3=+{cfg.get('trailing_activate_pct_3rd', 10):.0f}%/-{cfg.get('trailing_drop_low_3rd', 0.5):.1f}%p")
    print(f"         alloc={cfg['allocation_ratio']} / max_single=₩{cfg.get('max_single_buy_krw',50_000_000):,.0f}")
    print("=" * 80)

    portfolio = INITIAL
    cap_hit = False

    all_results = []
    total_buys = 0
    total_wins = 0
    total_losses = 0
    peak_portfolio = INITIAL

    print(f"\n{'날짜':^12} {'시작':>12} {'종료':>12} {'수익률':>8} {'거래':>5} {'승률':>7} {'1차':>5} {'2차':>5} {'3차':>5} {'100%+':>6}")
    print("-" * 80)

    for date in dates:
        result = run_engine(date, portfolio, cfg)

        if "error" in result:
            print(f"  {date}: SKIP ({result['error']})")
            continue

        trades = result.get("trades", [])
        buys = [t for t in trades if t["type"] == "BUY"]
        sells = [t for t in trades if t["type"] == "SELL"]
        wins = [t for t in sells if t.get("pnl_krw", 0) > 0]
        losses = [t for t in sells if t.get("pnl_krw", 0) <= 0]

        by_entry = {"1차": 0, "2차": 0, "3차": 0}
        for b in buys:
            et = b.get("entry_type", "1차")
            by_entry[et] = by_entry.get(et, 0) + 1

        total_buys += len(buys)
        total_wins += len(wins)
        total_losses += len(losses)

        ending = result["ending_krw"]
        pnl_pct = result["day_pnl_pct"]
        win_rate = result["win_rate_pct"]
        caught = f"{result['caught_100plus_count']}/{result['total_100plus_count']}"
        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"

        print(
            f"  {date}  ₩{portfolio:>10,.0f}  ₩{ending:>10,.0f}  "
            f"{pnl_icon}{pnl_pct:>+7.1f}%  {len(buys):>4}건  {win_rate:>6.1f}%  "
            f"{by_entry['1차']:>4}  {by_entry['2차']:>4}  {by_entry['3차']:>4}  {caught:>5}"
        )

        # 결과 저장
        out = RESULTS_DIR / f"{date}.json"
        with open(out, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        all_results.append({
            "date": date,
            "starting_krw": portfolio,
            "ending_krw": ending,
            "day_pnl_pct": pnl_pct,
            "win_rate": win_rate,
            "buy_count": len(buys),
            "win_count": len(wins),
            "loss_count": len(losses),
            "entry_1st": by_entry["1차"],
            "entry_2nd": by_entry["2차"],
            "entry_3rd": by_entry["3차"],
            "caught_100plus": caught,
            "pf": result["profit_factor"],
        })

        # 복리 캡 적용
        if ending >= CAP and not cap_hit:
            cap_hit = True
            print(f"  *** 🎯 2500만 달성! ({date}) — 이후 고정 출발 ***")
        portfolio = min(ending, CAP)
        peak_portfolio = max(peak_portfolio, ending)

    # ── 최종 요약 ──
    print("\n" + "=" * 80)
    total_sell = total_wins + total_losses
    overall_wr = total_wins / max(total_sell, 1) * 100

    # avg win/loss
    all_wins_pnl, all_loss_pnl = [], []
    for r in all_results:
        date = r["date"]
        res_path = RESULTS_DIR / f"{date}.json"
        with open(res_path) as f:
            full = json.load(f)
        for t in full.get("trades", []):
            if t["type"] == "SELL":
                if t.get("pnl_pct", 0) > 0:
                    all_wins_pnl.append(t["pnl_pct"])
                else:
                    all_loss_pnl.append(t["pnl_pct"])

    avg_win = sum(all_wins_pnl) / len(all_wins_pnl) if all_wins_pnl else 0
    avg_loss = sum(all_loss_pnl) / len(all_loss_pnl) if all_loss_pnl else 0
    pf_all = sum(all_wins_pnl) / max(abs(sum(all_loss_pnl)), 0.001)

    # 차수별 통계
    e1 = sum(r["entry_1st"] for r in all_results)
    e2 = sum(r["entry_2nd"] for r in all_results)
    e3 = sum(r["entry_3rd"] for r in all_results)

    # 수익일 카운트
    profit_days = sum(1 for r in all_results if r["day_pnl_pct"] > 0)

    final_portfolio = all_results[-1]["ending_krw"] if all_results else INITIAL
    total_return = (final_portfolio / INITIAL - 1) * 100

    print(f"\n📊 60일 연속 누적 시뮬 최종 결과 (v10.3 버그수정 후)")
    print(f"  초기: ₩{INITIAL:,}  →  최종: ₩{final_portfolio:,}  ({total_return:+,.1f}%)")
    print(f"  수익일: {profit_days}/{len(all_results)}일")
    print(f"  총 거래: {total_buys}건 (1차:{e1} / 2차:{e2} / 3차:{e3})")
    print(f"  전체 승률: {overall_wr:.1f}% ({total_wins}승 {total_losses}패)")
    print(f"  평균 수익: +{avg_win:.1f}% | 평균 손실: {avg_loss:.1f}%")
    print(f"  PF (60일 통합): {pf_all:.2f}")
    print(f"  Peak: ₩{peak_portfolio:,}")
    cap_status = "✅ 도달" if cap_hit else "❌ 미달성"
    print(f"  2500만 캡: {cap_status}")

    # 요약 저장
    summary = {
        "version": "v10.3-bugfix-d23e0ae",
        "dates": f"{all_results[0]['date']} ~ {all_results[-1]['date']}",
        "initial_krw": INITIAL,
        "final_krw": final_portfolio,
        "total_return_pct": round(total_return, 2),
        "profit_days": profit_days,
        "total_days": len(all_results),
        "total_trades": total_buys,
        "entry_1st": e1,
        "entry_2nd": e2,
        "entry_3rd": e3,
        "overall_win_rate": round(overall_wr, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(pf_all, 2),
        "peak_krw": peak_portfolio,
        "cap_hit": cap_hit,
        "daily": all_results,
    }
    out_path = SIM_DIR / "run_all_v10_summary.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n  저장: {out_path}")

if __name__ == "__main__":
    main()
