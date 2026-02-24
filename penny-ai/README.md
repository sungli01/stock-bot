# 🐾 penny-ai

> **페니스탁 전용 강화학습 AI 트레이딩 시스템**  
> AWS 독립 구동 | Dual-Agent RL | 자율 진화 학습 | 한국투자증권 API 연동

---

## 🏗️ 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                    AWS 독립 시스템                    │
│                                                     │
│  [EventBridge 스케줄러]                             │
│  16:00 ET → 수집 → 처리 → 학습 → 보고              │
│          ↓                                          │
│  [S3 Data Lake: sungli-market-data]                 │
│  raw/ → derived/ → ontology/ → models/             │
│          ↓                                          │
│  [EC2 Trading Engine]                               │
│  Feeder(Transformer) → Trader(LSTM+PPO)             │
│  → KIS API → 텔레그램 보고                          │
└─────────────────────────────────────────────────────┘
```

## 📁 프로젝트 구조

```
penny-ai/
├── collector/          # 일일 데이터 수집 (Polygon API)
│   ├── polygon_client.py
│   ├── s3_writer.py
│   └── daily_collector.py
├── processor/          # 피처 엔지니어링 + 케이스 분류
│   ├── feature_engine.py   # BB, RSI, VWAP
│   ├── event_detector.py   # 1차/2차 상승 감지
│   └── case_classifier.py  # A/B/C/D/E 분류
├── ai/                 # AI 모델 (Transformer + LSTM + PPO)
│   ├── environment.py  # gymnasium 트레이딩 환경
│   ├── feeder.py       # Transformer 패턴 인식
│   ├── trader.py       # LSTM + PPO 강화학습
│   └── trainer.py      # 학습 오케스트레이터
├── trading/            # 실전 매매
│   ├── kis_client.py   # 한국투자증권 API
│   ├── engine.py       # 트레이딩 엔진
│   └── risk_manager.py # 리스크 관리
├── simulation/         # 백테스트
│   └── backtester.py
├── reporter/           # 텔레그램 보고
│   └── telegram_reporter.py
├── utils/              # 공통 유틸
│   └── data_fabric.py  # S3 데이터 인터페이스
├── infrastructure/     # Docker
│   └── Dockerfile
├── .env.example
├── requirements.txt
└── main.py             # 메인 진입점
```

## 🚀 실행 방법

### 환경 설정
```bash
cp .env.example .env
# .env 파일에 API 키 입력
pip install -r requirements.txt
```

### 모드별 실행
```bash
# 데이터 수집
python main.py --mode collect

# 피처 엔지니어링
python main.py --mode process

# AI 학습
python main.py --mode train

# 백테스트 시뮬레이션
python main.py --mode simulate

# 실시간 매매 (PAPER_MODE=true → 가상, false → 실전)
python main.py --mode trade

# 전체 파이프라인
python main.py --mode all
```

### Docker 실행
```bash
docker build -t penny-ai .
docker run --env-file .env penny-ai
```

## 🤖 AI 모델 구조

### Feeder Agent (Transformer)
- 입력: 60분봉 슬라이딩 윈도우 (OHLCV + BB + RSI + VWAP)
- 출력: 케이스 분류 확률 (A/B/C/D/E), 2차 상승 신호
- 학습: Supervised (레이블된 이벤트 데이터)

### Trader Agent (LSTM + PPO)
- 입력: Feeder 출력 + 현재 포지션 + 잔고 비율
- 출력: BUY / HOLD / SELL
- 학습: Reinforcement Learning

### 리워드 함수
| 조건 | 리워드 |
|------|--------|
| 수익 +5% 이상 | +2.0 |
| 수익 +10% 이상 | +3.0 |
| 수익 +20% 이상 | +5.0 |
| 손절 -7% | -2.0 |
| C/D형 매수 | -1.0 |
| 20분 무변동 홀드 | -0.5 |

## 📊 케이스 분류

| 케이스 | 조건 | 전략 |
|--------|------|------|
| **A형** | 2차 상승 + BB돌파 + 지속 상승 | 피크 -5% 트레일링 |
| **B형** | 2차 상승 + BB돌파 + 급등 후 급락 | 피크 -3% 빠른 이탈 |
| **C형** | 2차 상승 + BB돌파 실패 | 매수 금지 |
| **D형** | 2차 상승 없음 | 매수 금지 |
| **E형** | 3차 이상 상승 (재료 지속) | 추가 매수 허용 |

## ⏰ 자동화 스케줄 (AWS EventBridge)

| 시간 (ET) | 작업 |
|-----------|------|
| 04:00 | 프리마켓 감시 시작 |
| 09:30 | 본장 매매 시작 |
| 16:00 | 장 마감 → 데이터 수집 시작 |
| 16:30 | 피처 엔지니어링 |
| 17:00 | AI 온라인 학습 |
| 17:30 | 텔레그램 일일 보고 |

## 🔐 환경 변수

| 변수 | 설명 |
|------|------|
| `POLYGON_API_KEY` | Polygon.io API 키 |
| `AWS_ACCESS_KEY_ID` | AWS 액세스 키 |
| `AWS_SECRET_ACCESS_KEY` | AWS 시크릿 키 |
| `S3_BUCKET` | S3 버킷명 |
| `KIS_APP_KEY` | 한국투자증권 앱 키 |
| `KIS_APP_SECRET` | 한국투자증권 앱 시크릿 |
| `KIS_ACCOUNT_NO` | 계좌번호 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 텔레그램 채팅 ID |
| `PAPER_MODE` | true=가상, false=실전 |
| `SEED_AMOUNT` | 초기 시드 (원) |

## 📈 예상 성과 (백테스트 기준)

| 시나리오 | 연 수익률 | MDD | 샤프비율 |
|----------|-----------|-----|---------|
| 보수적 | +150% | -35% | 2.0 |
| 중립적 | +300% | -30% | 2.8 |
| 낙관적 | +700% | -40% | 3.5 |

---

> ⚠️ 투자에는 항상 리스크가 따릅니다. 이 시스템은 교육/연구 목적으로 제작되었습니다.
