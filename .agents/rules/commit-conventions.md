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
- Never commit directly to `main` — work on a branch and PR into `dev`.
- Branch naming (product-wide rule; canonical text: `mind-signal/.plans/README.md`, section "브랜치 이름"):
  - If the work falls within the scope and completion criteria of a registered Work, use `{type}/{work-id}-{slug}` for any type. `{work-id}` is the full Work ID in lowercase. Examples from the merge history: `feat/ops-w013-message-bus-memory`, `fix/ops-w010-t7-preflight`, `refactor/ops-w017-session-csv-store`
  - If the work has no Work affiliation, use `{type}/{slug}` or `{type}/{issue-number}-{slug}` (format examples: `fix/readme-link`, `fix/123-readme-link`). The `#` before the issue number is optional: `fix/#123-readme-link` is the same format.
  - Never use an issue number in place of the Work ID on a branch that belongs to a Work; link the issue from the PR body.
  - Use lowercase English letters, digits, and hyphens for `{slug}`. It may differ from the Work folder slug.
  - `{type}` is the commit type that matches the final purpose of the work. `hotfix` is an extra branch prefix for urgent production fixes; it is not a commit type. It follows the same two formats: `hotfix/{work-id}-{slug}` for work that belongs to a Work, `hotfix/{slug}` otherwise.
  - If the product plan files (`mind-signal/.plans/`) are unavailable, decide by the Work ID and scope you were given. If no Work ID was given, use the format for work with no Work affiliation.
  - `main` and `dev` are integration branches and keep their names; release PRs go from `dev` to `main`. Do not rename existing branches because of this rule.
