# Code Style Rules — Mind Signal Data Engine (Python)

## Environment

- Python 3.10, conda env `mind-signal`.
- Confirm the right interpreter is active with `conda activate mind-signal && python --version` before running anything — don't hardcode a machine-specific interpreter path, everyone's conda install lives somewhere different.
- Never run with system Python (3.13/3.9) — dependencies break.

## Formatter / linter

- Formatter: `black .` (line length 88, PEP8-compatible)
- Import sort: `isort .` (black-compatible profile)
- Linter: `flake8 .` (`sdk/` excluded, per `.flake8`)

## Comments — Google-style docstrings + Korean noun-form endings

This is a deliberate team convention and stays in Korean regardless of the language the rest of the docs are written in.

- Public functions/classes: Google-style docstring (Args/Returns/Raises sections).
- Inline/block comments end in noun form:
  - Allowed endings: `~함`, `~사용`, `~완료`, `~임`, `~반환`, `~생성`, `~처리`.
  - ✅ `# EEG 데이터 Redis로 전송함`
  - ✅ `# 환경변수 로드 완료`
  - ❌ `# EEG 데이터를 Redis로 전송합니다`
  - ❌ `# 환경변수를 로드하는 함수`

## sdk/ — do not modify

`sdk/` is vendored from Emotiv — never modify it. Also excluded in `.flake8`. A PR touching `sdk/` gets reverted on sight.

## Naming

- Modules/functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants (module-level, actually immutable): `SCREAMING_SNAKE_CASE`
- Files: `snake_case.py`

## Type hints

- Mandatory on public function signatures.
- Use the Python 3.10 union `X | None`, not `typing.Optional[X]`.
- Spell out `pandas.DataFrame` / `numpy.ndarray` explicitly rather than leaving them untyped.
