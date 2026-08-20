---
type: cleanup-plan
status: complete
scope: repository-wide-ruff-baseline
related_pr: 116
---

# Ollama G1 Gate Remediation

## Goal

Remove the existing repository-wide Ruff check and format debt that blocks the Ollama G1 gate, without changing product behavior.

## Plan

1. Capture the exact Ruff check, Ruff format, and full non-integration pytest baselines.
2. Apply Ruff's safe fixes, then review and repair remaining findings category by category with minimal edits.
3. Run Ruff check, Ruff format, prek, the full non-integration suite, eligible integration tests, compile checks, and a final diff review.

## Baseline Evidence

- `uv run ruff check .` - failed with 195 findings (168 safe-fixable, 7 unsafe-fixable) under initially installed Ruff 0.16.4. The requested 187 expectation corresponds to the repository-pinned Ruff 0.6.0 hook; the isolated environment had no Ruff installed before capture, so the version mismatch is retained here rather than misreported.
- `uv run ruff format --check .` - failed with 11 files requiring formatting and 266 already formatted.
- `uv run pytest tests/ -v --ignore=tests/integration --tb=short` - passed: 1119 tests in 26.73 seconds.
- Environment note: the isolated worktree initially lacked Ruff and dev dependencies; `uv sync --frozen --extra dev` plus an untracked virtual-environment-only Ruff install prepared the exact commands without changing repository dependency files.

## Constraints

- No new dependencies or product refactors.
- Preserve behavior; add regression coverage only if a manual semantic repair requires it.
- Do not commit or push.

## Completion Evidence

- `uv run ruff check .` - passed with Ruff 0.6.0.
- `uv run ruff format --check .` - passed: 143 files already formatted.
- `uvx prek run --all-files` - all nine hooks passed.
- `uv run pytest tests/ -v --ignore=tests/integration --tb=short` - 1119 passed in 16.34 seconds.
- `uv run pytest tests/integration -q` - 526 passed and 6 live/manual tests skipped in 37.00 seconds.
- `uv run python -m compileall -q src/reverso` and per-file `py_compile` - passed.
- `git diff --check` - passed.
