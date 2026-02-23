# 🤖 Nanobot 컨텍스트 문서
> 이 문서는 AI 어시스턴트(nanobot)가 프로젝트를 파악하고 이어서 작업할 수 있도록 작성된 인수인계 문서입니다.
> 마지막 업데이트: 2026-02-23

---

## 👤 계정 정보

| 항목 | 값 |
|------|-----|
| GitHub 계정 | `sungli01` |
| 텔레그램 Chat ID | `****` (secrets 참조) |
| Railway 계정 | sungli01 연동 |

---

## 🔑 API 키 / 자격증명 목록 (실제값은 별도 보관)

| 서비스 | 키 이름 | 상태 | 비고 |
|--------|---------|------|------|
| GitHub | `GITHUB_PAT` | ✅ 유효 | `ghp_****` |
| KIS (한국투자증권) | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT` | ⚠️ 토큰 만료됨 | 봇 재시작 시 자동 재발급 |
| Polygon.io | `POLYGON_API_KEY` | 확인 필요 | 미국주식 데이터 |
| Telegram Bot | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | ✅ 유효 | stock-bot 알림용 |
| Anthropic | `ANTHROPIC_API_KEY` | ⚠️ 재발급 필요 | 이전 세션에서 노출됨 |
| Railway | `RAILWAY_TOKEN` | 미등록 | nanobot에 미제공 |

> ⚠️ **실제 키값은 절대 이 문서에 기재하지 않습니다.**
> 실제값은 Railway 환경변수 또는 로컬 `.env` 파일에서 관리합니다.

---

## 📦 프로젝트 현황

### 1. 📈 stock-bot (비공개) — 최우선 관리
- **설명**: AI 미국주식 자동매매 시스템
- **현재 버전**: v10.4 (BudgetLearner)
- **배포**: Railway + Docker
- **실행 명령**: `FORCE_STANDALONE=true python3 main.py`
- **운영 시간**: KST 18:00 ~ 06:00 (미국장)
- **전략**: 페니스탁 급등 스캘핑 (1차→2차→3차 진입)
- **외부 API**: KIS API + Polygon.io + Telegram

#### 현재 파라미터 (v10.4)
| 항목 | 값 |
|------|-----|
| 매수 범위 | $0.7 ~ $30 |
| 1차 트리거 | +10% + Vol 800%+ |
| 2차 트리거 | +10% + Vol 300%+ |
| 3차 트리거 | +5% + Vol 300%+ |
| 손절 | -15% |
| 최대 보유 | 90분 |
| 복리 상한 | 2,500만원 |

#### 버전 히스토리 요약
| 버전 | 날짜 | 주요 변경 | 시뮬 성과 |
|------|------|-----------|-----------|
| v1~v4 | 02.17 | 기본 구조, ML/LSTM, KIS REST API 연동 | - |
| v5~v8 | 02.18 | 급등 스캘핑 전략, 동적 트레일링, 3분봉 모멘텀 | - |
| v8.1~v8.5 | 02.19 | ETF 필터, 거래량 버그 수정, 온톨로지 학습 | - |
| v9 | 02.20 | 1차/2차 상승 엔진, 손절 -15% | - |
| v10.0 | 02.21 | 최적 파라미터 | +739% |
| v10.1 | 02.21 | 1차/2차 트레일링 분리 | +3,253% |
| v10.2 | 02.21 | 2차 vol spike 300% | +5,837% |
| v10.3 | 02.21 | 3차 파라미터 완전 분리 | +16,812% |
| v10.4 | 02.21 | BudgetLearner 추가 | - |

#### ⚠️ 현재 이슈
- KIS 토큰 만료 (2026-02-19 22:25 KST) → 봇 재시작 필요
- Railway 실시간 로그 확인을 위해 Railway Token nanobot에 제공 필요

---

### 2. 🐪 camel-seller (비공개)
- **설명**: CAMT 스텔스 매도봇
- **구조**: `seller.py` 단일 파일
- **배포**: Railway
- **상태**: ✅ 배포 완료, 운영 중
- **특이사항**: 잔고 0 오판 방지 (3회 연속 확인 로직)

---

### 3. 📋 plan-crft-MVP (공개)
- **설명**: AI 기반 사업계획서 생성 서비스
- **기술**: TypeScript, Next.js
- **구조**: 모노레포 (backend / frontend-v1 / frontend-v2 / v2 / v3 / web)
- **상태**: ✅ 활성 개발 중 (v3까지 진행)

---

### 4. 🗺️ Traver_AI (공개)
- **설명**: Claude 기반 AI 여행 일정 자동화
- **기술**: TypeScript, 모노레포
- **배포**: Railway (백엔드) + Vercel (프론트엔드)
- **최근 이슈 해결**: SSE 스트리밍으로 Railway 30초 타임아웃 해결 (02.20)
- **상태**: ✅ 활성 개발 중

---

### 5. 🏗️ plan-craft (공개)
- **설명**: AI Autonomous Full-Stack Development Engine v8.0
- **기술**: JavaScript, Vite, Cloudflare Workers
- **상태**: ⏸️ 개발 중단 (plan-crft-MVP로 이전)

---

### 6. ✈️ Travelagent (공개)
- **상태**: ❌ 미개발 (Traver_AI로 이전)

---

## 🛠️ 기술 스택 전체 요약

| 분류 | 기술 |
|------|------|
| 언어 | Python, TypeScript, JavaScript |
| 프레임워크 | Next.js 14, Express, FastAPI |
| 배포 | Railway, Vercel, Cloudflare Workers, Docker |
| AI | Claude (Anthropic), Genspark AI |
| 외부 API | KIS API, Polygon.io, Telegram Bot API |
| 데이터 | 파일 기반 (standalone), Railway Volume |

---

## 📝 nanobot 작업 이력

### 2026-02-23
- GitHub 전체 저장소 분석 완료
- stock-bot v10.4 현황 파악
- KIS 토큰 만료 이슈 발견
- 이 문서(NANOBOT_CONTEXT.md) 작성 및 GitHub 업로드

---

## 🔜 다음 할 일 (TODO)

- [ ] Railway Token nanobot에 등록 → 실시간 로그 모니터링
- [ ] KIS 토큰 만료 → stock-bot Railway 재시작
- [ ] Anthropic API 키 재발급 (이전 세션 노출)
- [ ] stock-bot v10.4 실전 성과 모니터링
- [ ] plan-crft-MVP v3 개발 현황 파악

---

> 📌 이 문서를 읽는 AI 어시스턴트에게:
> - 민감 정보(API 키 등)는 절대 대화에 노출하지 말 것
> - 실제 키값은 Railway 환경변수 또는 사용자에게 직접 요청할 것
> - 작업 완료 후 이 문서의 TODO와 작업 이력을 업데이트할 것
