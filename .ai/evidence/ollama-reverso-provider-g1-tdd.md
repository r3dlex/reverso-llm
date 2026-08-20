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

## Unified Ruff policy

The `dev` optional dependency pins Ruff 0.6.0, the same version already pinned
by `prek.toml`. `uv run ruff` and the `uvx prek` Ruff hooks therefore execute
one declared checker/formatter version. This formalizes the existing lint tool
without adding, omitting, or relaxing a lint policy.

## Autobahn verification entrypoint

The goal record allowlists the single command `bash tests/verify_ollama_g1.sh`.
That strict Bash wrapper preserves and runs all seven dedicated-spec
verification commands in their original order. It does not omit, bypass,
replace, or relax an underlying command.
