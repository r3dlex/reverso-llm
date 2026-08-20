---
title: OLLAMA-RP-G1 red-green evidence
goal: OLLAMA-RP-G1
date: 2026-08-20
---

# OLLAMA-RP-G1 red-green evidence

## Legacy-safe TDD selection

- Coverage percent: 0
- Legacy-safe TDD: true
- Reason: high coupling/composition blast radius
- Readiness gate: passed for implementation-ready goal `OLLAMA-RP-G1`
- Prerequisite gate: passed for ai-catapult-init v3 and the direct goal record

## Red

Command:

```text
uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/unit/test_codex_sync.py tests/unit/test_client_convergence_contract.py tests/integration/test_ollama_codex_profile.py -q
```

Result: exit 2 during collection. Both new unit modules failed with
`ModuleNotFoundError: No module named 'reverso.protocols.adapters.ollama'`.
This was the required missing routed Responses provider failure.

## Green

The exact red command was rerun after implementation.

Result: exit 0, 156 passed in 1.65 seconds.

Additional verification:

- Integration profile command: exit 0, 1 passed.
- Anthropic negative proof command: exit 0, 29 passed.
- Headroom regression command: exit 0, 58 passed.
- Client convergence regression command: exit 0, 126 passed.
- Composition and registry regression command: exit 0 after the G1 expectation
  was updated for composition injection.
- `uvx prek run --all-files`: exit 0, all hooks passed.

## Known repository-wide lint baseline

The declared repo-wide Ruff gates are not satisfied and remain red from the
pre-existing baseline. Fresh results after G1 review repair are:

- `uv run ruff check .`: exit 1, 187 errors (160 fixable; 7 additional unsafe
  fixes hidden).
- `uv run ruff format --check .`: exit 1, 11 files would be reformatted and 284
  files are already formatted.

No unrelated repository-wide Ruff or formatting finding was mass-fixed.
