# 온톨로지 기반 매매 지식 시스템

## 핵심 개념

모든 매매 데이터를 **관계형 온톨로지**로 정의하고, 축적된 데이터로 지속 학습하여
스캔 정확도 + 추세 판단 + 매매 성공률을 자동 개선하는 구조.

```
┌──────────────────────────────────────────────────────────┐
│                    KNOWLEDGE GRAPH                         │
│                                                           │
│  [종목] ──has_sector──→ [섹터]                              │
│    │                      │                               │
│    ├──triggered_signal──→ [시그널] ──resulted_in──→ [매매]   │
│    │                        │                             │
│    ├──has_pattern──→ [패턴]  ├──used_indicator──→ [지표]    │
│    │                        │                             │
│    └──in_context──→ [시장상황] └──confidence──→ [신뢰도]    │
│                                                           │
│  [매매] ──produced──→ [결과] ──feedback──→ [학습모델]        │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

---

## 온톨로지 엔티티 정의

### 1. Stock (종목)
```yaml
Stock:
  ticker: string          # NVDA, AAPL
  name: string            # NVIDIA Corp
  sector: Sector          # Technology
  industry: string        # Semiconductors
  market_cap: float       # 시가총액
  avg_volume_30d: int     # 30일 평균 거래량
  volatility_30d: float   # 30일 변동성
  # 학습 데이터
  win_rate: float         # 이 종목 매매 승률
  avg_return: float       # 평균 수익률
  trade_count: int        # 총 매매 횟수
  best_signal_type: string # 가장 성공적인 시그널 유형
  risk_score: float       # 리스크 점수 (0-100)
  tags: [string]          # ["momentum", "high-vol", "earnings-play"]
```

### 2. Signal (시그널)
```yaml
Signal:
  id: uuid
  stock: Stock
  type: enum              # BUY, SELL, STOP, WATCH
  timestamp: datetime
  # 트리거 조건
  trigger:
    price_change_pct: float
    volume_spike_pct: float
    timeframe: string
  # 기술지표 스냅샷
  indicators:
    ema_5: float
    ema_20: float
    rsi_14: float
    macd_value: float
    macd_signal: float
    macd_histogram: float
    bollinger_upper: float
    bollinger_lower: float
    volume_ratio: float   # 평균 대비
  # 추세 컨텍스트
  trend:
    direction: enum       # UP, DOWN, SIDEWAYS
    strength: float       # 0-100
    duration_minutes: int # 추세 지속 시간
  confidence: float       # 0-100 (학습 기반 신뢰도)
  market_context: MarketContext
```

### 3. Trade (매매)
```yaml
Trade:
  id: uuid
  stock: Stock
  signal: Signal          # 이 매매를 트리거한 시그널
  side: enum              # BUY, SELL
  # 주문 정보
  order_type: string      # MARKET, LIMIT
  quantity: int
  price: float
  total_amount: float
  split_index: int        # 분할매수 N/10
  # 실행 결과
  filled_price: float
  filled_at: datetime
  slippage: float         # 주문가 vs 체결가 차이
  commission: float
```

### 4. Position (포지션 = 매수~매도 전체 사이클)
```yaml
Position:
  id: uuid
  stock: Stock
  # 매수
  entry_signals: [Signal]
  entry_trades: [Trade]
  avg_entry_price: float
  total_quantity: int
  total_invested: float
  # 매도
  exit_signal: Signal
  exit_trades: [Trade]
  exit_price: float
  exit_reason: enum       # TAKE_PROFIT, STOP_LOSS, TREND_REVERSAL
  # 결과
  pnl: float              # 실현손익
  pnl_pct: float          # 수익률
  holding_duration: int   # 보유 시간(분)
  max_drawdown: float     # 보유 중 최대 낙폭
  max_profit: float       # 보유 중 최대 수익
  # 메타
  opened_at: datetime
  closed_at: datetime
```

### 5. MarketContext (시장 상황)
```yaml
MarketContext:
  timestamp: datetime
  # 지수
  sp500_change: float
  nasdaq_change: float
  vix: float              # 변동성 지수
  # 섹터 흐름
  sector_momentum:
    Technology: float
    Healthcare: float
    Financial: float
    Energy: float
    # ...
  # 매크로
  dxy: float              # 달러 인덱스
  us10y: float            # 미국 10년물 금리
  fear_greed_index: int   # CNN Fear & Greed (0-100)
  # 특이사항
  is_earnings_season: bool
  major_events: [string]  # ["FOMC", "CPI발표"]
```

### 6. Pattern (패턴 = 학습된 지식)
```yaml
Pattern:
  id: uuid
  name: string            # "고거래량_돌파_상승추세"
  description: string
  # 조건 조합
  conditions:
    - indicator: "volume_ratio"
      operator: ">="
      value: 3.0
    - indicator: "ema_5_vs_20"
      operator: ">"
      value: 0
    - indicator: "rsi_14"
      operator: "between"
      value: [40, 70]
  # 성과
  total_occurrences: int
  win_count: int
  win_rate: float
  avg_return: float
  avg_holding_minutes: int
  # 유효 컨텍스트
  best_market_conditions:
    vix_range: [float, float]
    sector: [string]
    time_of_day: [string]  # ["first_hour", "mid_session"]
  # 자동 생성됨
  discovered_at: datetime
  last_validated: datetime
  confidence: float
```

---

## 자기학습 파이프라인

```
               ┌─────────────────────────────────────────┐
               │          FEEDBACK LOOP                   │
               │                                          │
  매매 완료 ──→ │  1. 결과 기록 (Position 닫힘)             │
               │  2. 시그널 정확도 업데이트                  │
               │  3. 패턴 매칭 + 성과 업데이트               │
               │  4. 새 패턴 발견 (패턴 마이닝)              │
               │  5. Analyzer 가중치 조정                   │
               │  6. Scanner 필터 최적화                    │
               │                                          │
               └──────────────────────────────────────────┘
```

### Step 1: 결과 기록
매도 실행 시 Position에 전체 사이클 기록 (진입~청산, 모든 지표 스냅샷)

### Step 2: 시그널 정확도 업데이트
```python
# 시그널 타입별 승률 추적
signal_accuracy = {
    "ema_cross_buy": {"wins": 45, "total": 60, "rate": 0.75},
    "volume_breakout": {"wins": 30, "total": 50, "rate": 0.60},
    "macd_divergence": {"wins": 22, "total": 40, "rate": 0.55},
}
```

### Step 3: 패턴 매칭
완료된 매매의 진입 시점 지표 조합 → 기존 Pattern과 매칭 → 성과 업데이트

### Step 4: 패턴 마이닝 (핵심)
```python
# 매주 실행: 성공한 매매들의 공통 조건 추출
def mine_patterns(closed_positions, min_sample=10):
    """
    승률 60% 이상 매매들의 공통 지표 조건을 클러스터링하여
    새로운 Pattern 엔티티 자동 생성
    """
    winners = [p for p in closed_positions if p.pnl > 0]
    
    # 지표 조합별 클러스터링
    clusters = cluster_by_indicators(winners)
    
    for cluster in clusters:
        if cluster.sample_size >= min_sample and cluster.win_rate >= 0.6:
            create_pattern(
                conditions=cluster.common_conditions,
                win_rate=cluster.win_rate,
                avg_return=cluster.avg_return
            )
```

### Step 5: Analyzer 가중치 자동 조정
```python
# 지표별 가중치 (학습으로 자동 조정)
indicator_weights = {
    "ema_cross": 0.25,      # 초기값
    "macd": 0.25,
    "rsi": 0.20,
    "volume": 0.30,
}

def update_weights(recent_trades, lookback=100):
    """
    최근 100건 매매 결과 기반으로
    각 지표의 예측 정확도에 비례하여 가중치 재배분
    """
    for indicator in indicator_weights:
        accuracy = calc_indicator_accuracy(indicator, recent_trades)
        indicator_weights[indicator] = accuracy / sum_all_accuracies
```

### Step 6: Scanner 필터 최적화
```python
# 필터 조건도 학습으로 조정
def optimize_scanner(closed_positions, lookback=200):
    """
    성공 매매의 진입 시점 조건 분포 분석 →
    최적 필터 임계값 자동 조정
    
    예: 5% 변동률 필터 → 실제 승률 높은 구간이 7% 이상이면
        필터를 7%로 상향 조정
    """
    winning_entries = [p.entry_conditions for p in closed_positions if p.pnl > 0]
    
    optimal_price_change = find_optimal_threshold(
        winning_entries, "price_change_pct", min_sample=30
    )
    # config.yaml 자동 업데이트
```

---

## 학습 주기

| 주기 | 작업 | 목적 |
|------|------|------|
| 매 매매 | Position 기록 + 시그널 정확도 | 실시간 데이터 축적 |
| 매일 06:30 | 일일 리포트 + 당일 패턴 매칭 | 단기 피드백 |
| 매주 토요일 | 패턴 마이닝 + 가중치 조정 | 전략 최적화 |
| 매월 1일 | Scanner 필터 최적화 + 패턴 검증 | 장기 진화 |

---

## 학습 데이터 활용 예시

### 1. 종목 스캔 개선
```
기존: 5분봉 5% + 거래량 200% → 종목 50개 발굴
학습후: 5분봉 7% + 거래량 300% + RSI<65 → 종목 15개 (승률 75%)
         ↑ 데이터가 알려줌: 5%짜리는 승률 낮음
```

### 2. 추세 판단 개선
```
기존: EMA 25%, MACD 25%, RSI 20%, Volume 30%
학습후: EMA 15%, MACD 35%, RSI 10%, Volume 40%
         ↑ 데이터가 알려줌: MACD+Volume이 가장 정확
```

### 3. 종목별 전략 분화
```
NVDA: 모멘텀 전략 승률 82% → 적극 매수
TSLA: 모멘텀 승률 45%, 역추세 승률 71% → 전략 전환
바이오: 전체 승률 30% → 자동 블랙리스트
```

### 4. 시장 상황별 전략
```
VIX < 15: 공격적 (10분할 빠르게)
VIX 15-25: 표준 (10분할 1분간격)
VIX > 25: 보수적 (5분할 + 손절 -10%로 타이트)
```

---

## PostgreSQL 스키마 (온톨로지 반영)

```sql
-- 종목 지식 (축적)
CREATE TABLE stock_knowledge (
  ticker VARCHAR(10) PRIMARY KEY,
  name VARCHAR(100),
  sector VARCHAR(50),
  industry VARCHAR(100),
  -- 학습된 메트릭
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
  -- 트리거
  price_change_pct DECIMAL(8,4),
  volume_spike_pct DECIMAL(8,2),
  -- 지표 스냅샷
  indicators JSONB NOT NULL,
  trend_direction VARCHAR(10),
  trend_strength DECIMAL(5,2),
  -- 컨텍스트
  market_context_id INT REFERENCES market_contexts(id),
  -- 결과 (매매 완료 후 업데이트)
  outcome VARCHAR(10),  -- WIN, LOSS, PENDING
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
  status VARCHAR(10) DEFAULT 'OPEN', -- OPEN, CLOSED
  -- 진입
  entry_signal_ids UUID[] DEFAULT '{}',
  avg_entry_price DECIMAL(12,4),
  total_quantity INT,
  total_invested DECIMAL(15,2),
  buy_count INT DEFAULT 0,
  -- 청산
  exit_signal_id UUID,
  exit_price DECIMAL(12,4),
  exit_reason VARCHAR(20),
  -- 결과
  pnl DECIMAL(15,2),
  pnl_pct DECIMAL(8,4),
  holding_minutes INT,
  max_drawdown DECIMAL(8,4),
  max_profit DECIMAL(8,4),
  -- 지표 스냅샷 (진입/청산 시점)
  entry_indicators JSONB,
  exit_indicators JSONB,
  market_context_entry INT REFERENCES market_contexts(id),
  market_context_exit INT REFERENCES market_contexts(id),
  -- 시간
  opened_at TIMESTAMPTZ DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

-- 학습된 패턴
CREATE TABLE patterns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(100),
  description TEXT,
  conditions JSONB NOT NULL,
  -- 성과
  total_occurrences INT DEFAULT 0,
  win_count INT DEFAULT 0,
  win_rate DECIMAL(5,3) DEFAULT 0,
  avg_return DECIMAL(8,4) DEFAULT 0,
  avg_holding_min INT DEFAULT 0,
  -- 유효 컨텍스트
  best_contexts JSONB,
  -- 메타
  is_active BOOLEAN DEFAULT true,
  confidence DECIMAL(5,2) DEFAULT 50,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  last_validated TIMESTAMPTZ DEFAULT NOW()
);

-- 지표 가중치 이력 (학습 추적)
CREATE TABLE weight_history (
  id SERIAL PRIMARY KEY,
  weights JSONB NOT NULL,
  performance_score DECIMAL(8,4),
  sample_size INT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_signals_ticker ON signals(ticker);
CREATE INDEX idx_signals_created ON signals(created_at);
CREATE INDEX idx_trades_position ON trades(position_id);
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_positions_ticker ON positions(ticker);
CREATE INDEX idx_patterns_active ON patterns(is_active);
```
