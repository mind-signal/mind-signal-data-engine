# Commit Rules

## Pre-commit local checks

See `.agents/rules/verification-loop.md` for the mandatory 4-step pipeline (`black -> isort -> flake8 -> pytest`) — run it before every commit, not summarized here to avoid drift between the two files.

`sdk/` is vendored from Emotiv — never modify it (also enforced by `.flake8`).

---

## Co-authored-by trailer

Every commit message ends with:

```
Co-authored-by: KWONSEOK02 <gwonseok02@gmail.com>
```

- Email fixed to `gwonseok02@gmail.com` — no `noreply` address.
- No Claude co-author line — `KWONSEOK02` only.

---

## Conventional Commits message format

```
{type}({scope}): {description}
```

Examples:

```
feat(streamer): add per-subject Redis channel keying
fix(analyzer): correct FAA calculation for right-handed subjects
perf(streamer): reduce pub/sub latency with batch publish
test(analyzer): cover empty CSV edge case
```

| Type | Use |
|------|-----|
| feat | new feature |
| fix | bug fix |
| refactor | structural change, no behavior change |
| style | formatting/whitespace, no logic change |
| docs | documentation |
| chore | build/config/dependencies |
| test | test addition/change |
| perf | performance improvement |
| ci | CI configuration |
| revert | revert a prior commit |

- One task = one commit.
- Never commit directly to `main` — branch as `{type}/{domain-wNNN}-{slug}` (Work ID style, matching the product's `.plans/` Work ID scheme), PR into `dev`.
