#!/usr/bin/env python3
"""
sim/run_all.py — 전체 60일 연속 누적 시뮬레이션

규칙:
- 첫날: ₩1,000,000 출발
- 매일 ending_krw → 다음 날 starting_krw
- 2500만 초과 달성 시: 다음 날부터 ₩25,000,000 고정 출발
- 각 날 내부는 복리 캡 2500만 그대로 유지
"""
import json, subprocess, sys
from pathlib import Path

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
RESULTS_DIR = SIM_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

CAP = 25_000_000
INITIAL = 1_000_000

# 전체 거래일 목록
DATES = sorted([
    p.stem.replace("_", "-").split("m")[0][:-1]   # 파일명에서 날짜 추출
    for p in Path("data/bars_cache").glob("*_1m.json")
], key=lambda x: "".join(p.stem for p in [Path(x)])
)

# 날짜 목록 재추출 (정확하게)
import re
DATES = sorted(set(
    re.search(r"(\d{4}-\d{2}-\d{2})", p.name).group(1)
    for p in Path("data/bars_cache").glob("*_1m.json")
))

print(f"총 {len(DATES)}거래일 | 2500만 캡 연속 누적 시뮬")
print(f"시작: {DATES[0]}  종료: {DATES[-1]}")
print("=" * 60)

portfolio = INITIAL
cap_reached = False
results_summary = []

for date in DATES:
    # 1. feeder 실행 (스트림 없으면 생성)
    stream_path = STREAM_DIR / f"{date}.json"
    if not stream_path.exists():
        subprocess.run(
            ["python3", "sim/feeder.py", date],
            capture_output=True, cwd=Path(__file__).parent.parent
        )

    # 2. engine 실행
    proc = subprocess.run(
        ["python3", "sim/engine.py", date, str(portfolio)],
        capture_output=True, text=True,
        cwd=Path(__file__).parent.parent
    )

    # 3. 결과 로드
    result_path = RESULTS_DIR / f"{date}.json"
    if not result_path.exists():
        print(f"[{date}] ❌ 결과 없음 — 스킵")
        continue

    with open(result_path) as f:
        r = json.load(f)

    ending = r["ending_krw"]
    pnl_pct = (ending / portfolio - 1) * 100
    win_rate = r["win_rate_pct"]
    pf = r["profit_factor"]
    trades = r["buy_count"]
    caught = f"{r['caught_100plus_count']}/{r['total_100plus_count']}"
    stop_cnt = r.get("sell_reasons", {}).get("STOP_LOSS", 0)

    # 2500만 달성 여부 체크
    cap_hit = ""
    if not cap_reached and ending >= CAP:
        cap_reached = True
        cap_hit = " 🏁 2500만 달성!"

    print(f"{date}  시작 ₩{portfolio:>12,.0f}  →  ₩{ending:>14,.0f}  "
          f"({pnl_pct:>+8.1f}%)  "
          f"거래{trades:>2}건  승률{win_rate:>5.1f}%  PF{pf:>6.2f}  "
          f"손절{stop_cnt}  100+:{caught}{cap_hit}")

    results_summary.append({
        "date": date,
        "starting_krw": portfolio,
        "ending_krw": ending,
        "pnl_pct": round(pnl_pct, 2),
        "win_rate": win_rate,
        "pf": pf,
        "trades": trades,
        "stop_loss_cnt": stop_cnt,
        "caught_100plus": caught,
        "cap_reached": cap_reached,
    })

    # 다음 날 시작 포트 결정
    if cap_reached:
        portfolio = CAP           # 2500만 고정
    else:
        portfolio = ending        # 복리 유지

# 최종 요약
print("=" * 60)
print(f"\n📊 최종 결과 요약")
print(f"{'='*60}")
total_days = len(results_summary)
win_days   = sum(1 for r in results_summary if r["pnl_pct"] > 0)
loss_days  = sum(1 for r in results_summary if r["pnl_pct"] <= 0)
final_port = results_summary[-1]["ending_krw"] if results_summary else INITIAL

all_wins  = sum(r["win_rate"] * r["trades"] / 100 for r in results_summary)
all_trades= sum(r["trades"] for r in results_summary)
avg_win_rate = all_wins / all_trades * 100 if all_trades > 0 else 0

print(f"총 거래일:   {total_days}일")
print(f"수익일:      {win_days}일  /  손실일: {loss_days}일")
print(f"총 거래:     {all_trades}건")
print(f"평균 승률:   {avg_win_rate:.1f}%")
print(f"최종 포트:   ₩{final_port:,.0f}")
print(f"총 수익률:   {(final_port/INITIAL - 1)*100:+.1f}% (₩100만 기준)")

cap_day = next((r["date"] for r in results_summary if r["cap_reached"]), None)
if cap_day:
    print(f"2500만 달성: {cap_day}")

# 결과 저장
summary_path = SIM_DIR / "run_all_summary.json"
with open(summary_path, "w") as f:
    json.dump({
        "initial_krw": INITIAL,
        "cap_krw": CAP,
        "final_krw": final_port,
        "total_return_pct": round((final_port/INITIAL - 1)*100, 2),
        "total_days": total_days,
        "win_days": win_days,
        "loss_days": loss_days,
        "avg_win_rate": round(avg_win_rate, 1),
        "cap_reached_date": cap_day,
        "daily": results_summary,
    }, f, indent=2, ensure_ascii=False)

print(f"\n결과 저장: {summary_path}")
