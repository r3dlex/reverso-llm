"""App-level error handling for the first-party Responses gateway.

A provider/adapter failure must surface as a structured Responses error, never
as an uncaught 500 or a silently truncated 200 stream (PR #4 review, MAJOR).
These tests wire failing adapters through build_app over httpx ASGITransport and
assert the gateway translates the failure and never leaks the exception payload.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.responses_app import build_app

BASE_URL = "http://127.0.0.1:64946"
SECRET = "sk-secret-should-not-leak"
_BOOM = f"token={SECRET} https://upstream.internal/v1"


class _BoomError(RuntimeError):
    """A provider-side failure whose message carries detail that must not leak."""


class FailingAdapter:
    """A ProviderAdapter whose calls all raise before producing any output."""

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        raise _BoomError(_BOOM)

    async def list_models(self) -> ModelList:
        raise _BoomError(_BOOM)

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        raise _BoomError(_BOOM)

    async def list_input_items(self, response_id: str) -> InputItemList:
        raise _BoomError(_BOOM)

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        return self._stream(request)

    async def _stream(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        raise _BoomError(_BOOM)
        yield  # pragma: no cover - unreachable; marks this as an async generator


class MidStreamFailAdapter(FailingAdapter):
    """Yields one valid event and then fails partway through the stream."""

    async def _stream(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(
            event="response.created",
            data={"type": "response.created", "response": {"status": "in_progress"}},
        )
        raise _BoomError(_BOOM)


def _client(adapter: Any) -> httpx.AsyncClient:
    app = build_app({"claude": adapter})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


@pytest.mark.asyncio
async def test_nonstreaming_failure_returns_structured_502():
    async with _client(FailingAdapter()) as client:
        resp = await client.post(
            "/claude/v1/responses", json={"model": "m", "input": "hi"}
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "server_error"
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_list_models_failure_returns_structured_502():
    async with _client(FailingAdapter()) as client:
        resp = await client.get("/claude/v1/models")
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "server_error"
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_get_response_failure_returns_structured_502():
    async with _client(FailingAdapter()) as client:
        resp = await client.get("/claude/v1/responses/resp_123")
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "server_error"
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_stream_failure_before_first_event_returns_502():
    async with _client(FailingAdapter()) as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={"model": "m", "input": "hi", "stream": True},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "server_error"
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_midstream_failure_emits_terminal_event_and_done():
    async with _client(MidStreamFailAdapter()) as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={"model": "m", "input": "hi", "stream": True},
        )
    # Header is already 200 once the first event is sent; the stream must still
    # close cleanly with a terminal failure event and the [DONE] sentinel.
    assert resp.status_code == 200
    text = resp.text
    assert "response.created" in text
    assert "response.failed" in text
    assert "[DONE]" in text
    assert SECRET not in text


# --- D1: pre-emission vs post-emission split on the streaming transport ---

import json as _json  # noqa: E402

from reverso.protocols.adapters.deepseek import DeepSeekAdapter  # noqa: E402


def _deepseek_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=30.0)

    adapter = DeepSeekAdapter(client_factory=factory)
    app = build_app({"deepseek": adapter})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


@pytest.mark.asyncio
async def test_deepseek_streaming_401_pre_emission_renders_structured_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 received at response.headers becomes a structured 502 server_error.

    Pre-emission branch (Architect REVISE point 4): the streaming transport
    raises DeepSeekError BEFORE any SSE byte ships, so responses_app._stream
    synthesises the structured error body. No SSE bytes may appear in the
    response.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-unused")

    class _Ack(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":"unauthorized"}'

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=_Ack())

    async with _deepseek_client(handler) as client:
        resp = await client.post(
            "/deepseek/v1/responses",
            json={"model": "deepseek-chat", "input": "hi", "stream": True},
        )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["type"] == "server_error"
    assert "DeepSeekError" in body["error"]["message"]
    assert "event: response.output_text" not in resp.text
    assert "data: [DONE]" not in resp.text


@pytest.mark.asyncio
async def test_deepseek_streaming_401_post_emission_renders_response_failed_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure DURING body iteration emits response.failed + [DONE].

    Post-emission branch (Architect REVISE point 4): once a delta has been
    emitted the gateway is committed to a 200 SSE response, so the upstream
    failure surfaces as response.failed + [DONE] rather than a structured
    502. Pinned end-to-end through the real DeepSeekAdapter and responses_app.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-unused")

    class _BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"id":"x","model":"deepseek-chat","choices":'
                b'[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
            )
            raise httpx.RemoteProtocolError("connection broke mid-stream")

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BrokenStream())

    async with _deepseek_client(handler) as client:
        resp = await client.post(
            "/deepseek/v1/responses",
            json={"model": "deepseek-chat", "input": "hi", "stream": True},
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    text = resp.text
    assert "response.output_text.delta" in text
    assert "response.failed" in text
    assert "[DONE]" in text
    # Sanity-check: at least one delta with the partial content arrived before
    # the failure event so the test pins the post-emission branch, not the
    # pre-emission one.
    assert '"Hel"' in text
    # _json is only used to make sure the module is referenced; the assertion
    # above already covers the wire format.
    _ = _json


class WorkspaceCaptureAdapter(FailingAdapter):
    """Capture the current workspace context seen by the adapter."""

    def __init__(self) -> None:
        self.create_workspace: str | None = None
        self.stream_workspace: str | None = None

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

        self.create_workspace = CURRENT_PROFILE_WORKSPACE.get()
        return ResponseEnvelope(id="resp_workspace", model=request.model, output=[])

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        return self._stream(request)

    async def _stream(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

        self.stream_workspace = CURRENT_PROFILE_WORKSPACE.get()
        yield SSEEvent(
            event="response.created",
            data={"type": "response.created", "response": {"status": "in_progress"}},
        )


_CODEX_WORKSPACE_METADATA = {
    "workspaces": {
        "/workspaces/example-repo": {
            "associated_remote_urls": ["git@example.com:org/example-repo.git"],
            "latest_git_commit_hash": "abc123",
            "has_changes": True,
        }
    }
}


@pytest.mark.asyncio
async def test_first_party_response_sets_workspace_context_from_client_metadata():
    adapter = WorkspaceCaptureAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/claude/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": "hi",
                "client_metadata": {
                    "x-codex-turn-metadata": _json.dumps(_CODEX_WORKSPACE_METADATA)
                },
            },
        )
    assert resp.status_code == 200
    assert adapter.create_workspace == "/workspaces/example-repo"


@pytest.mark.asyncio
async def test_first_party_stream_sets_workspace_context_from_header_metadata():
    adapter = WorkspaceCaptureAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/claude/v1/responses",
            headers={"x-codex-turn-metadata": _json.dumps(_CODEX_WORKSPACE_METADATA)},
            json={"model": "gpt-5.5", "input": "hi", "stream": True},
        )
    assert resp.status_code == 200
    assert adapter.stream_workspace == "/workspaces/example-repo"
