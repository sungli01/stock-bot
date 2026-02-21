#!/usr/bin/env python3
"""
sim/engine.py — v9 엔진 시뮬레이터 (서브에이전트 2)

사용법: python3 sim/engine.py 2025-11-19
입력:   sim/stream/YYYY-MM-DD.json
출력:   sim/results/YYYY-MM-DD.json

v9 알고리즘:
  1차 진입: vol spike 1000%+ → 큐 등록 → +20% 트리거 → 매수
  2차 진입: 1차 청산 후 vol spike 200%+ → 큐 등록 → +15% 트리거 → 풀매수
  손절:     sim_config.json의 stop_loss_pct (기본 -15%)
  트레일링: +8% 활성화, 구간별 폭
  시간제한: 120분
  큐 만료:  30분 (매 봉 체크)
  상단제한: 큐 대비 +40% 초과 차단
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

SIM_DIR = Path(__file__).parent
STREAM_DIR = SIM_DIR / "stream"
RESULTS_DIR = SIM_DIR / "results"
CONFIG_PATH = SIM_DIR / "sim_config.json"
DAILY_LOG = SIM_DIR / "daily_log.json"
RESULTS_DIR.mkdir(exist_ok=True)

# ── 설정 로드 ──────────────────────────────────────
def load_config() -> dict:
    default = {
        "initial_krw": 1_000_000,
        "stop_loss_pct": -15.0,          # 수정 가능
        "trailing_activate_pct": 8.0,
        "partial_sell_pct": 5.0,
        "vol_spike_1st_pct": 1000.0,     # 1차 threshold
        "vol_spike_2nd_pct": 200.0,      # 2차 threshold
        "trigger_1st_pct": 20.0,         # 1차 트리거
        "trigger_2nd_pct": 15.0,         # 2차 트리거
        "max_pct_from_queue": 40.0,      # 상단 제한
        "queue_expire_min": 30,          # 큐 만료
        "max_hold_min": 120,             # 최대 보유
        "max_positions": 2,
        "allocation_ratio": [0.7, 0.3],  # 1차 배분
        "usd_krw_rate": 1450.0,
        "compound_mode": True,
        "candidate_change_pct": 5.0,     # 후보 하단
        "candidate_max_change_pct": 20.0, # 후보 상단
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        default.update(user)
    return default


# ── 3분봉 계산 ───────────────────────────────────
def compute_3min_vol(bar_buffer: list) -> tuple[float, float]:
    """
    최근 6봉으로 완성된 3분봉 2개 계산
    bar_buffer[-6:-3] = 직전 3분봉 (N-2)
    bar_buffer[-3:]   = 현재 3분봉 (N-1)
    Returns: (현재봉 vol, 직전봉 vol)
    """
    if len(bar_buffer) < 6:
        return 0.0, 0.0
    cur_3 = bar_buffer[-3:]
    prev_3 = bar_buffer[-6:-3]
    cur_vol = sum(b["v"] for b in cur_3)
    prev_vol = sum(b["v"] for b in prev_3)
    return float(cur_vol), float(prev_vol)


# ── 트레일링 폭 계산 ─────────────────────────────
def get_trailing_drop(peak_pct: float, elapsed_min: float) -> float:
    if peak_pct >= 80:
        base = 30.0
    elif peak_pct >= 50:
        base = 8.0
    elif peak_pct >= 15:
        base = 5.0
    else:
        base = 3.0   # +8~15%: -3%p (초기 급등, 타이트)
    if elapsed_min >= 30:
        base *= 0.8
    return base


# ── 포지션 클래스 ────────────────────────────────
class Position:
    def __init__(self, ticker, entry_price, buy_krw, entry_time_ms, queue_price, is_second):
        self.ticker = ticker
        self.entry_price = entry_price
        self.buy_krw = buy_krw
        self.entry_time_ms = entry_time_ms
        self.queue_price = queue_price
        self.is_second = is_second

        self.peak_price = entry_price
        self.trailing_active = False
        self.partial_done = False

    def elapsed_min(self, current_time_ms):
        return (current_time_ms - self.entry_time_ms) / 60000

    def pnl_pct(self, current_price):
        return (current_price / self.entry_price - 1) * 100

    def pnl_krw(self, current_price):
        return self.buy_krw * (current_price / self.entry_price - 1)

    def check_exit(self, current_price, current_time_ms, cfg) -> tuple[bool, str]:
        pnl = self.pnl_pct(current_price)
        elapsed = self.elapsed_min(current_time_ms)

        # 고점 갱신
        if current_price > self.peak_price:
            self.peak_price = current_price

        # 1. 손절
        if pnl <= cfg["stop_loss_pct"]:
            return True, f"STOP_LOSS({pnl:.1f}%)"

        # 2. 시간제한
        if elapsed >= cfg["max_hold_min"]:
            return True, f"TIME_LIMIT({pnl:.1f}%,{elapsed:.0f}분)"

        # 3. 트레일링 활성화
        peak_pnl = self.pnl_pct(self.peak_price)
        if peak_pnl >= cfg["trailing_activate_pct"]:
            self.trailing_active = True

        if self.trailing_active:
            drop_width = get_trailing_drop(peak_pnl, elapsed)
            drop_from_peak = peak_pnl - pnl
            if drop_from_peak >= drop_width:
                return True, f"TRAILING(peak+{peak_pnl:.1f}%→+{pnl:.1f}%)"

        return False, ""


# ── 메인 엔진 ────────────────────────────────────
def run_engine(date_str: str, portfolio_krw: float, cfg: dict) -> dict:
    stream_path = STREAM_DIR / f"{date_str}.json"
    if not stream_path.exists():
        return {"error": f"스트림 파일 없음: {stream_path}"}

    with open(stream_path) as f:
        stream_data = json.load(f)

    events = stream_data.get("stream", [])
    if not events:
        return {"error": "스트림 비어있음"}

    # ── 상태 초기화 ──
    queue = {}          # ticker → {price, time_ms, is_second}
    positions = {}      # ticker → Position
    trades = []
    bar_buffers = defaultdict(list)   # ticker → 최근 봉 버퍼 (6개)

    # 거래 이력
    traded_once = set()     # 1차 완료
    traded_twice = set()    # 2차 완료 (완전 차단)

    running_krw = portfolio_krw
    total_vol_spikes = 0
    fake_signals = []       # vol spike 후 이후 미상승

    # 전체 종목별 최고가 (페이크 판단용)
    ticker_max_price = {}

    # ── 이벤트 루프 ──
    for event in events:
        ticker = event["ticker"]
        ts = event["time_ms"]
        cur_price = event["c"]
        daily_open = event["daily_open"]
        daily_vol = event["daily_volume_so_far"]

        if cur_price <= 0:
            continue

        # 최고가 업데이트
        if ticker not in ticker_max_price or cur_price > ticker_max_price[ticker]:
            ticker_max_price[ticker] = cur_price

        # ── 큐 만료 처리 (매 봉 체크) ──
        expire_ms = cfg["queue_expire_min"] * 60 * 1000
        expired = [t for t, q in queue.items() if ts - q["time_ms"] > expire_ms]
        for t in expired:
            del queue[t]

        # ── 봉 버퍼 업데이트 ──
        bar_buffers[ticker].append(event)
        if len(bar_buffers[ticker]) > 6:
            bar_buffers[ticker] = bar_buffers[ticker][-6:]

        # ── 3분봉 vol spike 계산 ──
        cur_vol, prev_vol = compute_3min_vol(bar_buffers[ticker])
        if cur_vol > 0 and prev_vol > 0:
            vol_ratio = (cur_vol / prev_vol) * 100

            is_second = ticker in traded_once and ticker not in traded_twice

            # 후보 범위 체크 (1차만, 2차는 무제한)
            change_from_open = (cur_price / daily_open - 1) * 100 if not is_second else 999

            if not is_second:
                is_candidate = (cfg["candidate_change_pct"] <= change_from_open < cfg["candidate_max_change_pct"])
            else:
                is_candidate = True  # 2차는 범위 무제한

            # vol spike 감지 → 큐 등록
            threshold = cfg["vol_spike_2nd_pct"] if is_second else cfg["vol_spike_1st_pct"]
            if vol_ratio >= threshold and is_candidate and ticker not in queue and ticker not in traded_twice:
                queue[ticker] = {
                    "price": cur_price,
                    "time_ms": ts,
                    "is_second": is_second,
                    "vol_ratio": vol_ratio,
                    "vol_at_queue": daily_vol,   # ★ 큐 등록 시점 누적 거래량
                }
                total_vol_spikes += 1

        # ── 큐 → 매수 트리거 체크 ──
        if ticker in queue and ticker not in positions:
            q = queue[ticker]
            is_second = q["is_second"]
            q_price = q["price"]
            pct_from_q = (cur_price / q_price - 1) * 100

            # 상단 제한: +40% 초과 차단
            if pct_from_q > cfg["max_pct_from_queue"]:
                del queue[ticker]
                continue

            # 트리거 체크
            trigger = cfg["trigger_2nd_pct"] if is_second else cfg["trigger_1st_pct"]

            if pct_from_q >= trigger:
                # 일 거래량 체크 (1차만)
                if not is_second:
                    req_vol = 50000 if cur_price >= 10 else 300000
                    if daily_vol < req_vol:
                        continue

                # 포지션 수 체크
                if len(positions) >= cfg["max_positions"]:
                    continue

                # ── 매수금액 계산 ──────────────────────────
                # [v9] 1차: 큐 등록 ~ 매수 시점 구간 거래량의 30% 이내
                usd_krw = cfg.get("usd_krw_rate", 1450.0)

                vol_at_queue = q.get("vol_at_queue", 0)
                vol_since_queue = max(daily_vol - vol_at_queue, 1)  # 구간 거래량
                max_shares_by_vol = vol_since_queue * 0.30          # 30% 캡
                max_krw_by_vol = max_shares_by_vol * cur_price * usd_krw  # 주수→KRW

                # 복리 cap 적용 (2500만 이하: 복리, 초과: 고정)
                cap = cfg.get("compound_cap_krw", 25_000_000)
                base_krw = min(running_krw, cap)

                pos_idx = len(positions)
                if is_second:
                    buy_krw = base_krw  # 2차: 풀 매수 (거래량 캡 없음)
                else:
                    alloc = cfg["allocation_ratio"]
                    alloc_pct = alloc[pos_idx] if pos_idx < len(alloc) else alloc[-1]
                    portfolio_krw = base_krw * alloc_pct
                    # 1차: 포트 기준 vs 거래량 30% 중 작은 값
                    buy_krw = min(portfolio_krw, max_krw_by_vol)

                buy_krw = min(buy_krw, running_krw)
                if buy_krw <= 0:
                    continue

                pos = Position(
                    ticker=ticker,
                    entry_price=cur_price,
                    buy_krw=buy_krw,
                    entry_time_ms=ts,
                    queue_price=q_price,
                    is_second=is_second,
                )
                positions[ticker] = pos
                del queue[ticker]

                entry_type = "2차" if is_second else "1차"
                trades.append({
                    "type": "BUY",
                    "entry_type": entry_type,
                    "ticker": ticker,
                    "price": cur_price,
                    "buy_krw": round(buy_krw),
                    "queue_price": q_price,
                    "pct_from_queue": round(pct_from_q, 1),
                    "vol_ratio": round(q.get("vol_ratio", 0), 0),
                    "vol_since_queue": int(vol_since_queue) if not is_second else None,
                    "max_krw_by_vol": round(max_krw_by_vol) if not is_second else None,
                    "vol_cap_applied": (not is_second and max_krw_by_vol < (base_krw * cfg["allocation_ratio"][pos_idx] if pos_idx < len(cfg["allocation_ratio"]) else base_krw)),
                    "time_kst": event["time_kst"],
                    "daily_vol": daily_vol,
                })

        # ── 포지션 매도 체크 ──
        if ticker in positions:
            pos = positions[ticker]
            should_sell, reason = pos.check_exit(cur_price, ts, cfg)

            if should_sell:
                pnl_k = pos.pnl_krw(cur_price)
                running_krw += pos.buy_krw + pnl_k

                # 1차/2차 완료 처리
                if pos.is_second:
                    traded_twice.add(ticker)
                    if ticker in queue:   # 2차 완료 → 큐 즉시 제거
                        del queue[ticker]
                else:
                    traded_once.add(ticker)

                trades.append({
                    "type": "SELL",
                    "entry_type": "2차" if pos.is_second else "1차",
                    "ticker": ticker,
                    "entry_price": pos.entry_price,
                    "sell_price": cur_price,
                    "pnl_pct": round(pos.pnl_pct(cur_price), 2),
                    "pnl_krw": round(pnl_k),
                    "buy_krw": round(pos.buy_krw),
                    "reason": reason,
                    "hold_min": round(pos.elapsed_min(ts), 1),
                    "time_kst": event["time_kst"],
                    "peak_price": pos.peak_price,
                    "max_possible_pct": round((ticker_max_price.get(ticker, cur_price) / pos.entry_price - 1) * 100, 1),
                })
                del positions[ticker]

    # ── 미청산 강제 종료 ──
    for ticker, pos in positions.items():
        last_price = bar_buffers[ticker][-1]["c"] if bar_buffers[ticker] else pos.entry_price
        pnl_k = pos.pnl_krw(last_price)
        running_krw += pos.buy_krw + pnl_k
        trades.append({
            "type": "SELL",
            "entry_type": "2차" if pos.is_second else "1차",
            "ticker": ticker,
            "entry_price": pos.entry_price,
            "sell_price": last_price,
            "pnl_pct": round(pos.pnl_pct(last_price), 2),
            "pnl_krw": round(pnl_k),
            "reason": "FORCE_CLOSE_EOD",
            "hold_min": 999,
            "time_kst": "EOD",
        })

    # ── 결과 집계 ──
    buy_trades = [t for t in trades if t["type"] == "BUY"]
    sell_trades = [t for t in trades if t["type"] == "SELL"]
    wins = [t for t in sell_trades if t.get("pnl_krw", 0) > 0]
    losses = [t for t in sell_trades if t.get("pnl_krw", 0) <= 0]

    win_sum = sum(t["pnl_krw"] for t in wins)
    loss_sum = abs(sum(t["pnl_krw"] for t in losses))
    pf = win_sum / max(loss_sum, 1)

    # 매도 이유 집계
    sell_reasons = {}
    for t in sell_trades:
        key = t["reason"].split("(")[0]
        sell_reasons[key] = sell_reasons.get(key, 0) + 1

    # 100%+ 종목 포착 여부
    caught_100plus = []
    for ticker, max_p in ticker_max_price.items():
        daily_open_p = None
        for ev in events:
            if ev["ticker"] == ticker:
                daily_open_p = ev["daily_open"]
                break
        if daily_open_p and daily_open_p > 0:
            max_gain = (max_p / daily_open_p - 1) * 100
            if max_gain >= 100:
                bought = any(t["ticker"] == ticker for t in buy_trades)
                caught_100plus.append({
                    "ticker": ticker,
                    "max_gain_pct": round(max_gain, 1),
                    "caught": bought,
                })

    result = {
        "date": date_str,
        "config_stop_loss": cfg["stop_loss_pct"],
        "starting_krw": round(portfolio_krw),
        "ending_krw": round(running_krw),
        "day_pnl_krw": round(running_krw - portfolio_krw),
        "day_pnl_pct": round((running_krw / portfolio_krw - 1) * 100, 2),

        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / max(len(sell_trades), 1) * 100, 1),
        "profit_factor": round(pf, 2),

        "vol_spikes_total": total_vol_spikes,
        "sell_reasons": sell_reasons,

        "caught_100plus": caught_100plus,
        "caught_100plus_count": sum(1 for x in caught_100plus if x["caught"]),
        "total_100plus_count": len(caught_100plus),

        "trades": trades,
    }
    return result


# ── 일별 로그 누적 ──────────────────────────────
def update_daily_log(result: dict):
    log = []
    if DAILY_LOG.exists():
        with open(DAILY_LOG) as f:
            log = json.load(f)

    # 이미 있으면 교체
    log = [x for x in log if x.get("date") != result["date"]]
    log.append({
        "date": result["date"],
        "starting_krw": result["starting_krw"],
        "ending_krw": result["ending_krw"],
        "day_pnl_pct": result["day_pnl_pct"],
        "win_rate": result["win_rate_pct"],
        "buy_count": result["buy_count"],
        "vol_spikes": result["vol_spikes_total"],
        "stop_loss": result["config_stop_loss"],
        "caught_100plus": f"{result['caught_100plus_count']}/{result['total_100plus_count']}",
    })
    log.sort(key=lambda x: x["date"])
    with open(DAILY_LOG, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ── 엔트리포인트 ─────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 sim/engine.py YYYY-MM-DD [portfolio_krw]")
        sys.exit(1)

    date_str = sys.argv[1]
    portfolio_krw = float(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000.0

    cfg = load_config()
    result = run_engine(date_str, portfolio_krw, cfg)

    # 결과 저장
    out_path = RESULTS_DIR / f"{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    update_daily_log(result)

    # 콘솔 요약 출력
    print(f"\n{'='*50}")
    print(f"📊 {date_str} 시뮬 결과 (손절 {cfg['stop_loss_pct']}%)")
    print(f"{'='*50}")
    print(f"포트폴리오: ₩{portfolio_krw:,.0f} → ₩{result['ending_krw']:,.0f} ({result['day_pnl_pct']:+.2f}%)")
    print(f"거래: {result['buy_count']}건 | 승률: {result['win_rate_pct']:.1f}% | PF: {result['profit_factor']:.2f}")
    print(f"볼스파이크: {result['vol_spikes_total']}건")
    print(f"100%+ 종목 포착: {result['caught_100plus_count']}/{result['total_100plus_count']}")
    print(f"매도 이유: {result['sell_reasons']}")

    if result["caught_100plus"]:
        print(f"\n📈 당일 100%+ 종목:")
        for c in sorted(result["caught_100plus"], key=lambda x: -x["max_gain_pct"]):
            icon = "✅" if c["caught"] else "❌"
            print(f"  {icon} {c['ticker']} +{c['max_gain_pct']:.1f}%")

    print(f"\n결과 저장: {out_path}")
    print(json.dumps(result, ensure_ascii=False, default=str)[:200] + "...")
