#!/usr/bin/env bash
#
# OCG-G8 verification. Wrapped per the verify_ollama_g*.sh precedent: the
# allowlist also permits a bare `pytest`, but that resolves to an interpreter
# without this project's dependencies.

# The last two lines split one full run into two passes. test_kimi_login and
# test_headroom_compression::test_real_headroom_smoke_uses_memory_only_state are
# PRE-EXISTING load-sensitive flakes: each fails roughly one full run in two,
# a DIFFERENT test each time, and each passes in isolation and on clean main.
# They are still RUN and still must pass; they are only run unloaded. This is a
# mitigation, not a skip, and the underlying flakiness is filed separately.

set -euo pipefail

uv run pytest tests/unit/test_opencode_codex_profile.py -q
uv run pytest tests/unit/test_model_exposure.py tests/unit/test_codex_sync.py -q
uv run pytest tests/unit/test_client_sync.py tests/unit/test_claude_code_sync.py -q
uv run pytest tests/unit/test_opencode_catalog_artifact.py tests/unit/test_opencode_collision_gate.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ --ignore=tests/integration -q --deselect tests/unit/test_kimi_login.py --deselect tests/unit/test_headroom_compression.py::test_real_headroom_smoke_uses_memory_only_state
uv run pytest tests/unit/test_kimi_login.py tests/unit/test_headroom_compression.py -q
