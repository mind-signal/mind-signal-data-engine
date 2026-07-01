# AGENTS.md — Mind Signal Data Engine (Python)

모든 에이전트(Claude Code / Codex CLI / 기타 모델)가 이 프로젝트에서 작업할 때 반드시 읽어야 할 공통 지시. Claude 전용 메타는 `CLAUDE.md` 참조.

> 본 문서는 자가완결 — 외부 import 없이 본문만 읽고도 동작 가능해야 함. 상세 확장은 `.agents/rules/*.md` 참조 (단방향: 본문이 1차 소스).

---

## 1. 프로젝트 역할

Emotiv 헤드셋에서 EEG 데이터를 받아 **Redis로 스트리밍**(`core.main`)하고, **FastAPI 서버**(`server/app.py`)로 사후 분석 API를 제공하는 Python 엔진.

```
[실시간 EEG]
Emotiv App (로컬 실행 필수)
    ↓ WebSocket (ws://localhost:6868)
core.main (백엔드가 세션 시작 시 spawn) → Redis pub/sub → 백엔드 → 프론트

[사후 분석]
server/app.py (FastAPI, 포트 5002)
    ├─ POST /api/analyze ← 백엔드가 HTTP 프록시로 호출
    ├─ CSV 읽기 + 통계 계산 + Synchrony
    └─ LLM용 Markdown 변환 (요청 시)
```

> ⚠️ Emotiv App이 로컬에서 실행 중이어야 함. 헤드셋 없이는 streamer 동작 안 함.

---

## 2. 실행 명령어

```bash
# conda 환경 활성화 (반드시 먼저)
conda activate mind-signal

# 백엔드 연동 실행 (백엔드가 자동 호출, 직접 실행은 아래 형식)
python -m core.main <groupId> <subjectIndex>

# FastAPI 분석 서버 (별도 터미널)
python run_server.py
# 또는: uvicorn server.app:app --port 5002 --reload
```

> `core.streamer`는 `__main__` 블록이 없어 모듈로 실행해도 즉시 종료 — 진입점으로 사용 불가, 반드시 `core.main` 경유.

---

## 3. 프로젝트 구조

```
mind-signal-data-engine/
├── core/         ← 실시간 EEG 파이프라인 (analyzer / main / streamer)
├── server/       ← FastAPI 독립 서버 (app / config / routes / services)
├── sdk/          ← ⚠️ 수정 금지 (Emotiv 제공 원본)
├── tests/        ← pytest 테스트
├── run_server.py ← uvicorn 실행 스크립트
└── .env.local
```

---

## 4. 환경 변수 (.env.local 필요)

```env
CLIENT_ID=<emotiv_client_id>
CLIENT_SECRET=<emotiv_client_secret>
REDIS_HOST=localhost
REDIS_PORT=6379

# FastAPI 서버
FASTAPI_PORT=5002
USE_NGROK=false
BACKEND_URL=http://localhost:5000
ENGINE_SECRET_KEY=<shared_secret>
```

Redis 채널은 `core.main`의 인수(groupId, subjectIndex)로 동적 결정 — 환경변수 불필요.

### Cortex TLS 인증서 — `certificates/rootCA.pem` 로컬 필수

`sdk/cortex.py`가 Cortex wss 연결의 TLS CA로 레포 루트의 `certificates/rootCA.pem`을 읽음. 이 파일은 gitignore라 git에 안 올라오므로 **새 머신마다 로컬에 직접 둬야 함**. 누락 시 `core.main`이 `SSL CA certificate loading failed: [Errno 2] No such file or directory` 출력 후 0.0초 만에 returncode 0으로 종료함(헤드셋은 정상 발견돼도 측정 0초). 기존 Emotiv 설치 경로의 인증서를 복사해 채움. 2026-06-28 노트북 B 라이브 연결 검증 중 D8(즉시종료) 근본 원인으로 확인됨.

### Phase migration 환경변수 cleanup 룰

Phase migration 진행 시 `.env.local` 또는 `.env.example`에 박제된 환경변수 중 이전 Phase 전용으로 남아 있는 값은 cleanup 의무. 5/26 D-0 시연 setup 도중 노출된 사례:

- Phase 17 `REGISTRATION_MODE=ngrok` 잔재 — Phase 18 proxy mode 환경에서 사용 시 `public_url`을 proxy `/register`로 보낼 때 ngrok URL 등록 risk 발생함. proxy mode 환경에선 `REGISTRATION_MODE=local` 의무함.
- Phase 18 `ALIGNMENT_LOCATION` + `PROXY_URL` 신규 추가 — proxy mode 활성화 트리거. `.env.example`에 빈 값 default 박제로 proxy mode 미사용 시 자연 비활성화 정합함.
- `LAN_IP` — DE가 proxy에 광고할 자기 IP. **Tailscale이 유일 transport로 pivot**(2026-06-30, 핫스팟/D-0 스크립트 아카이브)한 뒤로는 보통 비워둠. 해석 우선순위: 명시 `LAN_IP`(런처가 `tailscale ip -4`로 주입) > DE 자체 Tailscale 대역(100.64.0.0/10) 자동탐지(`server/app.py` `_detect_tailscale_ip`) > `socket.gethostbyname` 폴백. 과거 `socket.gethostbyname`은 Docker/WSL/Wi-Fi 어댑터를 오선택해 cross-machine 도달 불가 주소를 광고하는 결함이 있었음(노트북 B가 LAN IP `10.26.x`를 등록해 operator assign-group 타임아웃). 수동 지정 시 **반드시 그 머신의 Tailscale IP(100.x)** — LAN/Wi-Fi IP 금지. (핫스팟 primary였던 5/26 전략은 Tailscale primary로 대체됨.)

Phase migration PR scope에 `.env.example` 정합 확인 + AGENTS.md 본 절 amend 의무 (transitive 상속 박제 갭 차단). 옵시디언 [[2026-05-26-phase-18.1-d-0-hotspot-pivot-postponed]] 핵심 발견 6 + [[2026-05-26-track-1-doc-governance-correction-done]] 핵심 발견 2 정합.

---

## 5. 코드 스타일

- **Python 3.10**, Conda 가상환경 `mind-signal`.
- Conda Python 경로: `C:\Users\gs071\.conda\envs\mind-signal\python.exe`. 시스템 Python(3.13/3.9)으로 실행 금지 — 의존성 깨짐.
- 포맷터: `black .` (라인 88, PEP8 호환).
- import 정렬: `isort .` (black 호환 프로파일).
- 린터: `flake8 .` (`sdk/` 제외, `.flake8` 설정).
- 네이밍: 모듈/함수/변수 = `snake_case`, 클래스 = `PascalCase`, 모듈 상수 = `SCREAMING_SNAKE_CASE`.
- Type hints: public 함수 시그니처 의무. Python 3.10 union `X | None` 사용 (`Optional[X]` 대신).
- **`sdk/` 폴더 수정 금지** — Emotiv 제공 원본. PR에서 sdk/ 수정 발견 시 즉시 revert.

**주석 — Google Style Docstring + 한국어 명사형 종결 의무**:

```python
# ✅ EEG 데이터 Redis로 전송함
# ✅ 환경변수 로드 완료
# ✅ FAA 계산 수행함

# ❌ EEG 데이터를 Redis로 전송합니다
# ❌ 환경변수를 로드하는 함수
```

허용 종결: `~함`, `~사용`, `~완료`, `~임`, `~반환`, `~생성`, `~처리`.

상세: `.agents/rules/code-style.md`.

---

## 6. Redis 채널 계약

채널 키: `mind-signal:{groupId}:subject:{subjectIndex}` (세션별 동적).

- `groupId`: MongoDB ObjectId 문자열 (백엔드가 spawn 시 `sys.argv[1]`로 전달).
- `subjectIndex`: 0-based 정수 (`sys.argv[2]`).
- **고정 채널명 사용 금지** — `mind-signal-live` 같은 글로벌 채널 재도입 금지.
- **PC/host 정보 포함 금지** — 채널 키에 IP/hostname 삽입 금지.

발행 메시지 포맷:

```json
{
  "type": "brain_sync_all",
  "groupId": "<string>",
  "subjectIndex": "<int>",
  "waves": {"delta": 0.0, "theta": 0.0, "alpha": 0.0, "beta": 0.0, "gamma": 0.0},
  "metrics": {"focus": 0.0, "engagement": 0.0, "interest": 0.0, "excitement": 0.0, "stress": 0.0, "relaxation": 0.0},
  "time": "2025-01-01 12:00:00.000000"
}
```

출처: `core/streamer.py` 193~201줄의 payload 구조와 일치.

진입점 변경 이력(`feat/session-group-pairing` @ `73f5e36`):

| 항목 | 이전 | 현재 |
|------|------|------|
| 진입점 | `core.streamer` 상시 실행 | `core.main` 세션별 spawn |
| 채널 | `mind-signal-live` 고정 | `mind-signal:{groupId}:subject:{subjectIndex}` 동적 |
| 인수 | 없음 | `sys.argv[1]=groupId`, `sys.argv[2]=subjectIndex` |

이유: 동시 다중 세션 지원. 상시 실행 방식은 단일 Python 프로세스가 모든 세션을 처리해야 동시 측정 불가.

상세: `.agents/rules/redis-contract.md`.

---

## 7. 검증 루프 — 커밋 전 의무

GitHub Actions CI는 현재 이 레포에 없음. CodeRabbit AI 리뷰만 PR 자동 연결. 아래 4단계를 **커밋 전 수동으로** 실행:

```bash
conda activate mind-signal
black .       # 포맷 자동 수정
isort .       # import 정렬 자동 수정
flake8 .      # PEP8 린트 (sdk/ 제외)
pytest        # 단위 테스트 (tests/ 디렉토리)
```

**순서 중요**: black → isort → flake8 → pytest. 한 단계라도 실패 시 수정 후 재실행, 전부 통과한 뒤에만 커밋·push.

- `# noqa`/`# flake8: noqa` 인용 근거 없이 추가 금지.
- 실패 테스트 삭제·주석 처리로 통과 위장 금지.
- 동일 단계 3회 연속 실패 시 사람에게 에스컬레이션.

상세: `.agents/rules/verification-loop.md`.

---

## 8. 커밋 컨벤션 — Conventional Commits 1.0

형식: `{type}({scope}): {description}`.

허용 type: `feat` `fix` `refactor` `style` `docs` `chore` `test` `perf` `ci` `revert`.

예:
```
feat(streamer): add per-subject Redis channel keying
fix(analyzer): correct FAA calculation for right-handed subjects
perf(streamer): reduce pub/sub latency with batch publish
test(analyzer): cover empty CSV edge case
```

**1 task = 1 commit**. `main` 직접 commit 금지 — `feat/#이슈번호-작업내용` → PR → `dev` → PR → `main`.

**Co-authored-by 의무** (`gwonseok02@gmail.com` 고정, `noreply` 금지, Claude Co-Authored-By 추가 금지):

```
Co-authored-by: KWONSEOK02 <gwonseok02@gmail.com>
```

상세: `.agents/rules/commit-conventions.md`.

---

## 9. 용어 정의

- **FAA (Frontal Alpha Asymmetry)** — 좌우 전두엽 알파파 비대칭, 감정 접근/회피 지표
- **5대역 파워** — delta(0.5-4Hz), theta(4-8Hz), alpha(8-12Hz), beta(13-30Hz), gamma(30-45Hz)
- **RMS Power** — 필터링된 신호의 Root Mean Square, 각 대역 강도
- **Synchrony** — 두 피실험자 간 뇌파 상관계수 (Pearson correlation)
- **EmotivMetrics (MET)** — Emotiv 자체 산출 지표 6종 (focus, engagement, interest, excitement, stress, relaxation)
- **Cortex API** — Emotiv 헤드셋과 WebSocket(wss://localhost:6868) JSON-RPC 인터페이스

---

## 10. 트러블슈팅 (빠른 참조)

- **Cortex 연결 오류**: Emotiv App 실행 / `CLIENT_ID`·`CLIENT_SECRET` `.env.local` 확인 / Emotiv 계정 로그인 확인
- **Redis 연결 오류**: `cd ../mind-signal-backend && docker-compose up -d`
- **패키지 오류**: `conda activate mind-signal && pip install -r requirements.txt --break-system-packages`
- **Python 경로**: conda 활성화 미선행 시 시스템 Python(3.13/3.9) 잡혀 의존성 깨짐 — 반드시 `conda activate mind-signal` 먼저

상세: `.agents/rules/troubleshooting.md`.
