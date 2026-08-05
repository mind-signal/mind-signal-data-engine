# AGENTS.md — Mind Signal Data Engine (Python)

모든 에이전트(Claude Code / Codex CLI / 기타 모델)가 이 프로젝트에서 작업할 때 반드시 읽어야 할 공통 지시. Claude 전용 메타는 `CLAUDE.md` 참조.

> 본 문서는 자가완결 — 이 레포 클론만으로 개발·검증·커밋이 가능해야 함. 상세는 `.agents/rules/*.md` 참조 (단방향: 본문이 1차 소스). 예외: 아래 "계획 산출물 위치" 절의 `.plans/` 정본 파일들은 상위 워크스페이스(`Team-project/mind-signal/.plans/`)에 있는 팀 작업 맥락 참조용이라 이 레포 클론에는 없을 수 있음 — 판단에 필요한 요지는 그 절 본문에 남겨둠.

---

## 1. 프로젝트 역할

두 경로가 있다. **실시간** — Emotiv App -> WebSocket(ws://localhost:6868) -> `core.main`(백엔드가 세션 시작 시 spawn) -> Redis pub/sub -> 백엔드 -> 프론트. **사후 분석** — `server/app.py`(FastAPI, 포트 5002)가 `POST /api/analyze`(백엔드가 HTTP로 호출)를 서빙, 세션 CSV를 읽어 통계와 Synchrony를 계산하고 요청 시 LLM용 Markdown으로 변환.

Emotiv App이 로컬에서 실행 중이어야 함 — 헤드셋 없이는 streamer가 동작하지 않는다.

## 2. 실행 명령어

```bash
conda activate mind-signal

# 실시간 파이프라인 (백엔드가 자동 spawn함, 직접 실행 형식은 아래와 같음)
python -m core.main <groupId> <subjectIndex>

# FastAPI 분석 서버 (별도 터미널)
python run_server.py
# 또는: uvicorn server.app:app --port 5002 --reload
```

`core/streamer.py`는 `__main__` 블록이 없다 — 직접 실행(`python -m core.streamer`)하면 즉시 종료됨. 진입점이 아니므로 반드시 `core.main` 경유.

## 3. 프로젝트 구조

```
mind-signal-data-engine/
├── core/         # 실시간 EEG 파이프라인 (analyzer / main / streamer)
├── server/       # 독립 FastAPI 서버 (app / config / routes / services)
├── sdk/          # 수정 금지 — Emotiv 제공 원본
├── tests/        # pytest
├── run_server.py
└── .env.local
```

## 4. 환경 변수

`.env.local`에는 `CLIENT_ID`, `CLIENT_SECRET`, `REDIS_HOST`, `REDIS_PORT`, `FASTAPI_PORT`, `USE_NGROK`, `BACKEND_URL`, `ENGINE_SECRET_KEY`가 필요함. Redis 채널 자체는 `core.main`의 위치 인수(groupId, subjectIndex)로 결정되며 환경변수와 무관.

머신마다 반복되는 환경 함정이 둘 있다: Cortex TLS 루트 인증서는 gitignore 대상이라 새 머신마다 손으로 복사해야 하고, `LAN_IP` 해석에는 Tailscale이 개입할 때 지켜야 할 우선순위가 있다. 둘 다 `.agents/rules/troubleshooting.md`에 상세 — "헤드셋은 찾았는데 세션이 즉시 끝남" 또는 "operator가 이 엔진에 도달 못함" 증상을 디버깅하기 전에 먼저 읽을 것.

## 5. 코드 스타일

Python 3.10, `mind-signal` conda 환경. `black .`로 포맷, `isort .`로 import 정렬, `flake8 .`로 린트(sdk/ 제외). 네이밍: 모듈/함수/변수는 `snake_case`, 클래스는 `PascalCase`, 진짜 불변인 모듈 레벨 상수는 `SCREAMING_SNAKE_CASE`. public 함수 시그니처에는 type hint 의무, `Optional[X]` 대신 Python 3.10 union `X | None` 사용.

주석은 public 함수/클래스에 Google Style Docstring을 따르고, inline/block 주석은 한국어 명사형 종결(`~함`, `~완료` 등)을 쓴다 — 이건 팀 관례로 의도적으로 한국어를 유지한다. 허용 종결 전체 목록과 예시는 `.agents/rules/code-style.md` 참조.

`sdk/`는 Emotiv 제공 원본 — 절대 수정 금지. PR에서 sdk/ 수정 발견 시 즉시 revert.

## 6. Redis 채널 계약

채널 키: `mind-signal:{groupId}:subject:{subjectIndex}` (세션별 동적).

- `groupId`: MongoDB ObjectId 문자열 (백엔드가 spawn 시 `sys.argv[1]`로 전달).
- `subjectIndex`: **1-based**, `1` 또는 `2`만 (백엔드가 `z.number().int().min(1).max(2)`로 검증; `sys.argv[2]`로 전달).
- 고정/전역 채널명 금지 — `mind-signal-live` 재도입 금지.
- 채널 키에 PC/host 정보 포함 금지 — IP/hostname 금지.

이 채널에는 두 메시지 타입이 실린다: `brain_sync_all`(주기적 wave/metrics payload)과 `headset_status`(연결 상태 경보 — `no_data`, `metrics_stale`, `disconnected`). 전체 JSON 형태와 소스 위치는 `.agents/rules/redis-contract.md`.

## 7. 검증 루프 — 커밋 전 의무

이 레포에는 아직 GitHub Actions CI가 없음. CodeRabbit AI 리뷰만 PR에 연결됨. 매 커밋 전 아래 4단계를 손으로 실행:

```bash
conda activate mind-signal
black .       # 포맷 자동 수정
isort .       # import 정렬 자동 수정
flake8 .      # PEP8 린트 (sdk/ 제외)
pytest        # 단위 테스트 (tests/)
```

순서 중요: black -> isort -> flake8 -> pytest. 한 단계라도 실패하면 수정 후 재실행, 전부 통과한 뒤에만 커밋. 근거 없이 `# noqa` 추가 금지, 실패 테스트를 삭제하거나 주석 처리해 통과로 위장 금지. 같은 단계 3회 연속 실패 시 사람에게 에스컬레이션. 상세: `.agents/rules/verification-loop.md`.

## 8. 커밋 컨벤션 — Conventional Commits 1.0

형식: `{type}({scope}): {description}`. 허용 type: `feat fix refactor style docs chore test perf ci revert`.

1 task = 1 commit. `main` 직접 커밋 금지 — `feat/{domain-wNNN}-{slug}`(Work ID 스타일, 예: `feat/analysis-w005-band-power-fix`)로 브랜치해 `dev`로 PR, 이후 `dev`를 `main`으로 PR.

모든 커밋 끝에 아래를 고정 작성(`noreply` 금지, Claude co-author 줄 금지):

```
Co-authored-by: KWONSEOK02 <gwonseok02@gmail.com>
```

상세: `.agents/rules/commit-conventions.md`.

## 9. 용어 정의

- **FAA (Frontal Alpha Asymmetry)** — 좌우 전두엽 알파파 비대칭, 접근/회피 감정 지표.
- **5대역 파워** — delta(0.5-4Hz), theta(4-8Hz), alpha(8-12Hz), beta(13-30Hz), gamma(30-45Hz).
- **RMS Power** — 각 대역 강도를 Root Mean Square(uV)로 표현한 값.
- **EmotivMetrics (MET)** — Emotiv 자체 산출 지표 6종(focus, engagement, interest, excitement, stress, relaxation).
- **Cortex API** — Emotiv 헤드셋 WebSocket(wss://localhost:6868) JSON-RPC 인터페이스.

대역 파워 계산에는 위반 시 실제 버그로 이어졌던 비자명한 계약이 여럿 있다(필터뱅크 대신 PSD 합산, 사다리꼴 대신 직사각형 적분, 최소 창 길이, DC 제거, Synchrony 방식 등). `core/analyzer.py`나 대역 파워·Synchrony·Friendship Score를 계산하는 코드를 건드리기 전에 전체 계약을 반드시 읽을 것: `.agents/rules/analysis-contract.md`.

## 10. 트러블슈팅 (빠른 참조)

- **Cortex 연결 오류**: Emotiv App 실행 확인, `CLIENT_ID`/`CLIENT_SECRET`이 `.env.local`에 있는지 확인, Emotiv App 자체가 계정에 로그인돼 있는지 확인.
- **Redis 연결 오류**: `cd ../mind-signal-backend && docker-compose up -d`.
- **패키지 오류**: `conda activate mind-signal && pip install -r requirements.txt`. 참고: `requirements.txt`의 `packaging` 항목이 원본 머신에 남아있던 로컬 파일 경로(`file:///C:/miniconda3/conda-bld/...`)로 고정돼 있어서 새 클론에서는 해석 실패함. `pip freeze`로 재생성하거나 그 줄을 지우고 pip가 `packaging`을 정상 해석하게 둘 것.
- **Python 경로**: `conda activate mind-signal`을 먼저 안 하면 시스템 Python(3.13/3.9)이 잡혀 의존성이 깨짐.

상세 증상(헤드셋 즉시종료, cross-machine 엔진 도달 불가 등): `.agents/rules/troubleshooting.md`.

---

## 계획 산출물 위치 (2026-07-31 갱신, DOCS-W005)

mind-signal은 4레포 제품이므로 `.plans/`는 제품 단위로 딱 하나, `Team-project/mind-signal/.plans/`에 둔다. **이 정본 파일들은 이 레포 밖(상위 워크스페이스)에 있으므로, 이 레포만 클론한 환경에서는 접근할 수 없을 수 있다** — 그런 환경에서는 아래 요지만으로 판단하고, 실제 `.plans/` 조작이 필요하면 상위 워크스페이스 접근이 별도로 필요함을 알 것. 이 레포의 로컬 `.plans/`(git 밖)는 세션 로그(`_logs/`)와 임시 조사(`_quick/`)만 담는다 — 여기에 작업 폴더를 새로 만들지 말 것.

`Team-project/.plans/`(루트)와 혼동하지 말 것 — 그쪽은 2026-07-30 통합 이후 크로스 프로젝트 메타 전용이고, 폴더에 번호가 붙어 있어도 MindSignal 계획은 없다.

판단에 필요한 요지(정본 파일에 접근 못 해도 알아야 할 것): 작업 폴더명은 `{WORK-ID}[-{slug}]` 형식이고 Work ID는 도메인(`ANALYSIS`, `EEG`, `SESSION`, `OPS`, `DOCS`)별 독립 채번이며 전역 순번이 아니다. 신규는 W001부터, 소급 부여분은 W101부터 시작한다(예: `SESSION-W114`는 전체 114번째가 아니라 SESSION 소급 14번째). 번호는 영구 식별자라 재사용하지 않으므로 번호 공백은 오류가 아니다. 작업 상태의 정본은 `DASHBOARD.md`다.

- 현재 작업 정본: `mind-signal/.plans/DASHBOARD.md`
- 세션 핸드오프 정본: `mind-signal/.plans/HANDOFF.md` (대체 시 `_archive/HANDOFF-YYYYMMDD.md`)
- ID·상태 규칙 정본: `mind-signal/.plans/README.md` (v1.3, **LOCK**)
- 소급 Work ID 매핑: `mind-signal/.plans/LEGACY-REGISTRY.md`
- 작업 폴더: `mind-signal/.plans/{WORK-ID}[-{slug}]` (예: `ANALYSIS-W001-eeg-dc-offset-removal`)
- 상태 서술: `mind-signal/.plans/STATE.md`
- `docs/`는 외부 전달물 전용

### Work ID 읽는 법 (흔한 오독)

폴더 접두사는 `{domain}-W{NNN}`이며 **도메인별로 채번**한다(`ANALYSIS`, `EEG`, `SESSION`, `OPS`, `DOCS`) — 전역 순번이 아니다. 신규는 W001부터, 소급은 W101부터 시작하므로 `SESSION-W114`는 전체 114번째가 아니라 소급 14번째 SESSION 항목이다. 번호는 영구적이며 재사용되지 않는다 — 공백은 오류가 아니라 역사다. `project-flow` 스킬의 범용 `{NN}-{feature-name}` 규칙은 여기 적용되지 않는다 — `.plans/README.md`의 로컬 선언이 우선한다.
