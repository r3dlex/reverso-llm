#!/usr/bin/env bash
#
# OCG-G4 verification. Wrapped in a script, following the verify_ollama_g*.sh
# precedent, because the goal-record allowlist permits `bash tests/` but the bare
# `pytest` it also permits resolves to an interpreter without this project's
# dependencies. A red leg recorded there would be red for a missing httpx rather
# than for the change under test.

set -euo pipefail

uv run pytest tests/unit/test_opencode_adapter.py tests/unit/test_opencode_registration.py -q
uv run pytest tests/unit/test_opencode_credentials.py tests/unit/test_opencode_catalog.py tests/unit/test_opencode_exposure.py tests/unit/test_opencode_env_exposure.py -q
uv run pytest tests/unit/test_codex_direct_adapter.py tests/unit/test_client_convergence_contract.py tests/unit/test_headroom_compression.py tests/unit/test_codex_usage.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ --ignore=tests/integration -q
