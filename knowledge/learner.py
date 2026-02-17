"""
자기학습 파이프라인
- 매매 완료 시 결과 기록
- 시그널 정확도 업데이트
- 지표 가중치 조정 (최근 100건 기반)
"""
import json
import logging
from datetime import datetime
from typing import Optional

import redis
from sqlalchemy.orm import Session

from knowledge.models import (
    SessionLocal, Signal, Trade, Position, StockKnowledge, WeightHistory
)

logger = logging.getLogger(__name__)

# 기본 지표 가중치
DEFAULT_WEIGHTS = {
    "ema_cross": 0.25,
    "macd": 0.25,
    "rsi": 0.20,
    "volume": 0.30,
}


class Learner:
    """자기학습 엔진 — 매매 결과 피드백으로 전략 최적화"""

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.redis = redis_client

    def record_trade_result(self, db: Session, position_id: str, exit_price: float,
                            exit_reason: str, exit_indicators: Optional[dict] = None):
        """
        매매 완료 시 Position 닫기 + 결과 기록
        """
        position = db.query(Position).filter(Position.id == position_id).first()
        if not position:
            logger.error(f"포지션 {position_id} 없음")
            return

        # 손익 계산
        pnl = (exit_price - position.avg_entry_price) * position.total_quantity
        pnl_pct = ((exit_price - position.avg_entry_price) / position.avg_entry_price) * 100
        holding_min = int((datetime.utcnow() - position.opened_at).total_seconds() / 60)

        # Position 업데이트
        position.status = "CLOSED"
        position.exit_price = exit_price
        position.exit_reason = exit_reason
        position.pnl = pnl
        position.pnl_pct = pnl_pct
        position.holding_minutes = holding_min
        position.exit_indicators = exit_indicators
        position.closed_at = datetime.utcnow()

        db.commit()

        # 시그널 결과 업데이트
        self._update_signal_outcomes(db, position)

        # 종목 지식 업데이트
        self._update_stock_knowledge(db, position.ticker)

        logger.info(
            f"📝 매매 결과 기록: {position.ticker} "
            f"PnL={pnl:+.2f} ({pnl_pct:+.1f}%) "
            f"보유 {holding_min}분 사유={exit_reason}"
        )

    def _update_signal_outcomes(self, db: Session, position: Position):
        """시그널 outcome 업데이트 (WIN/LOSS)"""
        outcome = "WIN" if position.pnl > 0 else "LOSS"
        for sig_id in (position.entry_signal_ids or []):
            signal = db.query(Signal).filter(Signal.id == sig_id).first()
            if signal:
                signal.outcome = outcome
                signal.outcome_pnl = position.pnl_pct
        db.commit()

    def _update_stock_knowledge(self, db: Session, ticker: str):
        """종목별 승률, 평균 수익률 업데이트"""
        closed = db.query(Position).filter(
            Position.ticker == ticker,
            Position.status == "CLOSED"
        ).all()

        if not closed:
            return

        total = len(closed)
        wins = sum(1 for p in closed if p.pnl and p.pnl > 0)
        avg_ret = sum(p.pnl_pct or 0 for p in closed) / total
        avg_hold = sum(p.holding_minutes or 0 for p in closed) / total

        knowledge = db.query(StockKnowledge).filter(StockKnowledge.ticker == ticker).first()
        if not knowledge:
            knowledge = StockKnowledge(ticker=ticker)
            db.add(knowledge)

        knowledge.total_trades = total
        knowledge.win_count = wins
        knowledge.win_rate = wins / total if total > 0 else 0
        knowledge.avg_return = avg_ret
        knowledge.avg_holding_min = int(avg_hold)
        knowledge.updated_at = datetime.utcnow()
        db.commit()

    def update_weights(self, db: Session, lookback: int = 100):
        """
        최근 N건 매매 결과 기반으로 지표 가중치 자동 조정
        각 지표의 예측 정확도에 비례하여 가중치 재배분
        """
        closed = db.query(Position).filter(
            Position.status == "CLOSED"
        ).order_by(Position.closed_at.desc()).limit(lookback).all()

        if len(closed) < 20:
            logger.info(f"학습 데이터 부족 ({len(closed)}건) — 가중치 조정 스킵")
            return DEFAULT_WEIGHTS

        # 각 지표별 정확도 계산
        indicator_accuracy = {}
        for indicator in DEFAULT_WEIGHTS.keys():
            correct = 0
            total = 0
            for pos in closed:
                indicators = pos.entry_indicators or {}
                if not indicators:
                    continue
                total += 1
                # 지표가 상승을 가리켰고 실제로 수익 → 정확
                if self._indicator_was_bullish(indicator, indicators) and pos.pnl > 0:
                    correct += 1
                elif not self._indicator_was_bullish(indicator, indicators) and pos.pnl <= 0:
                    correct += 1

            indicator_accuracy[indicator] = correct / total if total > 0 else 0.25

        # 정확도 비례 가중치 재배분
        total_acc = sum(indicator_accuracy.values())
        if total_acc <= 0:
            return DEFAULT_WEIGHTS

        new_weights = {k: v / total_acc for k, v in indicator_accuracy.items()}

        # DB에 가중치 이력 저장
        wh = WeightHistory(
            weights=new_weights,
            performance_score=sum(1 for p in closed if p.pnl and p.pnl > 0) / len(closed),
            sample_size=len(closed),
        )
        db.add(wh)
        db.commit()

        # Redis 캐시 업데이트
        if self.redis:
            self.redis.set("indicator_weights", json.dumps(new_weights))

        logger.info(f"🔄 가중치 업데이트: {new_weights}")
        return new_weights

    def _indicator_was_bullish(self, indicator: str, indicators: dict) -> bool:
        """지표가 상승을 가리키고 있었는지 판단"""
        if indicator == "ema_cross":
            return indicators.get("ema_5", 0) > indicators.get("ema_20", 0)
        elif indicator == "macd":
            return indicators.get("macd_histogram", 0) > 0
        elif indicator == "rsi":
            rsi = indicators.get("rsi_14", 50)
            return 30 < rsi < 70  # 적정 범위
        elif indicator == "volume":
            return indicators.get("volume_ratio", 0) > 200
        return False
