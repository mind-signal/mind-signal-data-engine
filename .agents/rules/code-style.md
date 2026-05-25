# Code Style Rules — Mind Signal Data Engine (Python)

## 환경

- Python 3.10, Conda 가상환경 `mind-signal`
- conda 환경 Python 경로: `C:\Users\gs071\.conda\envs\mind-signal\python.exe`
- 시스템 Python(3.13/3.9)으로 실행 금지 — 의존성 깨짐

## 포맷터 / 린터

- 포맷터: `black .` (라인 길이 88, PEP8 호환)
- import 정렬: `isort .` (black 호환 프로파일)
- 린터: `flake8 .` (`sdk/` 제외, `.flake8` 설정 따름)

## 주석 — Google Style Docstring + 한국어 명사형 종결

- public 함수/클래스에는 Google Style Docstring (Args/Returns/Raises 섹션)
- inline/block 주석은 명사형으로 종결:
  - 허용 종결: `~함`, `~사용`, `~완료`, `~임`, `~반환`, `~생성`, `~처리`
  - ✅ `# EEG 데이터 Redis로 전송함`
  - ✅ `# 환경변수 로드 완료`
  - ❌ `# EEG 데이터를 Redis로 전송합니다`
  - ❌ `# 환경변수를 로드하는 함수`

## sdk/ 폴더 — 수정 금지

`sdk/`는 Emotiv 제공 원본 코드라 수정 금지. `.flake8`에서도 무시 대상으로 등록. PR에서 sdk/ 수정 발견 시 즉시 revert.

## 네이밍

- 모듈/함수/변수: `snake_case`
- 클래스: `PascalCase`
- 상수(모듈 레벨, 진짜 불변): `SCREAMING_SNAKE_CASE`
- 파일: `snake_case.py`

## Type Hints

- public 함수 시그니처에 type hint 의무
- `typing.Optional[X]` 대신 Python 3.10 union `X | None` 사용
- DataFrame/ndarray는 `pandas.DataFrame` / `numpy.ndarray` 명시
