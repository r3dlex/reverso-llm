#!/usr/bin/env bash
#
# OCG-G9 verification. The live proof needs a real subscription and is not run
# here; the recorded artifact is gated against the shipped deny-lists instead.
#
# The last two lines split one full run into two passes: test_kimi_login and the
# headroom smoke test are PRE-EXISTING load-sensitive flakes that each pass in
# isolation and on clean main. They are still run and still must pass.

set -euo pipefail

uv run pytest tests/unit/test_opencode_tool_routing.py tests/unit/test_opencode_proof.py -q
uv run pytest tests/unit/test_opencode_catalog.py tests/unit/test_opencode_adapter.py tests/unit/test_opencode_messages.py -q
uv run pytest tests/unit/test_opencode_anthropic_surface.py tests/unit/test_opencode_codex_profile.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ --ignore=tests/integration -q --deselect tests/unit/test_kimi_login.py --deselect tests/unit/test_headroom_compression.py::test_real_headroom_smoke_uses_memory_only_state
uv run pytest tests/unit/test_kimi_login.py tests/unit/test_headroom_compression.py -q
