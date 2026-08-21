"""G3 operator runbook and verification artifact contracts."""

from __future__ import annotations

import json
from pathlib import Path


EXPECTED_COMMANDS = [
    "uv run pytest tests/unit/test_ollama_convergence.py tests/unit/test_catalog_refresh.py tests/unit/test_client_convergence_contract.py -q",
    "uv run pytest tests/integration/test_ollama_convergence_runbook.py tests/integration/test_client_convergence_runbook.py -q",
    "uv run reverso-client-sync dry-run --json",
    "uv run reverso-client-sync apply --json",
    "uv run reverso-client-sync apply --json",
    "uv run reverso-client-sync refresh --json",
    "uv run reverso-client-sync verify --json",
    "./scripts/convergence-acceptance.sh",
    'uv run python tests/helpers/verify_isolated_convergence.py --home "${VERIFY_HOME}" --rtk-bin "${FAKE_BIN}/rtk"',
    "uv run pytest tests/ -v --ignore=tests/integration --tb=short",
]


def test_g3_wrapper_preserves_all_ten_commands_in_order_and_is_isolated() -> None:
    wrapper = Path("tests/verify_ollama_g3.sh")
    text = wrapper.read_text(encoding="utf-8")
    positions = [text.index(command) for command in EXPECTED_COMMANDS]

    assert positions == sorted(positions)
    assert text.startswith("#!/usr/bin/env bash\n\nset -euo pipefail\n")
    assert "mktemp -d" in text
    assert 'HOME="${VERIFY_HOME}"' in text
    assert "REVERSO_ISOLATED_VERIFICATION_HOME" not in text
    assert wrapper.stat().st_mode & 0o111


def test_g3_goal_and_evidence_are_ready_and_complete() -> None:
    goal = json.loads(Path(".ai/goals/ollama-reverso-provider-g3.json").read_text())
    evidence = json.loads(Path(".ai/evidence/OLLAMA-RP-G3.json").read_text())

    assert goal["id"] == "OLLAMA-RP-G3"
    assert goal["implementation_ready"] is True
    assert goal["verification"] == ["bash tests/verify_ollama_g3.sh"]
    assert goal["evidence"]
    assert evidence["goal_id"] == goal["id"]


def test_operator_docs_cover_prompt_free_refresh_restore_and_opt_out() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    client_sync = Path("docs/client-sync.md").read_text(encoding="utf-8")

    for required in (
        "REVERSO_OLLAMA_CLOUD=0",
        "prompt-free",
        "partial freshness",
        "stale Cloud",
        "restore",
        "uninstall",
    ):
        assert required in readme or required in client_sync
