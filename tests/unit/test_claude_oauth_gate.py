"""Falsifiable subscription-OAuth gate for the Claude adapter (ADR 0002 D3).

These tests assert that the Claude adapter authenticates by reading the
``claudeAiOauth`` artifact DIRECTLY and never by inferring auth from the
presence of ANTHROPIC_API_KEY (auth-by-elimination). The gate is constructed to
FAIL if the resolved method is an api-key path or if ANTHROPIC_API_KEY /
CLAUDE_CODE_OAUTH_TOKEN is consumed from the environment.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from reverso.protocols.adapters.claude import (
    OAUTH_METHOD,
    ClaudeAuthError,
    ClaudeOAuthAuth,
)

_ACCESS_TOKEN = "oauth-access-SECRET-7f3a9c1e2b"
_REFRESH_TOKEN = "oauth-refresh-SECRET-9d8e7f6a5b"


def _artifact(*, expires_at: int | None, access_token: str = _ACCESS_TOKEN) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access_token,
                "refreshToken": _REFRESH_TOKEN,
                "expiresAt": expires_at,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "default",
            }
        }
    )


def _future_ms() -> int:
    return int((time.time() + 3600) * 1000)


def _past_ms() -> int:
    return int((time.time() - 3600) * 1000)


@pytest.fixture(autouse=True)
def _no_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee no api-key/env-token is present so the gate is meaningful."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_resolves_oauth_path_directly(tmp_path) -> None:
    """With a claudeAiOauth artifact and NO api key, auth is the OAuth path.

    This is the core falsifiable assertion: authenticated is True AND the method
    is the OAuth path. If the adapter instead resolved an api-key method, the
    method assertion below would fail the gate.
    """
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")

    auth = ClaudeOAuthAuth(
        credentials_path=cred_file,
        keychain_reader=lambda: None,
    )
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    # Falsifiability: the method must NOT be a metered api-key path.
    assert resolution.method not in {"anthropic", "api_key", "api-key"}
    assert resolution.subscription_type == "max"
    assert resolution.details.get("source") == "credentials_file"


def test_keychain_artifact_resolves_oauth_path() -> None:
    """The macOS Keychain artifact resolves the same OAuth path directly."""
    auth = ClaudeOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(expires_at=_future_ms()),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("source") == "keychain"


def test_env_api_key_is_never_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN must not authenticate.

    Auth-by-elimination would treat the api key as proof of auth. Here, with NO
    claudeAiOauth artifact present, the presence of the env tokens must NOT make
    the gate pass. This is the falsifying counter-case.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-ignored")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token-should-be-ignored")

    auth = ClaudeOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: None,
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    # The method label is still the OAuth path (the adapter has no api-key path),
    # and crucially it never reports an api-key method as authenticated.
    assert resolution.method == OAUTH_METHOD


def test_api_key_present_but_oauth_missing_blocks_calls() -> None:
    """bearer_token must refuse when only an env api key would be available."""
    auth = ClaudeOAuthAuth(credentials_path=None, keychain_reader=lambda: None)
    import asyncio

    with pytest.raises(ClaudeAuthError):
        asyncio.run(auth.bearer_token())


def test_missing_auth_is_unauthenticated(tmp_path) -> None:
    """No artifact anywhere -> unauthenticated with a non-secret reason."""
    auth = ClaudeOAuthAuth(
        credentials_path=tmp_path / "does-not-exist.json",
        keychain_reader=lambda: None,
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "no_claude_oauth_artifact"


def test_expired_auth_is_observable(tmp_path) -> None:
    """An expired expiresAt is observable, so auth must fail closed."""
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_past_ms()), encoding="utf-8")

    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.details.get("reason") == "expired"


def test_artifact_without_access_token_is_unauthenticated(tmp_path) -> None:
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(
        _artifact(expires_at=_future_ms(), access_token=""), encoding="utf-8"
    )

    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.details.get("reason") == "no_access_token"


def test_no_token_substring_in_resolution_or_logs(tmp_path, caplog) -> None:
    """No token material may appear in the resolution summary or any log output."""
    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")

    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    with caplog.at_level(logging.DEBUG):
        resolution = auth.resolve()

    serialized = json.dumps(resolution.details)
    assert _ACCESS_TOKEN not in serialized
    assert _REFRESH_TOKEN not in serialized
    assert "SECRET" not in serialized
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRET" not in log_text
    assert _ACCESS_TOKEN not in log_text


def test_adapter_auth_failure_is_bounded(tmp_path) -> None:
    """The adapter surfaces a bounded auth error rather than calling upstream."""
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    auth = ClaudeOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        keychain_reader=lambda: None,
    )

    def _cli_runner_must_not_run(prompt: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("CLI must not run when auth fails")

    adapter = ClaudeAdapter(auth=auth, cli_runner=_cli_runner_must_not_run)
    request = ResponsesRequest(model="claude-sonnet-4-20250514", input="hi")

    with pytest.raises(ClaudeAuthError):
        asyncio.run(adapter.create_response(request))


def test_adapter_maps_output_to_envelope(tmp_path) -> None:
    """A successful completion maps into a Responses envelope; secrets stay out."""
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)

    adapter = ClaudeAdapter(auth=auth, cli_runner=lambda prompt, model: "Hello there.")
    request = ResponsesRequest(model="claude-sonnet-4-20250514", input="hi")
    envelope = asyncio.run(adapter.create_response(request))

    assert envelope.status == "completed"
    assert envelope.output[0]["content"][0]["text"] == "Hello there."
    assert _ACCESS_TOKEN not in json.dumps(envelope.output)


def test_adapter_streams_fixture_event_order(tmp_path) -> None:
    """The stream emits the Codex-observed Responses event order."""
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    adapter = ClaudeAdapter(auth=auth, cli_runner=lambda prompt, model: "Hi there.")
    request = ResponsesRequest(
        model="claude-sonnet-4-20250514", input="Say hi.", stream=True
    )

    async def _collect() -> list[str]:
        return [event.event async for event in adapter.stream_response(request)]

    events = asyncio.run(_collect())
    assert events == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
