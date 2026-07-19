from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.codex_direct import CodexDirectAdapter, CodexDirectError
from reverso.protocols.auth import AuthResolution, FakeAuth


class FakeDirectUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def create_response(
        self, *, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create", token, body))
        return {
            "id": "resp_direct_fake",
            "model": body["model"],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "direct codex proof"}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("stream", token, body))
        yield {"event": "response.output_text.delta", "data": {"delta": "direct "}}
        yield {"event": "response.output_text.delta", "data": {"delta": "stream"}}

    async def list_models(self, *, token: str) -> list[dict[str, Any]]:
        self.calls.append(("models", token, None))
        return [{"id": "gpt-5.5"}, {"name": "gpt-5.4-mini"}]


def _auth(token: str = "synthetic-access-token") -> FakeAuth:
    return FakeAuth(
        AuthResolution(
            authenticated=True,
            method="oauth",
            subscription_type="chatgpt-plus",
            details={"source": "unit-test"},
        ),
        token=token,
    )


def _request(stream: bool = False) -> ResponsesRequest:
    return ResponsesRequest(
        model="gpt-5.5",
        input="hello direct codex",
        stream=stream,
        instructions="answer briefly",
    )


@pytest.mark.asyncio
async def test_direct_codex_create_response_drains_streaming_endpoint() -> None:
    upstream = FakeDirectUpstream()
    adapter = CodexDirectAdapter(auth=_auth(), upstream=upstream)

    envelope = await adapter.create_response(_request())

    assert envelope.id.startswith("resp_")
    assert envelope.model == "gpt-5.5"
    assert envelope.output[0]["content"][0]["text"] == "direct stream"
    assert upstream.calls[0][0] == "stream"
    assert upstream.calls[0][1] == "synthetic-access-token"
    body = upstream.calls[0][2]
    assert body["stream"] is True
    assert body["store"] is False
    assert body["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "hello direct codex"}],
        }
    ]
    loaded = await adapter.get_response(envelope.id)
    assert loaded.id == envelope.id
    items = await adapter.list_input_items(envelope.id)
    assert items.response_id == envelope.id
    assert items.data == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello direct codex"}],
        }
    ]


@pytest.mark.asyncio
async def test_direct_codex_stream_response_buffers_to_completed_envelope() -> None:
    upstream = FakeDirectUpstream()
    adapter = CodexDirectAdapter(auth=_auth(), upstream=upstream)

    events = [event async for event in adapter.stream_response(_request(stream=True))]

    names = [event.event for event in events]
    assert names[:2] == ["response.created", "response.in_progress"]
    assert "response.output_text.delta" in names
    assert names[-1] == "response.completed"
    completed = events[-1].data["response"]
    assert completed["output"][0]["content"][0]["text"] == "direct stream"
    assert upstream.calls[0][0] == "stream"
    assert upstream.calls[0][2]["stream"] is True


@pytest.mark.asyncio
async def test_direct_codex_list_models_maps_openai_rows() -> None:
    upstream = FakeDirectUpstream()
    adapter = CodexDirectAdapter(auth=_auth(), upstream=upstream)

    models = await adapter.list_models()

    assert [row["id"] for row in models.data] == ["gpt-5.5", "gpt-5.4-mini"]
    assert all(row["object"] == "model" for row in models.data)


@pytest.mark.asyncio
async def test_direct_codex_fails_closed_without_auth_token() -> None:
    upstream = FakeDirectUpstream()
    adapter = CodexDirectAdapter(
        auth=FakeAuth(
            AuthResolution(
                authenticated=False, method="oauth", details={"reason": "missing"}
            ),
            token="should-not-be-used",
        ),
        upstream=upstream,
    )

    with pytest.raises(CodexDirectError, match="auth failed"):
        await adapter.create_response(_request())

    assert upstream.calls == []


class LifecycleDirectUpstream(FakeDirectUpstream):
    async def stream_response(
        self, *, token: str, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("stream", token, body))
        yield {
            "event": "response.created",
            "data": {"response": {"id": "resp_upstream", "model": body["model"]}},
        }
        yield {
            "event": "response.in_progress",
            "data": {"response": {"id": "resp_upstream"}},
        }
        yield {"event": "response.output_text.delta", "data": {"delta": "upstream"}}
        yield {
            "event": "response.completed",
            "data": {
                "response": {
                    "id": "resp_upstream",
                    "model": body["model"],
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "upstream"}],
                        }
                    ],
                }
            },
        }


def test_sse_parser_handles_standard_event_data_framing() -> None:
    from reverso.protocols.adapters.codex_direct import _SSEParser

    parser = _SSEParser()
    chunks = []
    chunks.extend(parser.feed("event: response.output_text.delta"))
    chunks.extend(parser.feed('data: {"delta":"hi"}'))
    chunks.extend(parser.feed(""))

    assert chunks == [{"event": "response.output_text.delta", "data": {"delta": "hi"}}]


@pytest.mark.asyncio
async def test_direct_codex_stream_preserves_upstream_lifecycle_ids() -> None:
    upstream = LifecycleDirectUpstream()
    adapter = CodexDirectAdapter(auth=_auth(), upstream=upstream)

    events = [event async for event in adapter.stream_response(_request(stream=True))]

    assert [event.event for event in events].count("response.created") == 1
    ids = []
    for event in events:
        response = event.data.get("response")
        if isinstance(response, dict) and response.get("id"):
            ids.append(response["id"])
    assert ids and set(ids) == {"resp_upstream"}
    assert events[-1].event == "response.completed"
    assert (await adapter.get_response("resp_upstream")).id == "resp_upstream"


def test_direct_codex_requires_injected_upstream() -> None:
    with pytest.raises(CodexDirectError, match="upstream must be injected"):
        CodexDirectAdapter(auth=_auth())


def test_direct_codex_route_is_reserved_but_disabled_by_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.protocols.responses_app import (
        APP_PROVIDER_PREFIXES,
        split_provider_path,
    )
    from reverso.proxy import compose

    monkeypatch.setenv(compose.CODEX_DIRECT_BACKEND_ENV, "0")

    assert "codex_direct" not in APP_PROVIDER_PREFIXES
    assert "codex-direct" in APP_PROVIDER_PREFIXES
    assert split_provider_path("/codex-direct/v1/models") is not None
    mounted = compose.build_adapters()
    assert set(mounted) == {"claude", "copilot", "auggie", "deepseek", "kimi"}
    assert "codex_direct" not in mounted
    assert "codex-direct" not in mounted


def test_direct_codex_mounts_by_default_unless_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy import compose

    monkeypatch.delenv(compose.CODEX_DIRECT_BACKEND_ENV, raising=False)

    mounted = compose.build_adapters()

    assert isinstance(mounted["codex-direct"], CodexDirectAdapter)

    for disabled in ("0", "false", "no", "off"):
        monkeypatch.setenv(compose.CODEX_DIRECT_BACKEND_ENV, disabled)
        assert "codex-direct" not in compose.build_adapters()


def test_codex_direct_explicit_enable_still_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy import compose

    monkeypatch.setenv(compose.CODEX_DIRECT_BACKEND_ENV, "1")

    mounted = compose.build_adapters()

    assert isinstance(mounted["codex-direct"], CodexDirectAdapter)


def test_codex_direct_non_loopback_host_never_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy import compose

    monkeypatch.setenv(compose.REVERSO_HOST_ENV, "0.0.0.0")
    monkeypatch.delenv(compose.CODEX_DIRECT_BACKEND_ENV, raising=False)

    assert "codex-direct" not in compose.build_adapters()

    monkeypatch.setenv(compose.CODEX_DIRECT_BACKEND_ENV, "1")
    assert "codex-direct" not in compose.build_adapters()


@pytest.mark.asyncio
async def test_direct_codex_reserved_route_fails_closed_before_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy import compose

    monkeypatch.setenv(compose.CODEX_DIRECT_BACKEND_ENV, "0")
    legacy_calls: list[str] = []

    async def legacy_app(scope, receive, send):  # noqa: ANN001
        legacy_calls.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 599, "headers": []})
        await send({"type": "http.response.body", "body": b"legacy"})

    app = compose.CompositionRoot(
        gateway=compose.build_app(compose.build_adapters()), legacy_app=legacy_app
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/codex-direct/v1/models",
            "headers": [],
        },
        receive,
        send,
    )

    assert legacy_calls == []
    assert sent[0]["status"] == 503
    assert b"codex-direct" in sent[1]["body"]
