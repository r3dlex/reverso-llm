"""OCG-G7: the recorded proof artifact, and what it pins.

The proof itself needs a live subscription and cannot run in CI. What CAN be
gated is that its RECORDED result stays consistent with the code shipped
alongside it, so the artifact does not quietly drift from the deny-list it
justifies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reverso.opencode_catalog_artifact import repo_root

PROOF_PATH = Path("docs/reference/opencode-go-proof.json")


@pytest.fixture(scope="module")
def proof() -> dict:
    return json.loads((repo_root() / PROOF_PATH).read_text(encoding="utf-8"))


def test_the_proof_covers_the_whole_catalog(proof) -> None:
    from reverso.opencode_catalog_artifact import load_catalog_ids

    assert set(proof["per_model"]) == set(load_catalog_ids())


def test_the_endpoint_deny_list_matches_the_recorded_measurement(proof) -> None:
    """The one id recorded as refusing the Anthropic format must be the one id
    on the deny-list; a silent divergence there reroutes traffic."""
    from reverso.protocols.adapters.opencode.catalog import (
        ANTHROPIC_UNSUPPORTED_MODELS,
    )

    refused = {
        model for model, caps in proof["per_model"].items() if caps["messages"] == "401"
    }
    assert refused == set(ANTHROPIC_UNSUPPORTED_MODELS)


def test_the_end_to_end_run_exercised_both_surfaces(proof) -> None:
    surfaces = {row["surface"] for row in proof["end_to_end"]}
    assert surfaces == {"responses", "anthropic_messages"}


def test_a_tool_call_completed_on_both_surfaces(proof) -> None:
    """kimi-k3 is the recorded case that proves the double translation is not
    lossy for tool_use: one parsed call with the right name on each surface."""
    rows = [
        row
        for row in proof["end_to_end"]
        if row["model"] == "kimi-k3" and row["status_code"] == 200
    ]
    assert len(rows) == 2
    for row in rows:
        fidelity = row["fidelity"]
        calls = fidelity.get("tool_use_count", fidelity.get("function_call_count"))
        assert calls == 1
        assert fidelity["tool_names"] == ["get_weather"]


def test_the_tools_gap_is_recorded_not_lost(proof) -> None:
    """The defect this proof found must stay visible in the artifact."""
    rejected = set(proof["anthropic_tools_rejected"])
    supported = set(proof["anthropic_tools_supported"])
    assert len(rejected) == 11
    assert "glm-5" in rejected
    assert "kimi-k3" in supported
    assert not rejected & supported


def test_the_filed_defect_record_exists() -> None:
    """G7 files defects rather than fixing them, so the record is the deliverable."""
    record = repo_root() / ".ai/work-intake/opencode-go-anthropic-tools-gap.md"
    text = record.read_text(encoding="utf-8")
    assert "scripts/opencode-live-proof.py" in text
    assert "glm-5" in text


def test_tools_and_output_config_support_are_independent(proof) -> None:
    """Neither capability may be inferred from the other: two upstream
    translators sit behind one endpoint."""
    per = proof["per_model"]
    both = {
        m
        for m, c in per.items()
        if c["anthropic_tools"] == "200" and c["anthropic_output_config"] == "200"
    }
    # If one implied the other, this intersection would be one of the full sets.
    assert 0 < len(both) < 11
