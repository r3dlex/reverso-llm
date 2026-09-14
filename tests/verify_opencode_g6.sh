#!/usr/bin/env bash
#
# OCG-G6 verification. Wrapped per the verify_ollama_g*.sh precedent: the
# allowlist also permits a bare `pytest`, but that resolves to an interpreter
# without this project's dependencies.

set -euo pipefail

uv run pytest tests/unit/test_opencode_catalog_artifact.py tests/unit/test_opencode_collision_gate.py -q
uv run pytest tests/unit/test_opencode_catalog.py tests/unit/test_opencode_exposure.py -q
uv run pytest tests/unit/test_client_sync.py tests/unit/test_claude_code_sync.py tests/unit/test_codex_sync.py -q
uv run pytest tests/unit/test_surface_registry.py tests/unit/test_surface_registry_catalog_owning.py tests/unit/test_docs_dash_clean.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ --ignore=tests/integration -q
