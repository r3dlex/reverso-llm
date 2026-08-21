#!/usr/bin/env bash

set -euo pipefail

uv run pytest tests/unit/test_ollama_messages.py tests/unit/test_anthropic_translate.py tests/unit/test_anthropic_stream.py tests/unit/test_headroom_compression.py -q
uv run pytest tests/unit/test_claude_code_sync.py tests/unit/test_client_convergence_contract.py -q
uv run pytest tests/integration/test_ollama_claude_launcher.py tests/integration/test_anthropic_messages_parity.py tests/integration/test_anthropic_messages_streaming.py -q
uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/integration/test_ollama_codex_profile.py -q
uv run ruff check .
uv run ruff format --check .
uvx prek run --all-files
uv run pytest tests/ -v --ignore=tests/integration --tb=short
