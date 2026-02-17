"""
패턴 마이닝 모듈
- 성공 매매 공통 조건 클러스터링
- 새 패턴 자동 생성 (승률 60%↑, 샘플 10건↑)
"""
import logging
from datetime import datetime
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from knowledge.models import Position, Pattern

logger = logging.getLogger(__name__)

# 분석 대상 지표 키
INDICATOR_KEYS = [
    "ema_5", "ema_20", "rsi_14",
    "macd_value", "macd_signal", "macd_histogram",
    "bollinger_upper", "bollinger_lower", "volume_ratio",
]


class PatternMiner:
    """성공 매매 패턴 자동 발견기"""

    def __init__(self, min_sample: int = 10, min_win_rate: float = 0.6):
        self.min_sample = min_sample
        self.min_win_rate = min_win_rate

    def mine(self, db: Session) -> list[dict]:
        """
        닫힌 포지션 분석 → 새 패턴 발견
        Returns: 새로 생성된 패턴 목록
        """
        closed = db.query(Position).filter(Position.status == "CLOSED").all()
        if len(closed) < self.min_sample:
            logger.info(f"데이터 부족 ({len(closed)}건) — 패턴 마이닝 스킵")
            return []

        winners = [p for p in closed if p.pnl and p.pnl > 0 and p.entry_indicators]
        losers = [p for p in closed if p.pnl and p.pnl <= 0 and p.entry_indicators]

        if len(winners) < self.min_sample:
            logger.info(f"성공 매매 부족 ({len(winners)}건)")
            return []

        # 지표별 성공/실패 분포 분석
        new_patterns = []
        conditions_sets = self._find_winning_conditions(winners, losers)

        for conditions, stats in conditions_sets:
            if stats["sample"] >= self.min_sample and stats["win_rate"] >= self.min_win_rate:
                # 기존 패턴과 중복 체크
                existing = db.query(Pattern).filter(Pattern.conditions == conditions).first()
                if existing:
                    # 기존 패턴 업데이트
                    existing.total_occurrences = stats["sample"]
                    existing.win_count = stats["wins"]
                    existing.win_rate = stats["win_rate"]
                    existing.avg_return = stats["avg_return"]
                    existing.last_validated = datetime.utcnow()
                    continue

                pattern = Pattern(
                    name=self._generate_name(conditions),
                    description=f"자동 발견된 패턴 (승률 {stats['win_rate']:.0%}, 샘플 {stats['sample']}건)",
                    conditions=conditions,
                    total_occurrences=stats["sample"],
                    win_count=stats["wins"],
                    win_rate=stats["win_rate"],
                    avg_return=stats["avg_return"],
                    is_active=True,
                    confidence=stats["win_rate"] * 100,
                )
                db.add(pattern)
                new_patterns.append({"name": pattern.name, "win_rate": stats["win_rate"]})
                logger.info(f"🆕 새 패턴 발견: {pattern.name} (승률 {stats['win_rate']:.0%})")

        db.commit()
        return new_patterns

    def _find_winning_conditions(self, winners: list, losers: list) -> list:
        """
        성공 매매의 공통 지표 조건 추출
        간단한 구간 분할 방식으로 클러스터링
        """
        results = []

        # 각 지표별 최적 임계값 탐색
        for key in INDICATOR_KEYS:
            win_vals = [p.entry_indicators.get(key, 0) for p in winners if p.entry_indicators]
            lose_vals = [p.entry_indicators.get(key, 0) for p in losers if p.entry_indicators]

            if not win_vals:
                continue

            # 승리 매매의 중앙값 기준 구간
            win_median = float(np.median(win_vals))
            win_q25 = float(np.percentile(win_vals, 25))
            win_q75 = float(np.percentile(win_vals, 75))

            # 이 구간에 해당하는 매매들의 승률 계산
            condition = {
                "indicator": key,
                "operator": "between",
                "value": [round(win_q25, 4), round(win_q75, 4)],
            }

            in_range_wins = sum(1 for v in win_vals if win_q25 <= v <= win_q75)
            in_range_losses = sum(1 for v in lose_vals if win_q25 <= v <= win_q75)
            total = in_range_wins + in_range_losses

            if total >= self.min_sample:
                win_rate = in_range_wins / total
                avg_ret = float(np.mean([p.pnl_pct for p in winners
                                         if p.entry_indicators and
                                         win_q25 <= p.entry_indicators.get(key, 0) <= win_q75]))

                results.append((
                    [condition],
                    {
                        "sample": total,
                        "wins": in_range_wins,
                        "win_rate": win_rate,
                        "avg_return": avg_ret,
                    }
                ))

        # 2개 지표 조합도 탐색
        for i, key1 in enumerate(INDICATOR_KEYS):
            for key2 in INDICATOR_KEYS[i+1:]:
                combo_conditions, combo_stats = self._check_combo(winners, losers, key1, key2)
                if combo_stats and combo_stats["sample"] >= self.min_sample:
                    results.append((combo_conditions, combo_stats))

        return results

    def _check_combo(self, winners, losers, key1, key2) -> tuple:
        """2개 지표 조합의 승률 검증"""
        win_v1 = [p.entry_indicators.get(key1, 0) for p in winners if p.entry_indicators]
        win_v2 = [p.entry_indicators.get(key2, 0) for p in winners if p.entry_indicators]

        if not win_v1 or not win_v2:
            return [], None

        med1 = float(np.median(win_v1))
        med2 = float(np.median(win_v2))

        # 중앙값 기준 필터
        conditions = [
            {"indicator": key1, "operator": ">=", "value": round(med1, 4)},
            {"indicator": key2, "operator": ">=", "value": round(med2, 4)},
        ]

        wins = sum(1 for p in winners if p.entry_indicators and
                   p.entry_indicators.get(key1, 0) >= med1 and
                   p.entry_indicators.get(key2, 0) >= med2)
        losses = sum(1 for p in losers if p.entry_indicators and
                     p.entry_indicators.get(key1, 0) >= med1 and
                     p.entry_indicators.get(key2, 0) >= med2)

        total = wins + losses
        if total < self.min_sample:
            return [], None

        return conditions, {
            "sample": total,
            "wins": wins,
            "win_rate": wins / total,
            "avg_return": 0,  # 간략화
        }

    def _generate_name(self, conditions: list) -> str:
        """조건 기반 패턴 이름 자동 생성"""
        parts = []
        for c in conditions:
            ind = c.get("indicator", "unknown").replace("_", "")
            op = c.get("operator", "")
            parts.append(f"{ind}_{op}")
        return "_".join(parts)[:100]
