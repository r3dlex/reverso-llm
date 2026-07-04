from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.openai import (
    OpenAIPassThroughAdapter,
    OpenAIPassThroughAuth,
    OpenAIPassThroughError,
    OpenAIPassThroughHTTPError,
)
from reverso.protocols.auth import AuthResolution, FakeAuth
from reverso.protocols.responses_app import ResponsesGatewayApp, split_provider_path
from reverso.proxy import compose


class SyncAuth:
    def __init__(self, resolution: AuthResolution, token: str = "sync-token") -> None:
        self._resolution = resolution
        self._token = token

    def resolve(self) -> AuthResolution:
        return self._resolution

    def bearer_token(self) -> str:
        return self._token


class FakeOpenAIUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(
            ("create", token, body["model"], body["input"], body["stream"])
        )
        return {
            "id": "resp_openai_unit",
            "model": body["model"],
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("stream", token, body))
        yield {
            "event": "response.output_text.delta",
            "data": {"type": "response.output_text.delta", "delta": "ok"},
        }
        yield {
            "event": "response.completed",
            "data": {
                "type": "response.completed",
                "response": {
                    "id": "resp_openai_stream",
                    "model": body["model"],
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            },
        }

    async def list_models(self, *, token: str) -> dict[str, Any]:
        self.calls.append(("models", token))
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-5.5",
                    "object": "model",
                    "created": 1,
                    "owned_by": "openai",
                },
                {
                    "id": "text-embedding-3-large",
                    "object": "model",
                    "created": 1,
                    "owned_by": "openai",
                },
                {
                    "id": "whisper-1",
                    "object": "model",
                    "created": 1,
                    "owned_by": "openai",
                },
                {
                    "id": "gpt-5.4-mini",
                    "object": "model",
                    "created": 1,
                    "owned_by": "openai",
                },
            ],
        }


class ErrorOpenAIUpstream:
    def __init__(self, status_code: int = 429) -> None:
        self.error = OpenAIPassThroughHTTPError(
            status_code,
            {
                "error": {
                    "message": "quota exceeded",
                    "type": "rate_limit_error",
                    "authorization": "Bearer secret-token",
                }
            },
        )

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        raise self.error

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        raise self.error
        yield {}  # pragma: no cover - keeps this an async generator

    async def list_models(self, *, token: str) -> dict[str, Any]:
        raise self.error


def _request(*, stream: bool = False) -> ResponsesRequest:
    return ResponsesRequest(
        model="gpt-5.5",
        input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=stream,
    )


def _auth(token: str = "unit-openai-token") -> FakeAuth:
    return FakeAuth(
        AuthResolution(
            authenticated=True, method="api-key", details={"source": "unit"}
        ),
        token=token,
    )


async def _call_app(
    app: ResponsesGatewayApp,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
        },
        receive,
        send,
    )
    return sent


def _json_body(sent: list[dict[str, Any]]) -> dict[str, Any]:
    return json.loads(sent[1]["body"].decode("utf-8"))


def test_auth_prefers_oauth_over_api_key() -> None:
    oauth = SyncAuth(
        AuthResolution(
            authenticated=True,
            method="oauth",
            subscription_type="chatgpt-plus",
            details={"source": "codex"},
        ),
        token="oauth-token",
    )
    auth = OpenAIPassThroughAuth(
        oauth_auth=oauth,
        env={"OPENAI_API_KEY": "sk-should-not-win"},
    )

    resolution = auth.resolve()

    assert resolution.authenticated
    assert resolution.method == "oauth"
    assert resolution.details["source"] == "oauth"
    assert auth.bearer_token() == "oauth-token"


def test_auth_falls_back_to_explicit_api_key() -> None:
    oauth = SyncAuth(
        AuthResolution(
            authenticated=False, method="none", details={"reason": "missing"}
        )
    )
    auth = OpenAIPassThroughAuth(oauth_auth=oauth, env={"OPENAI_API_KEY": "sk-unit"})

    resolution = auth.resolve()

    assert resolution.authenticated
    assert resolution.method == "api-key"
    assert resolution.details == {"source": "OPENAI_API_KEY"}
    assert auth.bearer_token() == "sk-unit"


def test_auth_reports_missing_auth_without_secret() -> None:
    auth = OpenAIPassThroughAuth(oauth_auth=None, env={})

    resolution = auth.resolve()

    assert not resolution.authenticated
    assert resolution.method == "none"
    assert resolution.details["reason"] == "missing_openai_auth"


@pytest.mark.asyncio
async def test_adapter_forwards_responses_and_filters_models() -> None:
    upstream = FakeOpenAIUpstream()
    adapter = OpenAIPassThroughAdapter(auth=_auth(), upstream=upstream)

    envelope = await adapter.create_response(_request())
    models = await adapter.list_models()

    assert envelope.id == "resp_openai_unit"
    assert envelope.output[0]["content"][0]["text"] == "ok"
    assert (await adapter.get_response(envelope.id)).id == envelope.id
    assert [row["id"] for row in models.data] == ["gpt-5.5", "gpt-5.4-mini"]
    assert upstream.calls[0] == (
        "create",
        "unit-openai-token",
        "gpt-5.5",
        _request().input,
        False,
    )
    assert upstream.calls[1][0] == "models"


@pytest.mark.asyncio
async def test_adapter_streams_responses_and_records_completed_response() -> None:
    upstream = FakeOpenAIUpstream()
    adapter = OpenAIPassThroughAdapter(auth=_auth(), upstream=upstream)

    events = [event async for event in adapter.stream_response(_request(stream=True))]

    assert [event.event for event in events] == [
        "response.output_text.delta",
        "response.completed",
    ]
    assert upstream.calls[0][0] == "stream"
    assert upstream.calls[0][2]["stream"] is True
    assert (await adapter.get_response("resp_openai_stream")).id == "resp_openai_stream"


@pytest.mark.asyncio
async def test_adapter_rejects_missing_auth() -> None:
    adapter = OpenAIPassThroughAdapter(
        auth=OpenAIPassThroughAuth(oauth_auth=None, env={}),
        upstream=FakeOpenAIUpstream(),
    )

    with pytest.raises(OpenAIPassThroughError, match="auth unavailable"):
        await adapter.create_response(_request())


def test_openai_adapter_is_local_loopback_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(compose.OPENAI_BACKEND_ENV, raising=False)
    monkeypatch.delenv(compose.REVERSO_HOST_ENV, raising=False)
    assert "openai" not in compose.build_adapters()

    monkeypatch.setenv(compose.OPENAI_BACKEND_ENV, "1")
    adapters = compose.build_adapters()
    assert isinstance(adapters["openai"], OpenAIPassThroughAdapter)
    assert adapters["openai-pass-through"] is adapters["openai"]

    monkeypatch.setenv(compose.REVERSO_HOST_ENV, "0.0.0.0")
    adapters = compose.build_adapters()
    assert "openai" not in adapters
    assert "openai-pass-through" not in adapters
    assert "codex-direct" not in adapters


@pytest.mark.asyncio
async def test_app_routes_operator_and_codex_sync_openai_aliases() -> None:
    adapter = OpenAIPassThroughAdapter(auth=_auth(), upstream=FakeOpenAIUpstream())
    app = ResponsesGatewayApp({"openai": adapter, "openai-pass-through": adapter})

    operator = await _call_app(app, "GET", "/openai/v1/models")
    codex_sync = await _call_app(app, "GET", "/openai-pass-through/v1/models")

    assert split_provider_path("/openai/v1/models").provider == "openai"  # type: ignore[union-attr]
    assert (
        split_provider_path("/openai-pass-through/v1/models").provider
        == "openai-pass-through"
    )  # type: ignore[union-attr]
    assert operator[0]["status"] == 200
    assert codex_sync[0]["status"] == 200
    assert b"gpt-5.5" in operator[1]["body"]
    assert b"text-embedding" not in operator[1]["body"]
    assert b"gpt-5.4-mini" in codex_sync[1]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_app_preserves_upstream_response_status_before_body_starts(
    status_code: int,
) -> None:
    adapter = OpenAIPassThroughAdapter(
        auth=_auth(), upstream=ErrorOpenAIUpstream(status_code)
    )
    app = ResponsesGatewayApp({"openai": adapter})

    sent = await _call_app(
        app, "POST", "/openai/v1/responses", {"model": "gpt-5.5", "input": "hi"}
    )

    assert sent[0]["status"] == status_code
    body = _json_body(sent)
    assert body["error"]["message"] == "quota exceeded"
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["authorization"] == "[redacted]"


@pytest.mark.asyncio
async def test_app_preserves_models_upstream_response_status() -> None:
    adapter = OpenAIPassThroughAdapter(auth=_auth(), upstream=ErrorOpenAIUpstream(500))
    app = ResponsesGatewayApp({"openai": adapter})

    sent = await _call_app(app, "GET", "/openai/v1/models")

    assert sent[0]["status"] == 500
    assert _json_body(sent)["error"]["type"] == "rate_limit_error"


@pytest.mark.asyncio
async def test_app_preserves_stream_initial_upstream_response_status() -> None:
    adapter = OpenAIPassThroughAdapter(auth=_auth(), upstream=ErrorOpenAIUpstream(429))
    app = ResponsesGatewayApp({"openai": adapter})

    sent = await _call_app(
        app,
        "POST",
        "/openai/v1/responses",
        {"model": "gpt-5.5", "input": "hi", "stream": True},
    )

    assert sent[0]["status"] == 429
    assert _json_body(sent)["error"]["message"] == "quota exceeded"
