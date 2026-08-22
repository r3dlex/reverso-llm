#!/usr/bin/env bash
#
# OCG-G5 verification. Wrapped per the verify_ollama_g*.sh precedent: the
# goal-record allowlist also permits a bare `pytest`, but that resolves to an
# interpreter without this project's dependencies.

set -euo pipefail

uv run pytest tests/unit/test_opencode_messages.py tests/unit/test_opencode_anthropic_surface.py -q
uv run pytest tests/unit/test_opencode_adapter.py tests/unit/test_opencode_registration.py -q
uv run pytest tests/unit/test_surface_registry.py tests/unit/test_surface_registry_catalog_owning.py tests/unit/test_surface_registry_index_conflicts.py -q
uv run pytest tests/unit/test_opencode_exposure.py tests/unit/test_docs_dash_clean.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ --ignore=tests/integration -q
