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
