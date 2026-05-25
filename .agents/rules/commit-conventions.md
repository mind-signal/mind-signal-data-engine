# 커밋 규칙

## PR 전 필수 로컬 검사 (커밋·push 전 반드시 실행)

GitHub Actions CI는 현재 이 레포에 없다. CodeRabbit AI 리뷰만 PR에 자동 연결되므로, 아래 4단계 로컬 검증을 **커밋 전에 반드시** 수동으로 돌린다.

```bash
conda activate mind-signal
black .               # 포맷 자동 수정
isort .               # import 정렬 자동 수정
flake8 .              # PEP8 린트 (sdk/ 제외)
pytest                # 단위 테스트 (필요 시 tests/ 디렉토리)
```

**순서 중요**: black → isort → flake8 → pytest 순으로 실행.
한 단계라도 실패하면 수정 후 재실행, 전부 통과한 뒤에만 커밋·push한다.

**Conda 환경 주의**: 반드시 `conda activate mind-signal`을 먼저 실행한다. 시스템 Python으로 실행하면 의존성이 깨진다. conda 환경 Python 경로: `C:\Users\gs071\.conda\envs\mind-signal\python.exe`.

**SDK 수정 금지**: `sdk/` 폴더는 Emotiv 제공 원본 코드라 수정 금지. `.flake8`에서도 무시 대상으로 등록되어 있다.

---

## Co-authored-by 작성 방법

모든 커밋 메시지 마지막에 아래 형식으로 고정 작성한다.

```
Co-authored-by: gs07103 <gwonseok02@gmail.com>
```

- **이메일**: `gwonseok02@gmail.com` 고정 (`noreply` 주소 사용 금지)
- Claude Co-Authored-By 추가 금지 — `gs07103` 단독으로만

---

## Conventional Commits 메시지 형식

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
- `main` 브랜치 직접 commit 금지 — 반드시 `feat/#이슈번호-작업내용` 브랜치에서 작업 후 `dev`로 PR
