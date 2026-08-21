---
title: OLLAMA-RP-G2 red-green evidence
goal: OLLAMA-RP-G2
date: 2026-08-21
---

# OLLAMA-RP-G2 red-green evidence

## Legacy-safe TDD selection

- Coverage percent: 0
- Legacy-safe TDD: true
- Reason: high coupling/protocol fidelity and composition blast radius
- Readiness gate: passed for implementation-ready goal `OLLAMA-RP-G2`
- Prerequisite gate: passed because merged G1 is the branch base

## Red

Command:

```text
uv run pytest tests/unit/test_ollama_messages.py tests/integration/test_ollama_claude_launcher.py -q
```

Result: exit 2 during collection. The new unit module failed with
`ModuleNotFoundError: No module named 'reverso.protocols.adapters.ollama.messages'`.
This is the required absent native Messages route and launcher capability.

## Green

The exact red command was rerun after implementation.

Result: exit 0, 10 passed.

## Verification wrapper

`bash tests/verify_ollama_g2.sh` completed all eight commands in order:

- G2 Messages, translation, stream, and Headroom unit set: 73 passed.
- Claude sync and convergence contract set: 41 passed.
- Claude launcher and Anthropic integration set: 42 passed.
- Frozen Responses and Codex Ollama regression set: 36 passed.
- Ruff check: passed.
- Ruff format check: 157 files formatted.
- Prek: all hooks passed.
- Full non-integration regression: 1171 passed.
