---
title: OLLAMA-RP-G3 red-green evidence
goal: OLLAMA-RP-G3
date: 2026-08-21
---

# OLLAMA-RP-G3 red-green evidence

## Legacy-safe TDD selection

- Coverage percent: 0
- Legacy-safe TDD: true
- Reason: cross-client filesystem atomicity and background authentication boundary
- Readiness gate: passed for implementation-ready goal `OLLAMA-RP-G3`
- Prerequisite gate: passed because G2 is present in the branch base

## Red

Command:

```text
uv run pytest tests/unit/test_ollama_convergence.py tests/integration/test_ollama_convergence_runbook.py -q
```

Result: exit 2 during collection. The new unit module failed with
`ImportError: cannot import name 'ollama_convergence' from 'reverso'`. This is
the required absent refresh, freshness, and atomicity capability.

## Green

The exact red-green selection exited 0 with 11 passed. The required four-file
targeted selection exited 0 with 41 passed. Review-repair regressions separately
cover marker-owned cross-client publication, total discovery preservation,
valid empty discovery, fail-closed uninstall and idempotent restore, Ollama-first
apply failure isolation, and the production deployment-drift bypass boundary.

The full isolated wrapper passed after the amended commit: 38 unit tests, 29
integration tests, both idempotent apply checks, refresh and verify, convergence
acceptance, the explicit test-only 19-surface isolated verifier, and 1194
non-integration tests.
