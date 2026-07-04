from __future__ import annotations

from collections.abc import AsyncIterator
import subprocess
import os
from typing import Any

import pytest

from reverso.protocols.adapters.codex_live_proof import (
    DIRECT_LIVE_PROOF_ENV,
    LIVE_PROOF_ENV,
    OFFICIAL_LIVE_PROOF_ENV,
    CodexLiveProofSkipped,
    LiveProofReport,
    auth_readiness,
    require_live_opt_in,
    run_direct_live_proof,
    run_official_cli_live_proof,
)
from reverso.protocols.auth import AuthResolution, FakeAuth


class TripwireAuth(FakeAuth):
    def resolve(self) -> AuthResolution:  # pragma: no cover - should not be reached.
        raise AssertionError("auth was touched without opt-in")

    async def bearer_token(self) -> str:  # pragma: no cover - should not be reached.
        raise AssertionError("token was touched without opt-in")


class FakeDirectUpstream:
    def __init__(self) -> None:
        self.called = False

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.called = True
        return {
            "id": "resp_live_fake",
            "model": body["model"],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover - unused in harness test.
        if False:
            yield {}

    async def list_models(
        self, *, token: str
    ) -> list[dict[str, Any]]:  # pragma: no cover
        return []


def _auth() -> FakeAuth:
    return FakeAuth(
        AuthResolution(
            authenticated=True,
            method="oauth",
            subscription_type="chatgpt-plus",
            details={"source": "unit-test", "token_fixture": "opaque-fixture-value"},
        ),
        token="opaque-fixture-bearer",
    )


def test_live_opt_in_fails_closed_by_default() -> None:
    with pytest.raises(CodexLiveProofSkipped):
        require_live_opt_in("direct", {})
    with pytest.raises(CodexLiveProofSkipped):
        require_live_opt_in("official", {})


def test_official_lane_accepts_master_or_lane_opt_in() -> None:
    require_live_opt_in("official", {LIVE_PROOF_ENV: "1"})
    require_live_opt_in("official", {OFFICIAL_LIVE_PROOF_ENV: "1"})


def test_direct_lane_requires_direct_specific_opt_in() -> None:
    with pytest.raises(CodexLiveProofSkipped):
        require_live_opt_in("direct", {LIVE_PROOF_ENV: "1"})
    require_live_opt_in("direct", {DIRECT_LIVE_PROOF_ENV: "1"})


@pytest.mark.asyncio
async def test_direct_live_proof_skip_does_not_touch_auth_or_upstream() -> None:
    upstream = FakeDirectUpstream()
    with pytest.raises(CodexLiveProofSkipped):
        await run_direct_live_proof(
            TripwireAuth(AuthResolution(True, "oauth")), env={}, upstream=upstream
        )
    assert upstream.called is False


@pytest.mark.asyncio
async def test_direct_live_proof_returns_secret_free_shape_report() -> None:
    report = await run_direct_live_proof(
        _auth(), env={DIRECT_LIVE_PROOF_ENV: "1"}, upstream=FakeDirectUpstream()
    )

    public = report.to_public_dict()
    assert report.status == "passed"
    assert public["token_present"] is True
    assert public["response_shape_keys"] == [
        "id",
        "model",
        "object",
        "output",
        "status",
        "usage",
    ]
    assert "opaque-fixture" not in repr(public)


@pytest.mark.asyncio
async def test_auth_readiness_redacts_secret_details() -> None:
    public = await auth_readiness(_auth(), probe_token=True)

    assert public["auth_authenticated"] is True
    assert public["auth_method"] == "oauth"
    assert public["auth_source"] == "unit-test"
    assert public["token_present"] is True
    assert "opaque-fixture" not in repr(public)


def test_official_cli_live_proof_skip_does_not_run_subprocess() -> None:
    def tripwire(**_: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess was touched without opt-in")

    with pytest.raises(CodexLiveProofSkipped):
        run_official_cli_live_proof(env={}, runner=tripwire)


def test_official_cli_live_proof_sanitizes_json_lines() -> None:
    def runner(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"type":"thread.started","model":"gpt-5.5"}\n{"type":"turn.completed","usage":{"output_tokens":1}}\n',
            stderr="opaque stderr should not be reported",
        )

    report = run_official_cli_live_proof(
        env={OFFICIAL_LIVE_PROOF_ENV: "1"}, runner=runner
    )
    public = report.to_public_dict()

    assert public["status"] == "passed"
    assert public["model"] == "gpt-5.5"
    assert public["usage_present"] is True
    assert "opaque-fixture" not in repr(public)


def test_live_proof_report_public_dict_has_only_expected_keys() -> None:
    public = LiveProofReport(
        lane="direct", status="skipped", reason="no opt-in"
    ).to_public_dict()
    assert set(public) == {
        "lane",
        "status",
        "auth_authenticated",
        "auth_method",
        "auth_source",
        "token_present",
        "model",
        "response_shape_keys",
        "usage_present",
        "rate_limit_present",
        "error_type",
        "reason",
    }


def test_empty_env_override_ignores_ambient_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DIRECT_LIVE_PROOF_ENV, "1")
    with pytest.raises(CodexLiveProofSkipped):
        require_live_opt_in("direct", {})


def test_direct_script_opt_in_reports_validate_only_auth_boundary() -> None:
    import subprocess as _subprocess

    result = _subprocess.run(
        ["scripts/codex-live-proof.py", "--lane", "direct", "--json"],
        env={**os.environ, DIRECT_LIVE_PROOF_ENV: "1"},
        text=True,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0
    assert "validate-only" in result.stdout
    assert "auth.json" not in result.stdout
