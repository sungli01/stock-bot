-- stock-bot 데이터베이스 초기화 스크립트
-- ONTOLOGY.md 스키마 반영

-- UUID 확장 (gen_random_uuid)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 종목 지식 (축적)
CREATE TABLE stock_knowledge (
  ticker VARCHAR(10) PRIMARY KEY,
  name VARCHAR(100),
  sector VARCHAR(50),
  industry VARCHAR(100),
  total_trades INT DEFAULT 0,
  win_count INT DEFAULT 0,
  win_rate DECIMAL(5,3) DEFAULT 0,
  avg_return DECIMAL(8,4) DEFAULT 0,
  avg_holding_min INT DEFAULT 0,
  best_signal_type VARCHAR(30),
  risk_score DECIMAL(5,2) DEFAULT 50,
  tags JSONB DEFAULT '[]',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 시장 컨텍스트 스냅샷
CREATE TABLE market_contexts (
  id SERIAL PRIMARY KEY,
  timestamp TIMESTAMPTZ NOT NULL,
  sp500_change DECIMAL(6,3),
  nasdaq_change DECIMAL(6,3),
  vix DECIMAL(6,2),
  dxy DECIMAL(7,3),
  us10y DECIMAL(5,3),
  fear_greed INT,
  sector_momentum JSONB,
  events JSONB DEFAULT '[]'
);

-- 시그널 (모든 지표 스냅샷 포함)
CREATE TABLE signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker VARCHAR(10) NOT NULL,
  signal_type VARCHAR(20) NOT NULL,
  confidence DECIMAL(5,2),
  price_change_pct DECIMAL(8,4),
  volume_spike_pct DECIMAL(8,2),
  indicators JSONB NOT NULL,
  trend_direction VARCHAR(10),
  trend_strength DECIMAL(5,2),
  market_context_id INT REFERENCES market_contexts(id),
  outcome VARCHAR(10),
  outcome_pnl DECIMAL(12,4),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 매매 기록
CREATE TABLE trades (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id UUID,
  ticker VARCHAR(10) NOT NULL,
  signal_id UUID REFERENCES signals(id),
  side VARCHAR(4) NOT NULL,
  quantity INT NOT NULL,
  order_price DECIMAL(12,4),
  filled_price DECIMAL(12,4),
  slippage DECIMAL(8,4),
  commission DECIMAL(8,4),
  split_index INT,
  filled_at TIMESTAMPTZ DEFAULT NOW()
);

-- 포지션 (전체 사이클)
CREATE TABLE positions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker VARCHAR(10) NOT NULL,
  status VARCHAR(10) DEFAULT 'OPEN',
  entry_signal_ids UUID[] DEFAULT '{}',
  avg_entry_price DECIMAL(12,4),
  total_quantity INT,
  total_invested DECIMAL(15,2),
  buy_count INT DEFAULT 0,
  exit_signal_id UUID,
  exit_price DECIMAL(12,4),
  exit_reason VARCHAR(20),
  pnl DECIMAL(15,2),
  pnl_pct DECIMAL(8,4),
  holding_minutes INT,
  max_drawdown DECIMAL(8,4),
  max_profit DECIMAL(8,4),
  entry_indicators JSONB,
  exit_indicators JSONB,
  market_context_entry INT REFERENCES market_contexts(id),
  market_context_exit INT REFERENCES market_contexts(id),
  opened_at TIMESTAMPTZ DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

-- 학습된 패턴
CREATE TABLE patterns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(100),
  description TEXT,
  conditions JSONB NOT NULL,
  total_occurrences INT DEFAULT 0,
  win_count INT DEFAULT 0,
  win_rate DECIMAL(5,3) DEFAULT 0,
  avg_return DECIMAL(8,4) DEFAULT 0,
  avg_holding_min INT DEFAULT 0,
  best_contexts JSONB,
  is_active BOOLEAN DEFAULT true,
  confidence DECIMAL(5,2) DEFAULT 50,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  last_validated TIMESTAMPTZ DEFAULT NOW()
);

-- 지표 가중치 이력
CREATE TABLE weight_history (
  id SERIAL PRIMARY KEY,
  weights JSONB NOT NULL,
  performance_score DECIMAL(8,4),
  sample_size INT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 일일 리포트
CREATE TABLE daily_reports (
  id SERIAL PRIMARY KEY,
  date DATE UNIQUE NOT NULL,
  total_trades INT,
  total_pnl DECIMAL(15,2),
  win_rate DECIMAL(5,2),
  details JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_signals_ticker ON signals(ticker);
CREATE INDEX idx_signals_created ON signals(created_at);
CREATE INDEX idx_trades_position ON trades(position_id);
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_positions_ticker ON positions(ticker);
CREATE INDEX idx_patterns_active ON patterns(is_active);
