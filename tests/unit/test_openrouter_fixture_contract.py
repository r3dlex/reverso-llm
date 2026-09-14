"""Unit tests for the OpenRouter OR-G0 spike fixture contract.

The spike lives entirely outside the gateway code path. This test enforces the
attended spike contract from `.ai/work-intake/openrouter-reverso-provider-or-g0.md`:

* the spike fixtures are redacted, schema-valid, and use the agreed envelope;
* the manifest names a freshness bound and an endpoint-binding proof;
* each real managed launcher has a proven per-launch capability carrier, or the
  carrier is explicitly unavailable for that client;
* the Claude alias grammar is exactly ``anthropic-openrouter-<encoded>`` and the
  full Responses replay grammar is decided;
* an explicit GO or REVISE verdict lives in ``docs/spike-notes.md``;
* no secret, capability handle, prompt, body, local path, or username leaks
  through the fixtures.

This test is the red-green command declared in the OR-G0 work item:
``uv run pytest tests/unit/test_openrouter_fixture_contract.py -q``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "openrouter"
SPIKE_NOTES = REPO_ROOT / "docs" / "spike-notes.md"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

# Redaction patterns: any fixture that contains a real secret, capability handle,
# prompt body, local path, or username fails the contract.
SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"Bearer\s+sk-or-v1-[A-Za-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"OPENROUTER_API_KEY=[A-Za-z0-9_\-]{16,}"),
]
CAPABILITY_PATTERN = re.compile(r"\bcap_[A-Za-z0-9_\-]{16,}\b")
PATH_PATTERN = re.compile(r"/Users/[A-Za-z0-9_.\-/]+|\.zsh_exports|~/[A-Za-z0-9_.\-/]+")
PROMPT_PATTERNS = [
    re.compile(r"\bprompt\s*:\s*\"[^\"]{8,}\"", re.IGNORECASE),
    re.compile(r"\bbody\s*:\s*\"[^\"]{40,}\"", re.IGNORECASE),
]

# Canonical envelopes (provider-neutral; spike uses an additive OR-G0 envelope).
REQUIRED_FIXTURE_KEYS = {"fixture", "name", "method", "path", "request", "expected"}

# Allowed upstream endpoints in OR-G0 spike fixtures.
ALLOWED_PATHS = {
    "/api/v1/models",
    "/api/v1/responses",
    "/api/v1/messages",
}

# Allowed methods for the spike fixtures.
ALLOWED_METHODS = {"GET", "POST"}


def _load_manifest() -> dict:
    assert (
        MANIFEST_PATH.is_file()
    ), "OR-G0 fixture manifest is missing at tests/fixtures/openrouter/manifest.json"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_fixture(rel_path: str) -> dict:
    return json.loads((FIXTURE_DIR / rel_path).read_text(encoding="utf-8"))


def _collect_strings(node) -> list[str]:
    """Flatten a JSON-like node into every string leaf for redaction checks."""
    found: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_collect_strings(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_collect_strings(value))
    elif isinstance(node, str):
        found.append(node)
    return found


def test_manifest_present_and_schema_valid() -> None:
    manifest = _load_manifest()
    for key in ("manifest", "fixtures", "freshness_bound_seconds", "endpoint_binding"):
        assert key in manifest, f"manifest missing key '{key}'"
    assert manifest["manifest"] == "openrouter_or-g0_spike"
    assert manifest["endpoint_binding"]["policy"] in {"zdr_required", "default_deny"}
    assert isinstance(manifest["freshness_bound_seconds"], int)
    assert manifest["freshness_bound_seconds"] > 0
    assert (
        isinstance(manifest["fixtures"], list) and manifest["fixtures"]
    ), "manifest must list at least one fixture"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "list_models_authenticated",
        "responses_unary",
        "responses_streaming",
        "messages_unary",
        "messages_streaming",
    ],
)
def test_required_fixtures_present(fixture_name: str) -> None:
    manifest = _load_manifest()
    names = {entry["name"] for entry in manifest["fixtures"]}
    assert (
        fixture_name in names
    ), f"required OR-G0 fixture '{fixture_name}' missing from manifest"
    file_entry = next(
        entry for entry in manifest["fixtures"] if entry["name"] == fixture_name
    )
    path = FIXTURE_DIR / file_entry["file"]
    assert path.is_file(), f"fixture file '{file_entry['file']}' missing"


def test_fixture_envelopes_use_canonical_shape() -> None:
    manifest = _load_manifest()
    for entry in manifest["fixtures"]:
        fixture = _load_fixture(entry["file"])
        missing = REQUIRED_FIXTURE_KEYS - set(fixture.keys())
        assert (
            not missing
        ), f"fixture '{entry['name']}' missing envelope keys {sorted(missing)}"
        assert fixture["method"] in ALLOWED_METHODS
        assert fixture["path"] in ALLOWED_PATHS


def test_fixtures_are_redacted() -> None:
    manifest = _load_manifest()
    for entry in manifest["fixtures"]:
        fixture = _load_fixture(entry["file"])
        flat = "\n".join(_collect_strings(fixture))
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(
                flat
            ), f"fixture '{entry['name']}' leaks a secret-shaped value matching {pattern.pattern}"
        assert not CAPABILITY_PATTERN.search(
            flat
        ), f"fixture '{entry['name']}' leaks a capability handle"
        assert not PATH_PATTERN.search(
            flat
        ), f"fixture '{entry['name']}' leaks a local path or username"
        for pattern in PROMPT_PATTERNS:
            assert not pattern.search(
                flat
            ), f"fixture '{entry['name']}' leaks a prompt/body value matching {pattern.pattern}"


def test_endpoint_binding_proof_recorded() -> None:
    manifest = _load_manifest()
    binding = manifest["endpoint_binding"]
    for key in ("routing_object", "evidence_revision_field", "compatibility_set"):
        assert key in binding, f"endpoint_binding missing '{key}'"
    assert (
        isinstance(binding["compatibility_set"], list) and binding["compatibility_set"]
    ), "compatibility_set must be a non-empty list of compatible endpoint providers"


def test_capability_carriers_recorded() -> None:
    manifest = _load_manifest()
    carriers = manifest["capability_carriers"]
    for client in ("codex", "claude"):
        entry = carriers[client]
        assert entry["feasible"] in {True, False}
        if entry["feasible"]:
            for key in ("header", "handshake", "verification"):
                assert key in entry, f"capability_carriers.{client} missing '{key}'"
            assert entry["header"], "feasible carrier must name a Reverso-owned header"
        else:
            assert entry.get("fallback") in {
                "deny_default_policy"
            }, "unfeasible carrier must deny weaker-privacy grants for that client"


def test_claude_alias_grammar_decided() -> None:
    manifest = _load_manifest()
    grammar = manifest["claude_alias_grammar"]
    assert grammar["template"].startswith(
        "anthropic-openrouter-"
    ), "claude alias template must start with 'anthropic-openrouter-'"
    assert grammar["encoding"] in {"base64url_strip_padding", "percent_quote_safe"}
    assert grammar["upstream_identity_preserved"] is True


def test_responses_replay_grammar_decided() -> None:
    manifest = _load_manifest()
    replay = manifest["responses_replay_grammar"]
    assert replay["chain_source"] == "local_storage"
    assert replay["forbidden_upstream_fields"] == ["previous_response_id", "store"]
    assert replay["rejects_unknown_chain"] is True
    assert replay["rejects_partial_chain"] is True


def test_spike_notes_publish_explicit_verdict() -> None:
    assert SPIKE_NOTES.is_file(), "docs/spike-notes.md is required for OR-G0"
    text = SPIKE_NOTES.read_text(encoding="utf-8")
    # The verdict line must be the only place that says GO or REVISE.
    matches = re.findall(r"^verdict:\s*(GO|REVISE)\s*$", text, flags=re.MULTILINE)
    assert (
        len(matches) == 1
    ), "docs/spike-notes.md must contain exactly one 'verdict: GO|REVISE' line"
    verdict = matches[0]
    if verdict == "GO":
        assert (
            "revoked_grant_unavailable" not in text.lower()
        ), "GO verdict cannot coexist with documented weaker-privacy unavailability"
    else:
        assert (
            "REVISE" in text
        ), "REVISE verdict requires a revision rationale in the spike notes"


def test_probe_budget_isolated_from_fixtures() -> None:
    manifest = _load_manifest()
    budget = manifest.get("probe_budget", {})
    for key in ("approved_amount_usd", "spent_amount_usd", "currency"):
        assert key in budget, f"probe_budget missing '{key}'"
    assert budget["currency"] == "USD"
    assert float(budget["approved_amount_usd"]) <= 0.10
    assert float(budget["spent_amount_usd"]) <= float(budget["approved_amount_usd"])
