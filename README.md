# mind-signal-data-engine

## 1. 프로젝트 개요 (Project Overview)

**Mind Signal Data Engine**은 EEG(뇌파) 기반 2인 심리 동기화 측정 · 분석 서비스의 **Python 엔진**입니다.
Emotiv Cortex API로 실시간 EEG를 수신해 Redis Pub/Sub으로 백엔드에 스트리밍하고, FastAPI 독립 서버로 사후 분석 API를 제공합니다.

### 핵심 파이프라인

```
[실시간 EEG]
Emotiv App (로컬 실행 필수)
    ↓ WebSocket (ws://localhost:6868)
core.main (백엔드가 세션 시작 시 spawn) ──→ Redis pub/sub ──→ 백엔드 ──→ 프론트

[사후 분석]
server/app.py (FastAPI, 포트 5002)
    ├─ POST /api/analyze ← 백엔드가 HTTP 프록시로 호출
    ├─ CSV 읽기 + 통계 계산 + Synchrony
    └─ LLM용 Markdown 변환 (요청 시)
```

**전제 조건**: Emotiv App이 로컬에서 반드시 실행 중이어야 합니다. 헤드셋 없이는 streamer가 동작하지 않습니다.

---

## 2. Tech Stack

| 구분 | 기술 |
| :--- | :--- |
| **Language** | `Python 3.10` |
| **Environment** | `Conda` 가상 환경 `mind-signal` |
| **Framework** | `FastAPI`, `uvicorn` (분석 서버) |
| **Real-time** | `Emotiv Cortex API` (WebSocket), `Redis Pub/Sub` |
| **Config** | `pydantic-settings` |
| **Tunnel** | `pyngrok` (ngrok 자동 등록) |
| **BLE (실험)** | `bleak` (BLE 직접 연결 테스트용) |
| **Test** | `pytest` |
| **Quality** | `black`, `isort`, `flake8` |
| **SDK** | Emotiv 제공 `sdk/` (수정 금지) |

---

## 3. 프로젝트 실행 방법 (Getting Started)

### 요구사항

- `Python 3.10` + `Conda` (Miniconda/Anaconda)
- Emotiv App 설치 및 로컬 실행
- Emotiv 계정으로 Cortex API CLIENT_ID / CLIENT_SECRET 발급
- Redis (로컬 Docker — 백엔드 레포 `mind-signal-backend`의 `docker-compose.yml` 사용)

### 1. 저장소 복제 (Clone)

```bash
git clone https://github.com/KWONSEOK02/mind-signal-data-engine.git
cd mind-signal-data-engine
```

### 2. 로컬 가상 환경 설정 (Conda)

#### Python 3.10으로 `mind-signal` 환경 생성

```bash
conda create -n mind-signal python=3.10
conda activate mind-signal
```

#### 의존성 설치

```bash
pip install -r requirements.txt
```

### 3. 환경 변수 설정

`.env.example`을 복사해서 `.env.local` 파일을 생성한 뒤 Emotiv CLIENT_ID / CLIENT_SECRET 등을 채웁니다.

```bash
cp .env.example .env.local
```

### 4. 실행 명령어

```bash
# FastAPI 분석 서버 실행 (포트 5002)
python run_server.py
# 또는: uvicorn server.app:app --port 5002 --reload

# 실시간 EEG 스트리머 실행 (백엔드가 세션 시작 시 spawn — 직접 실행 시 인수 필요)
python -m core.main <groupId> <subjectIndex>

# 독립 스트리머 (참고 — __main__ 블록 없어 즉시 종료됨, 백엔드 spawn 용도)
python -m core.streamer
```

---

## 4. 코드 스타일 관리 (Lint & Format)

협업 시 동일한 코딩 스타일을 유지하기 위해 아래 명령어를 권장합니다.

### 코드 자동 정렬 (Formatter)

```bash
# Black (포맷팅)
black .

# isort (임포트 정렬)
isort .
```

### 코드 스타일 검사 (Linter)

```bash
# PEP8 표준 준수 여부 및 잠재적 에러 확인
flake8 .
```

### 테스트

```bash
pytest
```

**주의**: 반드시 `conda activate mind-signal`로 환경을 먼저 활성화한 뒤 실행합니다. 시스템 Python으로 실행하면 의존성이 깨집니다.

---

## 5. 프로젝트 구조

```
mind-signal-data-engine/
├── core/                    # 실시간 EEG 파이프라인
│   ├── analyzer.py          # DSP 알고리즘 (FAA, 5대역 파워 등 — FastAPI에서도 재사용)
│   ├── main.py              # 백엔드 spawn 진입점 (groupId, subjectIndex 위치 인수)
│   └── streamer.py          # Redis pub/sub 스트리머
├── server/                  # FastAPI 독립 서버
│   ├── app.py               # FastAPI 앱 + lifespan (ngrok + webhook 자동 등록)
│   ├── config.py            # pydantic-settings 환경변수
│   ├── routes/              # analyze, health, export, stream, control, logs 라우트
│   └── services/            # analysis, markdown, webhook, friendship, score_params 등 서비스
├── sdk/                     # ⚠️ 수정 금지 (Emotiv 제공 원본)
│   ├── cortex.py            # 핵심 통신 라이브러리
│   ├── marker.py            # 마커 로직 참고용
│   ├── record.py            # 녹화 로직 참고용
│   └── sub_data.py          # 데이터 구독 참고용
├── tests/                   # pytest 단위 테스트
├── scripts/                 # 진단 · E2E 스크립트 (BLE 테스트 등)
├── certificates/            # Emotiv 연결용 SSL 인증서 (Git 추적 제외, 신규 머신마다 수동 배치 필요)
├── docs/                    # 레포 로컬 보조 문서
├── AGENTS.md                # 에이전트 공통 지시 (정본)
├── CLAUDE.md                # AGENTS.md + .agents/rules import
├── .env.example             # 환경 변수 가이드 (Git 추적)
├── .env.local               # CLIENT_ID, CLIENT_SECRET 등 (Git 추적 제외)
├── .flake8                  # PEP8 검사 설정 (sdk/ 제외)
├── .gitignore               # 제외 목록
├── pyproject.toml           # Black · isort 통합 설정
├── run_server.py            # uvicorn 실행 스크립트
├── requirements.txt         # 의존성 목록
└── README.md                # 프로젝트 설명서
```

### 파일별 핵심 역할 요약 (팀원 공유용)

| 파일명 | 핵심 역할 |
| :--- | :--- |
| **`.flake8`** | **ESLint의 파이썬 버전**입니다. 코드 가독성을 해치는 요소나 사용하지 않는 변수 등을 잡아내며, 수정하면 안 되는 `sdk/` 폴더는 무시하도록 설정되어 있습니다. |
| **`pyproject.toml`** | **Prettier의 파이썬 설정**과 같습니다. `Black`이 코드를 어떻게 예쁘게 정렬할지, `isort`가 상단 `import`문을 어떤 순서로 배치할지 정의합니다. |

---

## 6. 협업 가이드라인 (Contribution Guidelines)

### Git Workflow

- `main` (Production): 최종 배포 브랜치 — 직접 push 금지. `dev`에서만 PR 올림
- `dev` (Staging): 개발 통합 브랜치 — 모든 `feat/*` 기능 브랜치의 PR 대상
- `feat/#{이슈번호}-{작업명}`: 이슈 기반 기능 브랜치
- `fix/#{이슈번호}-{작업명}`: 이슈 기반 버그 수정 브랜치
- `docs/#{이슈번호}-{작업명}`: 문서 작업 브랜치
- `refactor/#{이슈번호}-{작업명}`, `chore/#{이슈번호}-{작업명}`: 그 외 목적별 브랜치

### 작업 흐름 (모든 변경은 이슈 기반)

모든 코드 변경은 반드시 **GitHub Issue를 먼저 생성**한 뒤 진행합니다. **`main` 직접 commit은 금지**이며, `dev` 직접 commit도 원칙적으로 금지합니다. 오타·로컬 세팅·사소한 문서 수정도 예외 없이 이슈 → 브랜치 → PR 절차를 따릅니다.

1. **Issue 생성**: GitHub Issues → New Issue → 템플릿 선택 후 작업 내용 등록 (제목: `feat: 작업 내용`)
2. **브랜치 생성**: 이슈 페이지 Development → Create a branch → **base를 `dev`로 설정** → `타입/#{이슈번호}-{작업명}` 형식
3. **개발**: 기능 구현. 커밋 전 로컬 검증(§7) 통과 필수
4. **PR**: **base를 `dev`로 설정**하여 PR 생성 (main 아님). Reviewers / Assignees / Labels 지정
5. **코드리뷰**: 팀원 1명 이상의 Approve + CodeRabbit 리뷰 확인
6. **머지**: 승인 완료 후 `feat/*` → `dev` 머지
7. **Issue Close**: 머지 직후 해당 이슈 close
8. **릴리스**: `dev`가 안정화되면 `dev` → `main` PR을 별도 생성, CodeRabbit 리뷰 통과 후 머지

### 프로젝트 규칙

- **PR은 작은 단위로.** 하나의 PR은 하나의 이슈 · 하나의 기능에만 집중합니다.
- 세부 작업은 이슈 체크리스트로 관리합니다.
- 머지 직전 `dev` 최신 변경 사항을 `pull` 하여 충돌을 최소화합니다.

### 개발 가이드라인

- 코딩 스타일: **PEP8 준수**, `flake8` 검사 통과 필수
- 함수·클래스 주석: **Google Style Docstring**
- 주석 종결 어미: 명사형 (`~함`, `~사용`, `~완료`, `~처리`)
- 모든 팀원은 동일한 `requirements.txt` 환경에서 개발합니다.
- **`sdk/` 폴더는 절대 수정 금지** — Emotiv 제공 원본 코드

---

## 7. CI 파이프라인 & AI 코드 리뷰

이 레포에는 현재 GitHub Actions CI가 없습니다. **로컬 검증 + CodeRabbit AI 리뷰** 조합으로 품질을 유지합니다.

```
  PR 생성
     ↓
┌─── 로컬 검증 (커밋 전 필수) ───────────────────┐
│ 1. black .        → 포맷 자동 정렬            │
│ 2. isort .        → import 정렬               │  ← 커밋 전
│ 3. flake8 .       → PEP8 린트 (sdk/ 제외)     │     통과 필수
│ 4. pytest         → 단위 테스트               │
└────────────────────────────────────────────────┘
     ↓ PR push
┌─── CodeRabbit AI 리뷰 ────────────────────────┐
│ • 로직 / 성능 / 보안 / 테스트 커버리지        │
│ • SDK 수정 여부 / 환경변수 하드코딩           │
└────────────────────────────────────────────────┘
```

### 로컬에서 잡아야 하는 것

| 도구 | 검증 항목 |
| :--- | :--- |
| **Black** | 포맷, 들여쓰기, 줄바꿈 |
| **isort** | import 순서 (표준 라이브러리 → 서드파티 → 로컬) |
| **flake8** | PEP8, 사용하지 않는 변수, 길이 초과 |
| **pytest** | 단위 테스트 (Cortex 연결 없이 동작하는 범위) |

### PR 전 로컬에서 확인하는 법

```bash
conda activate mind-signal
black .
isort .
flake8 .
pytest
```

순서: `black → isort → flake8 → pytest` — 한 단계라도 실패하면 멈추고 수정 후 재실행.

> 향후 GitHub Actions 도입 시 위 4단계를 그대로 `.github/workflows/ci.yml`로 옮길 예정입니다.

---

## 8. 커밋 메시지 컨벤션

**Conventional Commits** 규칙을 따릅니다. Gitmoji 이모지는 사용하지 않습니다.

### 형식

```
{type}({scope}): {description}
```

예시:

```
feat(streamer): add per-subject Redis channel keying
fix(analyzer): correct FAA calculation for right-handed subjects
perf(streamer): reduce pub/sub latency with batch publish
test(analyzer): cover empty CSV edge case
```

### 타입 목록

| 타입 | 용도 |
|------|------|
| feat | 새 기능 |
| fix | 버그 수정 |
| refactor | 리팩토링 (기능 변경 없는 구조 개선) |
| style | 포맷·공백 (로직 변경 없음) |
| docs | 문서 변경 |
| chore | 빌드·설정·패키지 |
| test | 테스트 추가·수정 |
| perf | 성능 개선 |
| ci | CI 설정 |
| revert | 이전 커밋 되돌리기 |

- 태스크 1개 = 커밋 1개
- `main` 브랜치 직접 commit 금지 — 반드시 `feat/#{이슈번호}-{작업명}` 브랜치에서 작업 후 `dev`로 PR

---

## 9. 브랜치 네이밍 컨벤션

```
{타입}/#{이슈번호}-{작업명}
```

예시:

- `feat/#14-sequential-analysis-pipeline`
- `fix/#27-faa-calculation-error`
- `docs/#31-readme-structure-update`
- `refactor/#22-analyzer-split-dsp`
- `chore/#18-upgrade-pydantic-settings`

이슈에서 "Create a branch"로 자동 생성할 때 base branch는 항상 `dev`로 설정합니다.

---

## 참고 자료

- [Emotiv Cortex API Python 공식 예제 저장소](https://github.com/Emotiv/cortex-example/tree/master/python)
- [Emotiv Cortex API 문서](https://emotiv.gitbook.io/cortex-api)
