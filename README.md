# 🤖 stock-bot — 미국주식 자동매매 시스템

3-Module Pipeline 기반 미국주식 자동매매 봇.

## 아키텍처

```
COLLECTOR (스캔) → Redis → ANALYZER (추세판단) → Redis → TRADER (매매실행)
```

- **Collector**: Polygon.io API로 전종목 스캔 + 1차 필터링
- **Analyzer**: 기술지표 (EMA, MACD, RSI, 볼린저밴드) 기반 시그널 생성
- **Trader**: KIS API 10분할 매수, 자동 손절/익절

## 빠른 시작

```bash
# 1. 환경변수 설정
cp .env.example .env
# .env 파일에 API 키 입력

# 2. 인프라 실행
docker-compose up -d

# 3. 의존성 설치
pip install -r requirements.txt

# 4. 실행
python main.py
```

## 1차 필터 조건

| 조건 | 값 |
|------|-----|
| 주당가격 | $1 이상 |
| 시가총액 | $5천만 이상 |
| 5분봉 변동률 | 5% 이상 |
| 거래량 증가율 | 200% 이상 |
| 지정거래량 | 1만주 이상 |

## 매매 조건

- **매수**: 10분할 (1분 간격), 총 100만원
- **익절**: +30% (추세 확인 후)
- **손절**: -15% (즉시)
- **동시 보유**: 최대 5종목

## Stub 모드

API 키 없이도 실행 가능 — 모의 데이터로 로직 테스트.

## 설계 문서

- `ARCHITECTURE.md` — 시스템 아키텍처
- `ONTOLOGY.md` — 온톨로지 기반 지식 시스템
