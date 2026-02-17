"""
SQLAlchemy ORM 모델 — ONTOLOGY.md의 스키마 반영
테이블: stock_knowledge, market_contexts, signals, trades,
       positions, patterns, weight_history, daily_reports
"""
import os
import uuid
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean,
    Text, DateTime, ForeignKey, Index, ARRAY
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://stockbot:stockbot@localhost:5432/stockbot")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_db():
    """DB 세션 팩토리"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── 종목 지식 ────────────────────────────────────────────
class StockKnowledge(Base):
    __tablename__ = "stock_knowledge"

    ticker = Column(String(10), primary_key=True)
    name = Column(String(100))
    sector = Column(String(50))
    industry = Column(String(100))
    total_trades = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    win_rate = Column(Float, default=0)
    avg_return = Column(Float, default=0)
    avg_holding_min = Column(Integer, default=0)
    best_signal_type = Column(String(30))
    risk_score = Column(Float, default=50)
    tags = Column(JSONB, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── 시장 컨텍스트 ────────────────────────────────────────
class MarketContext(Base):
    __tablename__ = "market_contexts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False)
    sp500_change = Column(Float)
    nasdaq_change = Column(Float)
    vix = Column(Float)
    dxy = Column(Float)
    us10y = Column(Float)
    fear_greed = Column(Integer)
    sector_momentum = Column(JSONB)
    events = Column(JSONB, default=list)


# ─── 시그널 ───────────────────────────────────────────────
class Signal(Base):
    __tablename__ = "signals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(10), nullable=False, index=True)
    signal_type = Column(String(20), nullable=False)
    confidence = Column(Float)
    price_change_pct = Column(Float)
    volume_spike_pct = Column(Float)
    indicators = Column(JSONB, nullable=False)
    trend_direction = Column(String(10))
    trend_strength = Column(Float)
    market_context_id = Column(Integer, ForeignKey("market_contexts.id"))
    outcome = Column(String(10))  # WIN, LOSS, PENDING
    outcome_pnl = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ─── 매매 기록 ────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_id = Column(UUID(as_uuid=True), index=True)
    ticker = Column(String(10), nullable=False)
    signal_id = Column(UUID(as_uuid=True), ForeignKey("signals.id"))
    side = Column(String(4), nullable=False)  # BUY/SELL
    quantity = Column(Integer, nullable=False)
    order_price = Column(Float)
    filled_price = Column(Float)
    slippage = Column(Float)
    commission = Column(Float)
    split_index = Column(Integer)
    filled_at = Column(DateTime, default=datetime.utcnow)


# ─── 포지션 ──────────────────────────────────────────────
class Position(Base):
    __tablename__ = "positions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(10), nullable=False, index=True)
    status = Column(String(10), default="OPEN", index=True)
    entry_signal_ids = Column(ARRAY(UUID(as_uuid=True)), default=list)
    avg_entry_price = Column(Float)
    total_quantity = Column(Integer)
    total_invested = Column(Float)
    buy_count = Column(Integer, default=0)
    exit_signal_id = Column(UUID(as_uuid=True))
    exit_price = Column(Float)
    exit_reason = Column(String(20))
    pnl = Column(Float)
    pnl_pct = Column(Float)
    holding_minutes = Column(Integer)
    max_drawdown = Column(Float)
    max_profit = Column(Float)
    entry_indicators = Column(JSONB)
    exit_indicators = Column(JSONB)
    market_context_entry = Column(Integer, ForeignKey("market_contexts.id"))
    market_context_exit = Column(Integer, ForeignKey("market_contexts.id"))
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime)


# ─── 학습된 패턴 ─────────────────────────────────────────
class Pattern(Base):
    __tablename__ = "patterns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100))
    description = Column(Text)
    conditions = Column(JSONB, nullable=False)
    total_occurrences = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    win_rate = Column(Float, default=0)
    avg_return = Column(Float, default=0)
    avg_holding_min = Column(Integer, default=0)
    best_contexts = Column(JSONB)
    is_active = Column(Boolean, default=True, index=True)
    confidence = Column(Float, default=50)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    last_validated = Column(DateTime, default=datetime.utcnow)


# ─── 지표 가중치 이력 ────────────────────────────────────
class WeightHistory(Base):
    __tablename__ = "weight_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    weights = Column(JSONB, nullable=False)
    performance_score = Column(Float)
    sample_size = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── 일일 리포트 ─────────────────────────────────────────
class DailyReport(Base):
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, unique=True, nullable=False)
    total_trades = Column(Integer)
    total_pnl = Column(Float)
    win_rate = Column(Float)
    details = Column(JSONB)
    created_at = Column(DateTime, default=datetime.utcnow)
