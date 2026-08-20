from __future__ import annotations

import json
from pathlib import Path

EXPECTED_COMMANDS = [
    "uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/unit/test_codex_sync.py tests/unit/test_client_convergence_contract.py -q",
    "uv run pytest tests/integration/test_ollama_codex_profile.py -q",
    "uv run pytest tests/unit/test_anthropic_provider_qualified.py tests/integration/test_anthropic_messages_parity.py -q",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uvx prek run --all-files",
    "uv run pytest tests/ -v --ignore=tests/integration --tb=short",
]


def test_ollama_g1_wrapper_preserves_all_spec_verification_commands() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    wrapper = repo_root / "tests/verify_ollama_g1.sh"
    lines = wrapper.read_text(encoding="utf-8").splitlines()

    assert lines[:3] == ["#!/usr/bin/env bash", "", "set -euo pipefail"]
    assert [line for line in lines[3:] if line] == EXPECTED_COMMANDS
    assert wrapper.stat().st_mode & 0o111


def test_ollama_g1_goal_uses_only_the_allowlisted_wrapper() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    goal = json.loads(
        (repo_root / ".ai/goals/ollama-reverso-provider-g1.json").read_text(
            encoding="utf-8"
        )
    )

    assert goal["verification"] == ["bash tests/verify_ollama_g1.sh"]
