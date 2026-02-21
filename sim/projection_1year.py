#!/usr/bin/env python3
"""
1년 투자 프로젝션 시뮬레이터

설정:
  - 초기 투자: ₩10,000,000
  - 1차 복리 캡: ₩25,000,000 (초과 시 ₩25M 고정 출발)
  - 2·3차 1회 상한: ₩50,000,000
  - 손절: -15% / 보유: 90분 (v10.4)
  - 미국 주식시장 1년 거래일: ~252일
  - 보유 60일 데이터를 순환해서 252일 커버
"""
import json
from pathlib import Path
from datetime import date, timedelta
from sim.engine import run_engine, load_config

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"

INITIAL_KRW      = 10_000_000   # 초기 투자 ₩1000만
COMPOUND_CAP     = 25_000_000   # 1차 복리 캡 ₩2500만
TRADING_DAYS_1Y  = 252          # 미국 주식시장 1년 거래일

def main():
    cfg = load_config()
    cfg["compound_cap_krw"]    = COMPOUND_CAP
    cfg["max_single_buy_krw"]  = 50_000_000   # 2·3차 상한 ₩5000만
    cfg["stop_loss_pct"]       = -15.0
    cfg["max_hold_min"]        = 90

    # 60일 스트림을 순환해서 252일 생성
    source_dates = sorted(p.stem for p in STREAM_DIR.glob("*.json"))
    # 252일 커버하도록 반복
    sim_dates = []
    for i in range(TRADING_DAYS_1Y):
        sim_dates.append(source_dates[i % len(source_dates)])

    portfolio = INITIAL_KRW
    cap_hit = False
    cap_hit_day = None

    # 월별 추적 (20거래일 ≈ 1개월)
    monthly_snapshots = []
    MONTH_DAYS = 21  # 약 1개월 거래일

    all_buys, all_wins_pnl, all_losses_pnl = 0, [], []
    profit_days = 0
    peak_portfolio = INITIAL_KRW
    stop_count = 0
    time_count = 0

    print("=" * 75)
    print(f"  📈 1년 투자 프로젝션 — v10.4 엔진")
    print(f"  초기: ₩{INITIAL_KRW:,.0f}  |  1차 캡: ₩{COMPOUND_CAP:,.0f}  |  2·3차 상한: ₩50,000,000")
    print(f"  손절: -15%  |  보유: 90분  |  총 {TRADING_DAYS_1Y}거래일 (60일 데이터 순환)")
    print("=" * 75)
    print(f"\n  {'거래일':>5} {'월':>4} | {'포트폴리오':>14} {'수익률':>9} {'일간':>8} {'누적승률':>8}")
    print("  " + "-" * 62)

    prev_monthly = INITIAL_KRW
    month_num = 0

    for day_idx, date_str in enumerate(sim_dates, 1):
        r = run_engine(date_str, portfolio, cfg)
        if "error" in r:
            continue

        trades = r.get("trades", [])
        ending = r["ending_krw"]

        for t in trades:
            if t["type"] == "BUY":
                all_buys += 1
            elif t["type"] == "SELL":
                reason = t.get("reason", "")
                if "STOP" in reason: stop_count += 1
                if "TIME" in reason: time_count += 1
                if t.get("pnl_pct", 0) > 0:
                    all_wins_pnl.append(t["pnl_pct"])
                else:
                    all_losses_pnl.append(t["pnl_pct"])

        if r["day_pnl_pct"] > 0:
            profit_days += 1
        peak_portfolio = max(peak_portfolio, ending)

        if ending >= COMPOUND_CAP and not cap_hit:
            cap_hit = True
            cap_hit_day = day_idx
            print(f"  {'▶':>5} {'':>4}   🎯 ₩2500만 달성! ({day_idx}거래일째)")

        # 복리 캡 적용
        portfolio = min(ending, COMPOUND_CAP)

        # 월별 스냅샷 (21거래일마다)
        if day_idx % MONTH_DAYS == 0:
            month_num += 1
            monthly_ret = (portfolio / INITIAL_KRW - 1) * 100
            wins_so_far = len(all_wins_pnl)
            losses_so_far = len(all_losses_pnl)
            wr = wins_so_far / max(wins_so_far + losses_so_far, 1) * 100
            monthly_gain = (portfolio / prev_monthly - 1) * 100
            monthly_snapshots.append({
                "month": month_num, "day": day_idx, "portfolio": portfolio,
                "total_ret": monthly_ret, "monthly_gain": monthly_gain, "win_rate": wr
            })
            print(f"  {day_idx:>5}일 {month_num:>2}개월 | ₩{portfolio:>12,.0f} {monthly_ret:>+8.1f}% {monthly_gain:>+7.1f}% {wr:>7.1f}%")
            prev_monthly = portfolio

    # 최종 결과
    final = portfolio
    total_ret = (final / INITIAL_KRW - 1) * 100
    total_wins = len(all_wins_pnl)
    total_losses = len(all_losses_pnl)
    wr_total = total_wins / max(total_wins + total_losses, 1) * 100
    avg_w = sum(all_wins_pnl) / len(all_wins_pnl) if all_wins_pnl else 0
    avg_l = sum(all_losses_pnl) / len(all_losses_pnl) if all_losses_pnl else 0
    pf = sum(all_wins_pnl) / max(abs(sum(all_losses_pnl)), 0.001)
    profit_krw = final - INITIAL_KRW

    print("\n" + "=" * 75)
    print(f"  📊 1년 후 결과 요약 (2026-02-21 → 2027-02-21)")
    print("=" * 75)
    print(f"  초기 투자       : ₩{INITIAL_KRW:>15,.0f}")
    print(f"  최종 포트폴리오 : ₩{final:>15,.0f}")
    print(f"  순수익          : ₩{profit_krw:>15,.0f}  ({total_ret:+.1f}%)")
    print(f"  최고점 (Peak)   : ₩{peak_portfolio:>15,.0f}")
    cap_str = f"✅ {cap_hit_day}거래일째 ({cap_hit_day//MONTH_DAYS:.1f}개월)" if cap_hit else "❌ 미달성"
    print(f"  ₩2500만 도달    : {cap_str}")
    print()
    print(f"  총 거래일       : {TRADING_DAYS_1Y}거래일 | 수익일 {profit_days}일 ({profit_days/TRADING_DAYS_1Y*100:.0f}%)")
    print(f"  총 매수         : {all_buys}건")
    print(f"  전체 승률       : {wr_total:.1f}%  ({total_wins}승 {total_losses}패)")
    print(f"  평균 수익       : +{avg_w:.1f}%  |  평균 손실 : {avg_l:.1f}%")
    print(f"  PF              : {pf:.2f}")
    print(f"  손절 발생       : {stop_count}건  |  시간초과 : {time_count}건")
    print()
    print(f"  ✅ ₩1,000만 투자 → 1년 후 ₩{final:,.0f} (수익 ₩{profit_krw:,.0f})")

    # 월별 정리
    print("\n  📅 월별 포트폴리오 현황")
    print(f"  {'월':>4} | {'포트폴리오':>14} {'누적수익률':>10} {'월간':>8}")
    print("  " + "-" * 45)
    prev = INITIAL_KRW
    for snap in monthly_snapshots:
        monthly_delta = (snap['portfolio'] / prev - 1) * 100
        print(f"  {snap['month']:>3}월 | ₩{snap['portfolio']:>12,.0f} {snap['total_ret']:>+9.1f}% {monthly_delta:>+7.1f}%")
        prev = snap['portfolio']

    # 결과 저장
    result = {
        "version": "v10.4",
        "initial_krw": INITIAL_KRW,
        "compound_cap": COMPOUND_CAP,
        "max_single_buy_2nd_3rd": 50_000_000,
        "trading_days": TRADING_DAYS_1Y,
        "final_krw": final,
        "profit_krw": profit_krw,
        "total_return_pct": round(total_ret, 2),
        "peak_krw": peak_portfolio,
        "profit_days": profit_days,
        "total_buys": all_buys,
        "win_rate": round(wr_total, 1),
        "profit_factor": round(pf, 2),
        "cap_hit": cap_hit,
        "cap_hit_day": cap_hit_day,
        "monthly": monthly_snapshots,
    }
    out = SIM_DIR / "projection_1year_result.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n  저장: {out}")

if __name__ == "__main__":
    main()
