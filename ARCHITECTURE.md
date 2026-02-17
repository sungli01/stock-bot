# C프로젝트: 미국주식 자동매매 시스템

## 아키텍처 (3-Module Pipeline)

```
┌─────────────┐    Redis     ┌─────────────┐    Redis     ┌─────────────┐
│  COLLECTOR   │ ──────────→ │  ANALYZER    │ ──────────→ │  TRADER     │
│  (데이터수집) │  pub/sub    │  (추세판단)   │  pub/sub    │  (매매봇)    │
└─────────────┘             └─────────────┘             └─────────────┘
       ↑                          ↑                          ↓    ↓
  Polygon.io                   TA-Lib                   KIS API  Telegram
  WebSocket                  (기술분석)                 (주문실행) (알림)
```

### 왜 3개로 분리하는가?
- **대응속도**: 각 모듈이 독립 프로세스 → 병렬 처리
- **장애격리**: Collector가 죽어도 Trader의 손절은 유지
- **확장성**: Analyzer 알고리즘 교체 시 다른 모듈 영향 없음

---

## Module 1: COLLECTOR (데이터 수집기)

**역할**: 미국 전종목 실시간 시세 수집 + 1차 필터링

**데이터 소스**: Polygon.io WebSocket (실시간 틱/분봉)
- 무료 플랜: 15분 지연 (테스트용)
- Starter ($29/월): 실시간 시세 ← 실전용

**1차 필터 조건** (형님 룰):
| 조건 | 값 |
|------|-----|
| 주당가격 | $1 이상 |
| 시가총액 | $5천만 이상 |
| 5분봉 가격변동률 | 5% 이상 |
| 1분봉 거래량 증가율 | 200% 이상 |
| 지정거래량 | 1만주 이상 |

**출력**: 조건 충족 종목 → Redis `channel:screened` 으로 publish

**기술 스택**: Python + `websocket-client` + `polygon-api-client`

---

## Module 2: ANALYZER (추세 판단기)

**역할**: Collector에서 넘어온 종목의 추세 분석 + 매수/매도 시그널 생성

**분석 지표**:
1. **EMA 크로스**: 5EMA > 20EMA = 상승추세
2. **MACD**: Signal 라인 돌파 확인
3. **RSI**: 30 이하 과매도 / 70 이상 과매수
4. **볼린저밴드**: 상단 돌파 + 거래량 동반 = 강한 매수 시그널
5. **거래량 프로파일**: 평균 대비 급증 확인

**시그널 종류**:
```python
SIGNAL_BUY = "BUY"       # 상승추세 진입 → 10분할 매수 시작
SIGNAL_SELL = "SELL"      # 추세 꺾임 → 일괄매도
SIGNAL_STOP = "STOP"      # -15% 손절
SIGNAL_WATCH = "WATCH"    # 관심종목 등록 (통보만)
```

**추세 꺾임 판단 로직**:
- 5EMA가 20EMA 하향 돌파
- MACD 히스토그램 음전환
- RSI 70 이상에서 하락 시작
- 3개 중 2개 이상 충족 시 SELL 시그널

**출력**: 시그널 → Redis `channel:signal` 로 publish

**기술 스택**: Python + `TA-Lib` + `pandas` + `numpy`

---

## Module 3: TRADER (매매봇)

**역할**: 시그널 수신 → KIS API 주문 실행 → 텔레그램 알림

**매수 로직**:
```
BUY 시그널 수신
→ 잔고 확인 (100만원 or 남은 잔고)
→ 10분할 계산 (1회 = 총액/10)
→ 1분 간격으로 10회 시장가 매수
→ 매수 완료 후 텔레그램 통보
   (종목명, 티커, 수량, 평균매입가)
```

**매도 로직**:
```
Case 1: 수익률 +30% 도달
  → ANALYZER에 추세 확인 요청
  → 추세 유지면 HOLD
  → 추세 꺾이면 즉시 일괄매도

Case 2: SELL 시그널 수신
  → 해당 종목 즉시 일괄매도 (시장가)

Case 3: 손절 (-15%)
  → 즉시 일괄매도 (시장가)
  → 텔레그램 긴급 알림
```

**안전장치**:
- 1일 최대 매수금액 제한
- 동시 보유 종목 수 제한 (기본 5종목)
- 미체결 주문 3분 초과 시 자동 취소
- 서버 장애 시 모든 보유종목 손절 주문 자동 등록

**기술 스택**: Python + `pykis` (KIS 공식 SDK) + `python-telegram-bot`

---

## 인프라

```
┌──────────────────────────────┐
│     AWS EC2 (t3.small)       │
│  or Railway (always-on)      │
│                              │
│  ┌─────────┐  ┌───────────┐ │
│  │ Redis   │  │ PostgreSQL│ │
│  │ (Pub/Sub│  │ (매매기록) │ │
│  │  + 캐시) │  │           │ │
│  └─────────┘  └───────────┘ │
│                              │
│  collector.py (프로세스 1)    │
│  analyzer.py  (프로세스 2)    │
│  trader.py    (프로세스 3)    │
│  supervisor   (프로세스 관리)  │
└──────────────────────────────┘
```

**프로세스 관리**: `supervisord` 또는 `PM2`
- 각 모듈 자동 재시작
- 로그 분리
- 헬스체크

---

## 텔레그램 알림 형식

### 종목 발굴 알림
```
🔍 종목 발굴
━━━━━━━━━━━━━━
티커: NVDA
종목명: NVIDIA Corp
현재가: $142.50
5분 변동: +6.2%
거래량: 15,200 (평균 대비 312%)
시총: $3.5T
━━━━━━━━━━━━━━
추세: 📈 상승추세 (신뢰도 87%)
[자동매수 진행중] [무시]
```

### 매수 완료 알림
```
✅ 매수 완료
━━━━━━━━━━━━━━
티커: NVDA
매수수량: 7주 (10분할 완료)
평균매입가: $143.20
총매수금액: ₩1,002,400
━━━━━━━━━━━━━━
목표가(+30%): $186.16
손절가(-15%): $121.72
```

### 매도 알림
```
💰 매도 실행
━━━━━━━━━━━━━━
티커: NVDA
매도수량: 7주 (일괄)
매도가: $188.50
수익률: +31.6%
실현손익: +₩317,200
━━━━━━━━━━━━━━
사유: 추세 꺾임 감지 (MACD 음전환)
```

---

## 스케줄

| 시간 (KST) | 동작 |
|------------|------|
| 18:00 | Collector 자동 시작, 프리마켓 감시 |
| 23:30 | 미국장 개장 (정규장 감시 시작) |
| 00:00 | 개장 30분 경과 → 자동매매 활성화 |
| 06:00 | 미국장 마감 |
| 06:30 | 일일 리포트 텔레그램 발송 |
| 06:30~18:00 | 휴면 (리소스 절약) |

---

## 데이터베이스 (PostgreSQL)

```sql
-- 매매 기록
CREATE TABLE trades (
  id SERIAL PRIMARY KEY,
  ticker VARCHAR(10) NOT NULL,
  side VARCHAR(4) NOT NULL, -- BUY/SELL
  quantity INT NOT NULL,
  price DECIMAL(12,4) NOT NULL,
  total_amount DECIMAL(15,2),
  signal_type VARCHAR(20),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 포지션 (현재 보유)
CREATE TABLE positions (
  id SERIAL PRIMARY KEY,
  ticker VARCHAR(10) UNIQUE NOT NULL,
  quantity INT NOT NULL,
  avg_price DECIMAL(12,4) NOT NULL,
  current_price DECIMAL(12,4),
  unrealized_pnl DECIMAL(15,2),
  buy_count INT DEFAULT 0, -- 분할매수 횟수
  target_count INT DEFAULT 10,
  stop_loss_price DECIMAL(12,4),
  take_profit_price DECIMAL(12,4),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 시그널 로그
CREATE TABLE signals (
  id SERIAL PRIMARY KEY,
  ticker VARCHAR(10) NOT NULL,
  signal_type VARCHAR(20) NOT NULL,
  confidence DECIMAL(5,2),
  indicators JSONB, -- RSI, MACD, EMA 등
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
```

---

## 설정 파일 (config.yaml)

```yaml
# 매수 조건
scanner:
  min_price: 1.0          # $1 이상
  min_market_cap: 50000000 # $5천만 이상
  min_volume: 10000        # 1만주 이상
  price_change_pct: 5.0    # 5% 이상
  volume_spike_pct: 200.0  # 200% 이상
  timeframe: "5min"
  volume_timeframe: "1min"

# 매매 조건
trading:
  total_buy_amount: 1000000  # 100만원
  split_count: 10             # 10분할
  split_interval_sec: 60      # 1분 간격
  max_positions: 5            # 동시 보유 최대 5종목
  take_profit_pct: 30.0       # +30% 익절
  stop_loss_pct: -15.0        # -15% 손절

# 추세 판단
analyzer:
  ema_fast: 5
  ema_slow: 20
  rsi_period: 14
  rsi_overbought: 70
  rsi_oversold: 30
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  min_signals_for_sell: 2  # 3개 중 2개 이상 충족시 매도

# 스케줄
schedule:
  start_time: "18:00"       # KST
  active_trading_after: "00:00"  # 개장 30분 후
  market_close: "06:00"
  timezone: "Asia/Seoul"

# 알림
notification:
  telegram_enabled: true
  report_time: "06:30"
  alert_on_buy: true
  alert_on_sell: true
  alert_on_discovery: true
  alert_on_error: true
```

---

## 필요 API 키 / 계정

| 서비스 | 용도 | 비용 | 비고 |
|--------|------|------|------|
| KIS 한국투자증권 | 해외주식 매매 API | 무료 | 계좌 개설 필요 |
| Polygon.io | 미국 실시간 시세 | $29/월 (Starter) | 무료 플랜은 15분 지연 |
| Telegram Bot | 알림 | 무료 | 기존 봇 활용 가능 |
| AWS EC2 / Railway | 서버 | ~$10-20/월 | 24시간 가동 |
| Redis | 모듈간 통신 | 무료 (자체 설치) | |
| PostgreSQL | 매매 기록 | 무료 (자체 설치) | |

**월 예상 비용: $40-50 (약 5-6만원)**

---

## 구현 순서

### Phase 1 (1주): 기반 + Collector
- [ ] 프로젝트 셋업 (Python, Redis, PostgreSQL)
- [ ] Polygon.io 연동 + 전종목 스캔
- [ ] 1차 필터링 로직
- [ ] 텔레그램 봇 알림 (종목 발굴 통보만)

### Phase 2 (1주): Analyzer
- [ ] TA-Lib 기술분석 파이프라인
- [ ] 상승/하락 추세 판단 알고리즘
- [ ] 매수/매도 시그널 생성
- [ ] 백테스트 프레임워크

### Phase 3 (1주): Trader
- [ ] KIS API 연동 (인증, 주문, 잔고)
- [ ] 10분할 매수 로직
- [ ] 손절/익절 자동 실행
- [ ] 텔레그램 매매 알림

### Phase 4 (1주): 안전장치 + 최적화
- [ ] 모의투자 테스트 (2주)
- [ ] 장애 대응 (자동 재시작, 긴급 손절)
- [ ] 일일 리포트
- [ ] 설정 텔레그램으로 변경 가능하게
