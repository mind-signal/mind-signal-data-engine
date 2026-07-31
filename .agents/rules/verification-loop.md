# Verification Loop Rules — Mind Signal Data Engine (Python)

## 4-단계 파이프라인

GitHub Actions CI는 현재 이 레포에 없음. CodeRabbit AI 리뷰만 PR에 자동 연결되므로 아래 4단계를 **커밋 전 수동으로** 실행함.

```bash
conda activate mind-signal
black .               # 포맷 자동 수정
isort .               # import 정렬 자동 수정
flake8 .              # PEP8 린트 (sdk/ 제외)
pytest                # 단위 테스트 (tests/ 디렉토리)
```

**순서 중요**: black → isort → flake8 → pytest. 한 단계라도 실패 시 수정 후 재실행, 전부 통과한 뒤에만 커밋·push함.

## Conda 환경 전제

`conda activate mind-signal`이 먼저임. 시스템 Python으로 실행하면 의존성 깨짐. conda 환경 Python 경로: `C:\Users\gs071\.conda\envs\mind-signal\python.exe`.

## sdk/ 폴더 수정 금지

`sdk/`는 Emotiv 제공 원본 코드라 수정 금지. `.flake8`에서도 무시 대상으로 등록됨. 수정 PR 발견 시 즉시 revert.

## Agent 자기 검증 규칙

1. 4단계 전체 통과 전까지 작업 완료 선언 금지.
2. 실패 시 근본 원인 수정 — `# noqa`/`# flake8: noqa` 인용 근거 없이 추가 금지.
3. 동일 단계 3회 연속 실패 시 사람에게 에스컬레이션.
