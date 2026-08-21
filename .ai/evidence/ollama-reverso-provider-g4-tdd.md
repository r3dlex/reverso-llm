---
title: OLLAMA-RP-G4 red-green evidence
goal: OLLAMA-RP-G4
date: 2026-08-21
---

# OLLAMA-RP-G4 red-green evidence

## Legacy-safe TDD selection

- Coverage percent: 0
- Legacy-safe TDD: true
- Reason: foreground authentication and live client subprocess safety boundary
- Readiness gate: passed for implementation-ready goal `OLLAMA-RP-G4`
- Prerequisite gate: passed because G3 is present in the branch base

## Red

Command:

```text
uv run pytest tests/integration/test_ollama_live_contract.py -q
```

Result: exit 4 because the deterministic live-contract test module did not
exist. This is the required harness gap and does not depend on credentials.

## Green

The exact targeted command exited 0 with 5 passed. The focused integration and
unit selection exited 0 with 10 passed. Repository regression evidence is 1199
unit tests passed and 539 integration tests passed with 6 skipped. Ruff, Ruff
format, prek, compileall, rules validation, and structural archgate also passed.

The attended proof then exposed a hardcoded account name in the deployment
authority. A regression selection covering installer ordering and canonical
checkout derivation first exited 1 with 2 failures. After deriving the account
home from the operating system password database, the full deployment drift
integration module exited 0 with 129 passed.

`./scripts/convergence-acceptance.sh` was not runnable in the requested
uncommitted worktree because the script requires a clean checkout. It exited 1
with `convergence-acceptance: checkout must be clean`; no live request or target
state mutation was attempted.

The production deployment-drift acceptance command was also inapplicable to
this isolated worktree. It exited 1 because acceptance requires the canonical
deployed checkout. The exact non-integration regression command still completed
with 1199 passed.
