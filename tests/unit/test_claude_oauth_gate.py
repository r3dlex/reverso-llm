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


def test_list_models_uses_live_anthropic_listing_with_oauth_bearer(tmp_path) -> None:
    """list_models fetches the live Anthropic /v1/models with the OAuth bearer."""
    import asyncio

    import httpx

    from reverso.protocols.adapters.claude import ClaudeAdapter

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["beta"] = request.headers.get("anthropic-beta")
        return httpx.Response(
            200, json={"data": [{"id": "claude-opus-4-8", "type": "model"}]}
        )

    adapter = ClaudeAdapter(
        auth=auth,
        cli_runner=lambda prompt, model: "ok",
        models_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    models = asyncio.run(adapter.list_models())

    assert [m["id"] for m in models.data] == ["claude-opus-4-8"]
    assert captured["path"] == "/v1/models"
    assert captured["auth"] == f"Bearer {_ACCESS_TOKEN}"
    assert captured["beta"] == "oauth-2025-04-20"


def test_list_models_falls_back_to_cli_aliases_when_unauthenticated(tmp_path) -> None:
    """Without OAuth, list_models degrades to the always-valid CLI aliases."""
    import asyncio

    from reverso.protocols.adapters.claude import ClaudeAdapter

    auth = ClaudeOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        keychain_reader=lambda: None,
    )

    def _factory_must_not_run():  # pragma: no cover
        raise AssertionError("models client must not be built without auth")

    adapter = ClaudeAdapter(
        auth=auth,
        cli_runner=lambda prompt, model: "ok",
        models_client_factory=_factory_must_not_run,
    )

    models = asyncio.run(adapter.list_models())

    assert [m["id"] for m in models.data] == ["opus", "sonnet", "haiku"]


def test_adapter_streams_fixture_event_order(tmp_path) -> None:
    """The stream emits the Codex-observed Responses event order.

    Uses the buffered fallback path (no stream_cli_runner injected) so the
    asserted event sequence has exactly one delta. Multi-chunk streaming is
    covered by test_stream_runner_emits_multiple_deltas below.
    """
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter, _StreamPreflightError

    async def _missing_stream(prompt, model):
        raise _StreamPreflightError("no streaming runner in this test")
        yield  # pragma: no cover - keep the function an async generator

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    adapter = ClaudeAdapter(
        auth=auth,
        cli_runner=lambda prompt, model: "Hi there.",
        stream_cli_runner=_missing_stream,
    )
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


def test_stream_runner_emits_multiple_deltas(tmp_path) -> None:
    """Fake async-generator runner: multiple deltas concatenate to full text.

    Asserts the B2 contract: an injected stream_cli_runner that yields several
    text fragments produces one response.output_text.delta per fragment, the
    canonical event sequence (collapsed across consecutive deltas) is
    preserved, the concatenated deltas equal the full assistant text, and the
    completed envelope is persisted for previous_response_id lookup.
    """
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    chunks = ["Hel", "lo", " ", "world"]

    async def _runner(prompt: str, model: str):
        for chunk in chunks:
            yield chunk

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)

    def _cli_must_not_run(prompt: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("buffered CLI must not run when streaming succeeds")

    adapter = ClaudeAdapter(
        auth=auth,
        cli_runner=_cli_must_not_run,
        stream_cli_runner=_runner,
    )
    request = ResponsesRequest(
        model="claude-sonnet-4-20250514", input="Say hi.", stream=True
    )

    async def _collect():
        out = []
        async for event in adapter.stream_response(request):
            out.append((event.event, event.data))
        return out

    events = asyncio.run(_collect())
    types = [event_type for event_type, _ in events]

    def _collapse(seq: list[str]) -> list[str]:
        collapsed: list[str] = []
        for entry in seq:
            if (
                entry == "response.output_text.delta"
                and collapsed
                and collapsed[-1] == "response.output_text.delta"
            ):
                continue
            collapsed.append(entry)
        return collapsed

    assert _collapse(types) == [
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

    delta_payloads = [
        data["delta"]
        for event_type, data in events
        if event_type == "response.output_text.delta"
    ]
    assert delta_payloads == chunks
    assert "".join(delta_payloads) == "Hello world"

    completed = next(
        data for event_type, data in events if event_type == "response.completed"
    )
    assert completed["response"]["status"] == "completed"
    response_id = completed["response"]["id"]

    stored = asyncio.run(adapter.get_response(response_id))
    assert stored.status == "completed"
    assert stored.output[0]["content"][0]["text"] == "Hello world"


def test_stream_runner_fallback_when_runner_fails_before_first_chunk(tmp_path) -> None:
    """Pre-first-chunk runner failure -> buffered CLI serves the request.

    Mirrors the named B2 fallback conditions ((a) nonzero exit before the
    first chunk, (b) first-chunk parse error) by raising _StreamPreflightError
    from a fake runner before yielding anything. The buffered cli_runner must
    be invoked and the canonical single-delta replay served.
    """
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter, _StreamPreflightError

    async def _failing_runner(prompt: str, model: str):
        raise _StreamPreflightError("simulated nonzero exit before first chunk")
        yield  # pragma: no cover - keeps the function an async generator

    cli_calls: list[tuple[str, str]] = []

    def _cli(prompt: str, model: str) -> str:
        cli_calls.append((prompt, model))
        return "buffered text"

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    adapter = ClaudeAdapter(
        auth=auth, cli_runner=_cli, stream_cli_runner=_failing_runner
    )
    request = ResponsesRequest(
        model="claude-sonnet-4-20250514", input="Say hi.", stream=True
    )

    async def _collect():
        out = []
        async for event in adapter.stream_response(request):
            out.append((event.event, event.data))
        return out

    events = asyncio.run(_collect())
    assert cli_calls, "buffered cli_runner must be invoked on pre-first-chunk failure"

    types = [event_type for event_type, _ in events]
    assert types == [
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
    delta_payloads = [
        data["delta"]
        for event_type, data in events
        if event_type == "response.output_text.delta"
    ]
    assert delta_payloads == ["buffered text"]


def test_stream_runner_mid_stream_failure_propagates(tmp_path) -> None:
    """Mid-stream runner failure -> exception propagates after first delta.

    The gateway's responses_app contract (response.failed event + [DONE])
    handles this OUTSIDE the adapter; the adapter must NOT silently swap to
    the buffered runner once any delta has been emitted. This test asserts
    the adapter yields at least one delta and then re-raises the iterator
    error unwrapped.
    """
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    class _MidStreamBoom(RuntimeError):
        pass

    async def _runner(prompt: str, model: str):
        yield "Hello"
        raise _MidStreamBoom("upstream went away mid-stream")

    def _cli_must_not_run(prompt: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("no silent fallback after a delta has been emitted")

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    adapter = ClaudeAdapter(
        auth=auth, cli_runner=_cli_must_not_run, stream_cli_runner=_runner
    )
    request = ResponsesRequest(
        model="claude-sonnet-4-20250514", input="Say hi.", stream=True
    )

    async def _drain():
        collected = []
        with pytest.raises(_MidStreamBoom):
            async for event in adapter.stream_response(request):
                collected.append((event.event, event.data))
        return collected

    events = asyncio.run(_drain())
    types = [event_type for event_type, _ in events]
    assert (
        "response.output_text.delta" in types
    ), "mid-stream failure test requires at least one delta before the boom"
    delta_payloads = [
        data["delta"]
        for event_type, data in events
        if event_type == "response.output_text.delta"
    ]
    assert delta_payloads == ["Hello"]
    assert "response.completed" not in types


def test_extract_assistant_text_ignores_non_text_events() -> None:
    """The stream-json parser must only surface assistant text content.

    Per A3 evidence the CLI interleaves system lifecycle, thinking parts,
    rate_limit_event and the terminal result event around the assistant text.
    The gateway must never stream reasoning or metadata as user-visible deltas.
    """
    from reverso.protocols.adapters.claude import _extract_assistant_text

    assert _extract_assistant_text({"type": "system", "subtype": "init"}) == ""
    assert _extract_assistant_text({"type": "result", "result": "ok"}) == ""
    assert (
        _extract_assistant_text(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "redact me"},
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": " world"},
                    ]
                },
            }
        )
        == "Hello world"
    )
    assert (
        _extract_assistant_text(
            {"type": "assistant", "message": {"content": "not a list"}}
        )
        == ""
    )
    assert _extract_assistant_text({"type": "rate_limit_event"}) == ""


def test_stream_runner_never_consumes_env_oauth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The streaming adapter must never authenticate from env tokens.

    With ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN in the parent env but
    NO claudeAiOauth artifact present, the streaming path must refuse to
    serve the request rather than infer auth from those env values.
    """
    import asyncio

    from reverso.protocols.adapter import ResponsesRequest
    from reverso.protocols.adapters.claude import ClaudeAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-ignored")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token-should-be-ignored")

    async def _runner_must_not_run(prompt: str, model: str):  # pragma: no cover
        raise AssertionError("streaming runner must not run without OAuth artifact")
        yield  # pragma: no cover

    def _cli_must_not_run(prompt: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("buffered CLI must not run without OAuth artifact")

    auth = ClaudeOAuthAuth(
        credentials_path=tmp_path / "missing.json",
        keychain_reader=lambda: None,
    )
    adapter = ClaudeAdapter(
        auth=auth,
        cli_runner=_cli_must_not_run,
        stream_cli_runner=_runner_must_not_run,
    )
    request = ResponsesRequest(
        model="claude-sonnet-4-20250514", input="hi", stream=True
    )

    async def _drain():
        async for _ in adapter.stream_response(request):  # pragma: no cover
            pass

    with pytest.raises(ClaudeAuthError):
        asyncio.run(_drain())


def test_default_claude_runner_honors_profile_workspace_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from reverso.protocols.adapters import claude as claude_module
    from reverso.protocols.adapters.claude import ClaudeAdapter
    from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen: dict[str, object] = {}

    def fake_run_bounded_cli(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cwd"] = kwargs.get("cwd")
        return "ok\n"

    monkeypatch.setattr(claude_module, "run_bounded_cli", fake_run_bounded_cli)
    adapter = ClaudeAdapter(auth=auth)

    token = CURRENT_PROFILE_WORKSPACE.set(str(workspace))
    try:
        assert adapter._run_claude_cli("prompt", "claude-opus-4-8") == "ok"
    finally:
        CURRENT_PROFILE_WORKSPACE.reset(token)

    assert seen["cwd"] == str(workspace)
    assert seen["argv"] == [
        "claude",
        "--print",
        "--model",
        "claude-opus-4-8",
        "--",
        "prompt",
    ]


def test_child_env_scrubs_routing_auth_and_keeps_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI child env never carries ANTHROPIC_BASE_URL/AUTH_TOKEN/API_KEY.

    ADR 0008 loop guard: when Reverso serves claude on the inbound Anthropic
    surface, a caller may set ANTHROPIC_BASE_URL (pointing at Reverso) and pass
    ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY. If the spawned claude CLI inherited
    those it would re-enter Reverso (infinite loop) or use a hijacked credential.
    The adapter must scrub all three from the CHILD env before injecting the
    subscription OAuth token, while leaving the parent os.environ untouched.
    """
    import os

    from reverso.protocols.adapters.claude import _child_env_for_cli

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:64946")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "caller-token-should-be-scrubbed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-scrubbed")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

    child_env = _child_env_for_cli("live-oauth-token")

    # The routing/auth keys are absent from the child env handed to the CLI spine.
    assert "ANTHROPIC_BASE_URL" not in child_env
    assert "ANTHROPIC_AUTH_TOKEN" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    # The subscription token is injected and an unrelated var (PATH) survives.
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == "live-oauth-token"
    assert "PATH" in child_env
    # The parent process env is NOT mutated (the falsifiable gate asserts this).
    assert os.environ.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:64946"
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "caller-token-should-be-scrubbed"
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-should-be-scrubbed"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_default_claude_stream_runner_honors_profile_workspace_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import asyncio

    from reverso.protocols.adapters import claude as claude_module
    from reverso.protocols.adapters.claude import ClaudeAdapter
    from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

    cred_file = tmp_path / ".credentials.json"
    cred_file.write_text(_artifact(expires_at=_future_ms()), encoding="utf-8")
    auth = ClaudeOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen: dict[str, object] = {}

    async def fake_stream_bounded_cli(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cwd"] = kwargs.get("cwd")
        yield json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            }
        )

    monkeypatch.setattr(claude_module, "stream_bounded_cli", fake_stream_bounded_cli)
    adapter = ClaudeAdapter(auth=auth)

    async def _drain() -> list[str]:
        token = CURRENT_PROFILE_WORKSPACE.set(str(workspace))
        try:
            return [
                chunk
                async for chunk in adapter._default_stream_cli_runner(
                    "prompt", "claude-opus-4-8"
                )
            ]
        finally:
            CURRENT_PROFILE_WORKSPACE.reset(token)

    assert asyncio.run(_drain()) == ["ok"]
    assert seen["cwd"] == str(workspace)
    assert seen["argv"] == [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        "claude-opus-4-8",
        "--",
        "prompt",
    ]
