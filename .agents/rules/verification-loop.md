# Verification Loop Rules — Mind Signal Data Engine (Python)

## 4-step pipeline

No GitHub Actions CI exists in this repo yet; only CodeRabbit AI review runs on PRs. Run these four steps by hand before every commit.

```bash
conda activate mind-signal
black .               # auto-fixes formatting
isort .               # auto-fixes import order
flake8 .              # PEP8 lint (sdk/ excluded)
pytest                # unit tests (tests/)
```

Order matters: black -> isort -> flake8 -> pytest. Fix and re-run on any failure; only commit and push once all four pass.

## Conda environment precondition

`conda activate mind-signal` first, always. Running with system Python breaks dependencies. Verify with `python --version` after activating rather than assuming the shell already has the right interpreter.

## sdk/ — do not modify

`sdk/` is vendored from Emotiv — never modify it. Also excluded in `.flake8`. A PR touching it gets reverted on sight.

## Agent self-verification rules

1. Do not declare work complete until all four steps pass.
2. Fix the root cause on failure — do not add `# noqa` / `# flake8: noqa` without a cited reason.
3. Escalate to a human after three consecutive failures on the same step.
