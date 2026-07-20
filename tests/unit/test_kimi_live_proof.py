"""Offline fail-closed tests for the credentialed Kimi proof gate."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import httpx

from reverso.kimi_live_proof import (
    ALLOWED_CHECK_KEYS,
    LIVE_PROOF_ENV,
    REQUIRED_CHECK_IDS,
    HttpLiveProofProbe,
    ProofFailure,
    require_live_opt_in,
    run_proof,
    validate_manifest,
    write_manifest,
)
from reverso.kimi_live_proof import _client_challenge as generate_client_challenge

CLIENT_PROMPT = (
    "Reverse this lowercase hexadecimal token and output only the result: abc123"
)
CLIENT_EXPECTED = "321cba"


@pytest.fixture(autouse=True)
def _fixed_client_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "reverso.kimi_live_proof._client_challenge",
        lambda: (CLIENT_PROMPT, CLIENT_EXPECTED),
    )


def test_generated_client_challenge_is_exact_but_not_echoable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragments = iter(("0123456789abcdef", "fedcba9876543210"))
    monkeypatch.setattr(
        "reverso.kimi_live_proof.secrets.token_hex", lambda _: next(fragments)
    )

    prompt, expected = generate_client_challenge()

    assert expected == "0123456789abcdeffedcba9876543210"
    assert expected not in prompt
    assert "0123456789abcdef fedcba9876543210" in prompt


class FakeProbe:
    def __init__(
        self,
        *,
        discovery_source: str = "live",
        headroom_deltas: dict[str, int] | None = None,
        loopback_route: str = "http://127.0.0.1:64946",
        fail_lane: str | None = None,
    ) -> None:
        self.discovery_source = discovery_source
        self.headroom_deltas = headroom_deltas or {"responses": 2, "messages": 1}
        self.loopback_route = loopback_route
        self.fail_lane = fail_lane

    def _maybe_fail(self, lane: str) -> None:
        if self.fail_lane == lane:
            raise ProofFailure(f"{lane}_failed")

    def model_discovery(self) -> dict[str, Any]:
        self._maybe_fail("model_discovery")
        return {
            "provider": "kimi",
            "route": "/kimi/v1/models",
            "model_id": "kimi-k2.5",
            "model_discovery_source": self.discovery_source,
            "status": 200,
        }

    def responses_continuity(self, model_id: str | None) -> dict[str, Any]:
        self._maybe_fail("responses_continuity")
        assert model_id == "kimi-k2.5"
        return {
            "provider": "kimi",
            "route": "/kimi/v1/responses",
            "model_id": model_id,
            "response_id_shape": "resp_*",
            "status": 200,
        }

    def messages(self, model_id: str | None) -> dict[str, Any]:
        self._maybe_fail("messages")
        return {
            "provider": "kimi",
            "route": "/kimi/v1/messages",
            "model_id": model_id,
            "status": 200,
        }

    def codex(self, model_id: str | None) -> dict[str, Any]:
        self._maybe_fail("codex")
        return {"provider": "kimi", "model_id": model_id, "status": 0}

    def claude_code(self, model_id: str | None) -> dict[str, Any]:
        self._maybe_fail("claude_code")
        return {
            "provider": "kimi",
            "route": "/kimi/v1/messages",
            "model_id": model_id,
            "status": 0,
        }

    def headroom(self) -> dict[str, Any]:
        self._maybe_fail("headroom")
        return {"provider": "kimi", "headroom_deltas": self.headroom_deltas}

    def redaction(self) -> dict[str, Any]:
        self._maybe_fail("redaction")
        return {"provider": "kimi", "status": 0}

    def loopback(self) -> dict[str, Any]:
        self._maybe_fail("loopback")
        return {"provider": "kimi", "route": self.loopback_route, "status": 0}


def _write_codex_fixture(tmp_path: Path, *, base_url: str | None = None) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[model_providers.reverso_kimi]\n'
        f'base_url = "{base_url or "http://127.0.0.1:64946/kimi/v1"}"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
    catalog_path = tmp_path / "kimi.json"
    catalog_path.write_text(
        json.dumps({"models": [{"slug": "kimi-k2.5"}, {"slug": "kimi-k2"}]}),
        encoding="utf-8",
    )
    (tmp_path / "kimi.config.toml").write_text(
        'model = "kimi-k2.5"\n'
        'model_provider = "reverso_kimi"\n'
        f'model_catalog_json = "{catalog_path}"\n',
        encoding="utf-8",
    )
    return config_path


def _codex_success_output(marker: str = CLIENT_EXPECTED) -> str:
    return (
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": marker},
            }
        )
        + "\n"
        + json.dumps({"type": "turn.completed"})
        + "\n"
    )


def test_live_opt_in_requires_exact_value() -> None:
    for env in ({}, {LIVE_PROOF_ENV: "true"}, {LIVE_PROOF_ENV: "01"}):
        with pytest.raises(ProofFailure, match="live_proof_not_enabled"):
            require_live_opt_in(env)
    require_live_opt_in({LIVE_PROOF_ENV: "1"})


def test_run_proof_builds_exact_schema_v1_manifest() -> None:
    manifest = run_proof(FakeProbe())

    assert manifest["schema_version"] == 1
    assert manifest["provider"] == "kimi"
    assert manifest["loopback"] is True
    assert manifest["overall"] == "pass"
    assert tuple(row["id"] for row in manifest["checks"]) == REQUIRED_CHECK_IDS
    assert all(row["result"] == "pass" for row in manifest["checks"])
    assert all(set(row) <= ALLOWED_CHECK_KEYS for row in manifest["checks"])


@pytest.mark.parametrize("lane", REQUIRED_CHECK_IDS)
def test_any_failed_or_skipped_lane_keeps_gate_red(lane: str) -> None:
    manifest = run_proof(FakeProbe(fail_lane=lane))

    assert manifest["overall"] == "fail"
    by_id = {row["id"]: row for row in manifest["checks"]}
    assert by_id[lane] == {
        "id": lane,
        "result": "fail",
        "error_category": f"{lane}_failed",
    }


def test_fallback_discovery_cannot_satisfy_live_gate() -> None:
    manifest = run_proof(FakeProbe(discovery_source="fallback"))

    row = manifest["checks"][0]
    assert row["result"] == "fail"
    assert row["error_category"] == "fallback_model_discovery"
    assert manifest["overall"] == "fail"


@pytest.mark.parametrize(
    "deltas",
    [
        {"responses": 0, "messages": 1},
        {"responses": 1, "messages": 0},
        {"responses": 1},
        {"responses": 1, "messages": -1},
        {"responses": 3, "messages": 1},
    ],
)
def test_headroom_requires_positive_controlled_delta_per_lane(
    deltas: dict[str, int],
) -> None:
    manifest = run_proof(FakeProbe(headroom_deltas=deltas))
    row = {check["id"]: check for check in manifest["checks"]}["headroom"]
    assert row["result"] == "fail"
    assert row["error_category"] == "non_attributable_headroom"


def test_non_loopback_route_keeps_gate_red() -> None:
    manifest = run_proof(FakeProbe(loopback_route="https://example.com:64946"))
    row = {check["id"]: check for check in manifest["checks"]}["loopback"]
    assert row["result"] == "fail"
    assert row["error_category"] == "non_loopback_route"
    assert manifest["loopback"] is False


def test_manifest_rejects_missing_duplicate_and_skipped_rows() -> None:
    manifest = run_proof(FakeProbe())
    missing = {**manifest, "checks": manifest["checks"][:-1], "overall": "fail"}
    with pytest.raises(ProofFailure, match="invalid_required_checks"):
        validate_manifest(missing)

    duplicate = {
        **manifest,
        "checks": [manifest["checks"][0], *manifest["checks"][:-1]],
        "overall": "fail",
    }
    with pytest.raises(ProofFailure, match="invalid_required_checks"):
        validate_manifest(duplicate)

    skipped = json.loads(json.dumps(manifest))
    skipped["checks"][2]["result"] = "skip"
    skipped["overall"] = "fail"
    with pytest.raises(ProofFailure, match="invalid_check_result"):
        validate_manifest(skipped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "do not persist"),
        ("response_text", "do not persist"),
        ("authorization", "Bearer secret"),
        ("raw_log", "secret log"),
        ("headers", {"x": "y"}),
    ],
)
def test_manifest_rejects_forbidden_evidence_fields(field: str, value: Any) -> None:
    manifest = run_proof(FakeProbe())
    manifest["checks"][0][field] = value
    with pytest.raises(ProofFailure, match="forbidden_evidence_field"):
        validate_manifest(manifest)


def test_manifest_rejects_secret_shaped_values() -> None:
    manifest = run_proof(FakeProbe())
    manifest["checks"][0]["model_id"] = "Bearer secret-value"
    with pytest.raises(ProofFailure, match="redaction_failure"):
        validate_manifest(manifest)


def test_manifest_write_is_atomic_private_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "evidence" / "kimi-live-proof.json"
    manifest = run_proof(FakeProbe())

    write_manifest(target, manifest)

    assert json.loads(target.read_text(encoding="utf-8")) == manifest
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".*.tmp"))


def test_http_probe_subprocess_failure_is_categorized_without_output() -> None:
    sentinel = "secret-child-output-must-not-surface"

    def runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["codex"], returncode=2, stdout=sentinel, stderr=sentinel
        )

    probe = HttpLiveProofProbe(runner=runner, log_paths=())
    with pytest.raises(ProofFailure, match="client_failed") as exc_info:
        probe._subprocess(["codex"])
    assert sentinel not in str(exc_info.value)


def test_http_probe_rejects_non_loopback_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tripwire(*_: Any, **__: Any) -> None:
        raise AssertionError("network must not run for a non-loopback route")

    monkeypatch.setattr("reverso.kimi_live_proof.httpx.request", tripwire)
    probe = HttpLiveProofProbe(base_url="https://example.com:64946", log_paths=())

    with pytest.raises(ProofFailure, match="non_loopback_route"):
        probe.model_discovery()


def test_public_live_discovery_claim_without_nonce_bound_proof_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("reverso.kimi_live_proof.secrets.token_hex", lambda _: "a" * 64)

    def request(method: str, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_discovery_source": "live",
                "data": [{"id": "kimi-k2.5"}],
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("reverso.kimi_live_proof.httpx.request", request)
    probe = HttpLiveProofProbe(log_paths=())

    with pytest.raises(ProofFailure, match="fallback_model_discovery"):
        probe.model_discovery()


def test_http_probe_codex_uses_live_profile_default_independent_of_proof_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_codex_fixture(tmp_path)
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(config_path))
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        return subprocess.CompletedProcess(
            argv, 0, stdout=_codex_success_output(), stderr=""
        )

    probe = HttpLiveProofProbe(runner=runner, log_paths=())
    probe._live_models = ("kimi-k2.5", "kimi-k2")

    observation = probe.codex("kimi-k2")

    assert observation["model_id"] == "kimi-k2.5"
    assert calls[0][:5] == ["codex", "exec", "-p", "kimi", "--skip-git-repo-check"]


def test_http_probe_codex_rejects_catalog_drift_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_codex_fixture(tmp_path)
    catalog_path = tmp_path / "kimi.json"
    catalog_path.write_text(
        json.dumps({"models": [{"slug": "stale-kimi"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(config_path))

    def tripwire(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess must not run for a stale catalog")

    probe = HttpLiveProofProbe(runner=tripwire, log_paths=())
    probe._live_models = ("kimi-k2.5", "kimi-k2")

    with pytest.raises(ProofFailure, match="codex_profile_mismatch"):
        probe.codex("kimi-k2.5")


def test_http_probe_codex_rejects_non_loopback_provider_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_codex_fixture(tmp_path, base_url="https://example.com/kimi/v1")
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(config_path))

    def tripwire(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess must not run for a non-loopback provider")

    probe = HttpLiveProofProbe(runner=tripwire, log_paths=())
    probe._live_models = ("kimi-k2.5", "kimi-k2")

    with pytest.raises(ProofFailure, match="codex_profile_mismatch"):
        probe.codex("kimi-k2.5")


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not-json\n",
        _codex_success_output("WRONG"),
        _codex_success_output(CLIENT_PROMPT),
    ],
)
def test_http_probe_codex_zero_exit_requires_completed_controlled_marker(
    stdout: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_codex_fixture(tmp_path)
    monkeypatch.setenv("REVERSO_CODEX_CONFIG", str(config_path))

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    probe = HttpLiveProofProbe(runner=runner, log_paths=())
    probe._live_models = ("kimi-k2.5", "kimi-k2")

    with pytest.raises(ProofFailure, match="client_completion_invalid"):
        probe.codex("kimi-k2.5")


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "WRONG\n",
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": CLIENT_PROMPT,
            }
        ),
    ],
)
def test_http_probe_claude_zero_exit_requires_controlled_marker(stdout: str) -> None:
    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    probe = HttpLiveProofProbe(runner=runner, log_paths=())

    with pytest.raises(ProofFailure, match="client_completion_invalid"):
        probe.claude_code("kimi-k2.5")


def test_http_probe_claude_requires_exact_parsed_success_result() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": CLIENT_EXPECTED,
        }
    )

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[-1] == CLIENT_PROMPT
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    observation = HttpLiveProofProbe(runner=runner, log_paths=()).claude_code(
        "kimi-k2.5"
    )

    assert observation["status"] == 0


def test_unrelated_headroom_traffic_cannot_mask_missing_controlled_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = HttpLiveProofProbe(log_paths=())

    def fake_request(method: str, route: str, **_: Any) -> tuple[dict[str, Any], int]:
        assert method == "GET"
        assert route.startswith("/usage/headroom/proof/")
        return {"proof": {"responses": 0, "messages": 0}}, 1

    monkeypatch.setattr(probe, "_json_request", fake_request)
    probe._headroom_deltas = {
        "responses": probe._correlated_headroom("a" * 64, "responses"),
        "messages": 1,
    }

    with pytest.raises(ProofFailure, match="non_attributable_headroom"):
        probe.headroom()


def test_redaction_requires_known_credential_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "missing"))
    log = tmp_path / "proxy.log"
    log.write_text("", encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))

    with pytest.raises(ProofFailure, match="redaction_sentinel_unavailable"):
        probe.redaction()


@pytest.mark.parametrize(
    "message",
    [
        "API Error: 401 authentication_error: invalid auth token",
        "access_token field is unavailable",
        "refresh token must be renewed",
    ],
)
def test_redaction_allows_benign_credential_terms_without_assigned_values(
    message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_BEARER_TOKEN", "unrelated-active-sentinel")
    log = tmp_path / "proxy.log"
    log.write_text("", encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))
    probe._captured_text.append(message)

    assert probe.redaction() == {"provider": "kimi", "status": 0}


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer assigned-secret",
        '"access_token": "assigned-secret"',
        "refresh token=assigned-secret",
        "api_key: assigned-secret",
        "Bearer assigned-secret",
    ],
)
def test_redaction_rejects_assigned_secret_shapes(
    message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_BEARER_TOKEN", "unrelated-active-sentinel")
    log = tmp_path / "proxy.log"
    log.write_text("", encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))
    probe._captured_text.append(message)

    with pytest.raises(ProofFailure, match="redaction_failure"):
        probe.redaction()


@pytest.mark.parametrize("source", ["captured", "log"])
def test_redaction_rejects_unlabeled_active_credential_value(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "opaque-active-kimi-value-7f0c9a"
    monkeypatch.setenv("KIMI_BEARER_TOKEN", sentinel)
    log = tmp_path / "proxy.log"
    log.write_text("existing historical text\n", encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))
    if source == "captured":
        probe._captured_text.append(sentinel)
    else:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(sentinel)

    with pytest.raises(ProofFailure, match="redaction_failure") as exc_info:
        probe.redaction()
    assert sentinel not in str(exc_info.value)


def test_redaction_scans_from_start_after_log_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "opaque-active-kimi-value-truncated"
    monkeypatch.setenv("KIMI_BEARER_TOKEN", sentinel)
    log = tmp_path / "proxy.log"
    log.write_text("historical line\n" * 20, encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))
    log.write_text(sentinel, encoding="utf-8")

    with pytest.raises(ProofFailure, match="redaction_failure"):
        probe.redaction()


def test_redaction_scans_replacement_log_after_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "opaque-active-kimi-value-rotated"
    monkeypatch.setenv("KIMI_BEARER_TOKEN", sentinel)
    log = tmp_path / "proxy.log"
    log.write_text("historical line\n", encoding="utf-8")
    probe = HttpLiveProofProbe(log_paths=(log,))
    log.rename(tmp_path / "proxy.log.1")
    log.write_text(sentinel, encoding="utf-8")

    with pytest.raises(ProofFailure, match="redaction_failure"):
        probe.redaction()


def test_runner_without_opt_in_writes_no_manifest(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    env = os.environ.copy()
    env.pop(LIVE_PROOF_ENV, None)

    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/kimi-live-proof.py",
            "--manifest",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "live_proof_not_enabled" in result.stderr
    assert not target.exists()
