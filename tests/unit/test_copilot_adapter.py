"""Unit tests for the Copilot adapter ported direct-forward spine.

All credentials are FAKE fixtures: no Keychain, real GitHub login, or network.
Covers OAuth token read, bearer-token refresh/exchange, the runtime.protocols
contract surface, timeout handling, and that no token substring is ever logged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
import logging
import time

import httpx
import pytest

from reverso.protocols.feature_policy import UnsupportedFeature
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
)
from reverso.protocols.adapters.copilot import (
    CopilotAdapter,
    CopilotAuth,
    CopilotUpstreamError,
    GITHUB_TOKEN_URL,
    _raise_for_upstream_status,
)

FAKE_OAUTH_TOKEN = "gho_FAKEoauthTOKENvalue1234567890"
FAKE_BEARER_TOKEN = "tid=FAKEbearerTOKENvalue0987654321"


def _write_hosts(config_dir, token: str = FAKE_OAUTH_TOKEN) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "hosts.json").write_text(
        json.dumps({"github.com": {"oauth_token": token}})
    )


def _mock_client(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


class _AsyncBytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_read_oauth_token_from_hosts(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    auth = CopilotAuth(config_dir=config_dir)

    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == "copilot_oauth"


def test_resolve_unauthenticated_when_missing(tmp_path):
    auth = CopilotAuth(config_dir=tmp_path / "github-copilot")

    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == "copilot_oauth"


async def test_bearer_token_exchanges_and_caches(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GITHUB_TOKEN_URL
        assert request.headers["Authorization"] == f"token {FAKE_OAUTH_TOKEN}"
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600},
        )

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)

    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(
                GITHUB_TOKEN_URL,
                headers={"Authorization": f"token {auth._read_oauth_token()}"},
            )
        auth._copilot_token = response.json()
        auth._save_cached_token()

    await _exchange()
    token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN
    assert (config_dir / "token.json").exists()
    assert calls["n"] == 1


async def test_bearer_token_refreshes_expired(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    (config_dir / "token.json").write_text(
        json.dumps({"token": "tid=STALEfake", "expires_at": time.time() - 10})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600},
        )

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)
    auth._exchange_token = _patched_exchange(auth, transport)  # type: ignore[method-assign]

    token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN


def _patched_exchange(auth: CopilotAuth, transport: httpx.MockTransport):
    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(
                GITHUB_TOKEN_URL,
                headers={"Authorization": f"token {auth._read_oauth_token()}"},
            )
        auth._copilot_token = response.json()
        auth._save_cached_token()

    return _exchange


async def test_bearer_token_timeout_propagates(tmp_path):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("token endpoint timed out")

    auth = CopilotAuth(config_dir=config_dir)
    transport = httpx.MockTransport(handler)

    async def _exchange() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(GITHUB_TOKEN_URL)

    auth._exchange_token = _exchange  # type: ignore[method-assign]

    with pytest.raises(httpx.TimeoutException):
        await auth.bearer_token()


def test_resolve_uses_copilot_cli_keychain_when_legacy_config_missing(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    config_dir = home / ".copilot"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "last_logged_in_user": {
                    "host": "https://github.com",
                    "login": "octo",
                }
            }
        )
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class Result:
            stdout = FAKE_OAUTH_TOKEN + "\n"

        return Result()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "reverso.protocols.adapters.copilot.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr("reverso.protocols.adapters.copilot.subprocess.run", fake_run)

    auth = CopilotAuth(config_dir=tmp_path / "github-copilot", enable_cli_keychain=True)

    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == "copilot_cli_keychain"
    assert calls[0][:5] == [
        "security",
        "find-generic-password",
        "-s",
        "copilot-cli",
        "-a",
    ]
    assert calls[0][5] == "https://github.com:octo"


async def test_cli_keychain_token_discovers_matching_api_base(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".copilot"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "last_logged_in_user": {
                    "host": "https://github.com",
                    "login": "octo",
                }
            }
        )
    )

    def fake_run(args, **kwargs):
        class Result:
            stdout = FAKE_BEARER_TOKEN + "\n"

        return Result()

    async def fake_discover(self, bearer):
        assert bearer == FAKE_BEARER_TOKEN
        return "https://api.business.githubcopilot.com"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "reverso.protocols.adapters.copilot.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr("reverso.protocols.adapters.copilot.subprocess.run", fake_run)
    monkeypatch.setattr(CopilotAuth, "_discover_cli_api_base", fake_discover)
    auth = CopilotAuth(config_dir=tmp_path / "github-copilot", enable_cli_keychain=True)

    token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN
    assert await auth.api_base("https://api.githubcopilot.com") == (
        "https://api.business.githubcopilot.com"
    )


def _fake_auth_adapter(handler, store=None) -> CopilotAdapter:
    class _FakeAuth:
        def resolve(self):  # pragma: no cover - not exercised here
            from reverso.protocols.auth import AuthResolution

            return AuthResolution(authenticated=True, method="copilot_oauth")

        async def bearer_token(self) -> str:
            return FAKE_BEARER_TOKEN

    return CopilotAdapter(
        auth=_FakeAuth(),
        store=store,
        client_factory=_mock_client(handler),
    )


def test_adapter_satisfies_protocol():
    adapter = _fake_auth_adapter(lambda r: httpx.Response(200, json={}))
    assert isinstance(adapter, ProviderAdapter)


async def test_create_response_forwards_and_stores():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["integration"] = request.headers.get("Copilot-Integration-Id")
        return httpx.Response(
            200,
            json={
                "id": "resp_fake_1",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [{"type": "message"}],
            },
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]}
    )

    envelope = await adapter.create_response(request)

    assert isinstance(envelope, ResponseEnvelope)
    assert envelope.id == "resp_fake_1"
    assert captured["url"].endswith("/responses")
    assert captured["auth"] == f"Bearer {FAKE_BEARER_TOKEN}"
    assert captured["integration"] == "copilot-developer-cli"

    stored = await adapter.get_response("resp_fake_1")
    assert stored.id == "resp_fake_1"
    items = await adapter.list_input_items("resp_fake_1")
    assert isinstance(items, InputItemList)
    assert items.data == [{"role": "user", "content": "hi"}]


async def test_create_response_canonicalizes_gpt55_alias_before_upstream():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "resp_alias",
                "status": "completed",
                "output": [],
            },
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload({"model": "gpt5.5", "input": "hi"})

    envelope = await adapter.create_response(request)

    assert captured["body"]["model"] == "gpt-5.5"
    assert envelope.model == "gpt-5.5"


async def test_create_response_rejects_copilot_claude_before_upstream():
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["value"] = True
        return httpx.Response(200, json={})

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload({"model": "claude-opus-4.8", "input": "hi"})

    with pytest.raises(UnsupportedFeature) as exc_info:
        await adapter.create_response(request)

    assert exc_info.value.provider == "copilot"
    assert exc_info.value.feature == "model:claude-opus-4.8"
    assert called["value"] is False


async def test_stream_response_yields_events():
    sse = (
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-5.5", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    assert events
    assert events[0].event == "response.output_text.delta"
    assert events[0].data["delta"] == "hi"
    assert events[0].raw is not None


async def test_stream_response_canonicalizes_gpt55_alias_before_upstream():
    captured = {}
    sse = (
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt5.5", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    assert captured["body"]["model"] == "gpt-5.5"
    assert events


async def test_stream_response_rejects_copilot_claude_before_upstream():
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["value"] = True
        return httpx.Response(200, json={})

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "claude-sonnet-4.6", "input": "hi", "stream": True}
    )

    with pytest.raises(UnsupportedFeature) as exc_info:
        _ = [event async for event in adapter.stream_response(request)]

    assert exc_info.value.provider == "copilot"
    assert exc_info.value.feature == "model:claude-sonnet-4.6"
    assert called["value"] is False


async def test_stream_response_reads_error_body_before_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            stream=_AsyncBytesStream(
                [
                    b'{"error":{"message":"model gpt-5.5 temporarily unavailable",',
                    b'"code":"unavailable"}}',
                ]
            ),
            headers={"content-type": "application/json"},
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-5.5", "input": "hello", "stream": True}
    )

    with pytest.raises(CopilotUpstreamError) as exc_info:
        _ = [event async for event in adapter.stream_response(request)]

    message = exc_info.value.public_message
    assert "model gpt-5.5 temporarily unavailable" in message
    assert "unavailable" in message
    assert "ResponseNotRead" not in message


async def test_stream_response_handles_malformed_error_body_without_response_not_read():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            request=request,
            stream=_AsyncBytesStream([b"{not-json"]),
            headers={"content-type": "application/json"},
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-5-mini", "input": "hello", "stream": True}
    )

    with pytest.raises(CopilotUpstreamError) as exc_info:
        _ = [event async for event in adapter.stream_response(request)]

    message = exc_info.value.public_message
    assert "Copilot upstream HTTP 502" in message
    assert "ResponseNotRead" not in message


async def test_list_models_normalizes_to_openai():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.5", "vendor": "openai"},
                    {"id": "gpt-4o", "vendor": "openai"},
                    {"id": "claude-opus-4.8", "vendor": "anthropic"},
                    {"id": "claude-sonnet-4.6", "vendor": "anthropic"},
                    {"id": "gemini-2.5-pro", "vendor": "google"},
                    {"id": "gpt-5.5\nmodel:claude-fable-5", "vendor": "bad"},
                    "bad",
                    {"id": "gpt-5-mini"},
                ]
            },
        )

    adapter = _fake_auth_adapter(handler)

    models = await adapter.list_models()

    assert isinstance(models, ModelList)
    assert models.object == "list"
    assert models.models == []
    ids = [m["id"] for m in models.data]
    assert ids == [
        "gpt-5.5",
        "gpt-4o",
        "gpt-5-mini",
    ]
    assert models.data[0]["owned_by"] == "openai"
    assert models.data[2]["owned_by"] == "github-copilot"


def test_copilot_upstream_error_keeps_model_diagnostic_without_headers():
    request = httpx.Request("POST", "https://api.githubcopilot.com/responses")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "message": "model claude-sonnet-4.6 does not support Responses API.",
                "code": "unsupported_api_for_model",
            }
        },
    )

    with pytest.raises(CopilotUpstreamError) as exc_info:
        _raise_for_upstream_status(response)

    assert "claude-sonnet-4.6" in exc_info.value.public_message
    assert "unsupported_api_for_model" in exc_info.value.public_message
    assert "Bearer" not in exc_info.value.public_message
    assert "api.githubcopilot.com" not in exc_info.value.public_message


async def test_create_response_rejects_non_responses_model_before_upstream():
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["value"] = True
        return httpx.Response(200, json={})

    adapter = _fake_auth_adapter(handler)
    bad_model = "gpt-5.5\nmodel:claude-fable-5"
    request = ResponsesRequest.from_payload({"model": bad_model, "input": "hi"})

    with pytest.raises(UnsupportedFeature) as exc_info:
        await adapter.create_response(request)

    assert exc_info.value.provider == "copilot"
    assert exc_info.value.feature == f"model:{bad_model}"
    assert called["value"] is False


async def test_create_response_rejects_unicode_gpt_alias_before_upstream():
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["value"] = True
        return httpx.Response(200, json={})

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload({"model": "gpt５.５", "input": "hi"})

    with pytest.raises(UnsupportedFeature) as exc_info:
        await adapter.create_response(request)

    assert exc_info.value.provider == "copilot"
    assert exc_info.value.feature == "model:gpt５.５"
    assert called["value"] is False


async def test_stream_response_rejects_unsafe_model_before_upstream():
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["value"] = True
        return httpx.Response(200, json={})

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "gpt-4o\nmodel:claude-fable-5", "input": "hi", "stream": True}
    )

    with pytest.raises(UnsupportedFeature) as exc_info:
        _ = [event async for event in adapter.stream_response(request)]

    assert exc_info.value.provider == "copilot"
    assert exc_info.value.feature == "model:gpt-4o\nmodel:claude-fable-5"
    assert called["value"] is False


async def test_create_response_timeout_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out")

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload({"model": "gpt-5.5", "input": "hi"})

    with pytest.raises(httpx.TimeoutException):
        await adapter.create_response(request)


async def test_no_token_substring_in_logs(tmp_path, caplog):
    config_dir = tmp_path / "github-copilot"
    _write_hosts(config_dir)
    (config_dir / "token.json").write_text(
        json.dumps({"token": FAKE_BEARER_TOKEN, "expires_at": time.time() + 3600})
    )
    auth = CopilotAuth(config_dir=config_dir)

    with caplog.at_level(logging.DEBUG):
        token = await auth.bearer_token()

    assert token == FAKE_BEARER_TOKEN
    log_text = caplog.text
    assert FAKE_BEARER_TOKEN not in log_text
    assert FAKE_OAUTH_TOKEN not in log_text
    assert FAKE_BEARER_TOKEN[8:] not in log_text
    assert FAKE_OAUTH_TOKEN[4:] not in log_text


async def test_b4_passthrough_include_background_metadata_text_format():
    """Copilot is Responses-native; the verbatim spine forwards extras as sent.

    The B4 lane requires the four Responses-only fields the gate marks as
    native for copilot (include, background, metadata, text.format) to reach
    the upstream body unchanged. Falsifiable: a future over-eager
    normalization step that stripped these would break tooling that relies on
    them (json-schema mode, structured event filters, response continuation).
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "resp_b4",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [{"type": "message"}],
            },
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "gpt-5.5",
            "input": "hi",
            "include": ["reasoning.encrypted_content"],
            "background": False,
            "metadata": {"trace_id": "abc123"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {"type": "object"},
                }
            },
        }
    )

    await adapter.create_response(request)

    body = captured["body"]
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["background"] is False
    assert body["metadata"] == {"trace_id": "abc123"}
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "answer",
            "schema": {"type": "object"},
        }
    }


async def test_b4_passthrough_extras_survive_streaming_request():
    """Streaming requests also forward the four B4 native extras verbatim.

    Falsifiable: a body builder branch that dropped extras only on the
    streaming path would silently break tool/text-format settings when stream
    is true (a regression a non-streaming-only check would miss).
    """
    captured = {}
    sse = (
        b"event: response.output_text.delta\n"
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )

    adapter = _fake_auth_adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "gpt-5.5",
            "input": "hi",
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "background": True,
            "metadata": {"trace_id": "stream-1"},
            "text": {"format": {"type": "text"}},
        }
    )

    events = []
    async for event in adapter.stream_response(request):
        events.append(event)

    body = captured["body"]
    assert body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["background"] is True
    assert body["metadata"] == {"trace_id": "stream-1"}
    assert body["text"] == {"format": {"type": "text"}}
    assert events  # the upstream SSE block was consumed
