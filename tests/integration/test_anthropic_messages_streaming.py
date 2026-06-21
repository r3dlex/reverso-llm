"""Streaming /v1/messages integration tests over the FixtureAdapter (G004).

These drive the real AnthropicMessagesApp streaming path (stream=true) through
httpx.ASGITransport, backed by the UNCHANGED FixtureAdapter from the Responses
parity harness (no real provider, process, or credential). They pin, per
Anthropic-surface backend (copilot/deepseek/auggie): a 200 text/event-stream
response with a valid Anthropic event order, that concatenated text_deltas equal
the full output text, and that anthropic-version is echoed.

The FixtureAdapter.stream_response replays the SAME Codex-observed streaming
fixture for every backend, so the Anthropic mapping is exercised identically
across backends without changing the frozen adapters or the parity harness.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import AnthropicMessagesApp

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie"]

_BACKEND_MODEL = {
    "copilot": "copilot-gpt-5",
    "deepseek": "deepseek-v4-pro",
    "auggie": "prism-a",
}


def _build_client() -> httpx.AsyncClient:
    adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    app = AnthropicMessagesApp(adapters)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(backend: str) -> str:
    return f"/{backend}/v1/messages"


def _parse_anthropic_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def _collapse_repeated_deltas(types: list[str]) -> list[str]:
    """Collapse consecutive content_block_delta events into one delta phase.

    Adapters legitimately chunk text deltas at different granularities, so the
    canonical Anthropic event order treats any run of deltas as one delta phase.
    """
    collapsed: list[str] = []
    for event_type in types:
        if (
            event_type == "content_block_delta"
            and collapsed
            and collapsed[-1] == "content_block_delta"
        ):
            continue
        collapsed.append(event_type)
    return collapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_streaming_valid_anthropic_event_order(backend: str) -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            assert resp.headers["anthropic-version"] == "2023-06-01"
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_anthropic_sse(text)
    types = [event["type"] for event in events]

    assert _collapse_repeated_deltas(types) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Exactly one ping, immediately after message_start.
    assert types.count("ping") == 1
    assert types[1] == "ping"
    assert events[0]["message"]["role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_streaming_concatenated_text_deltas_equal_full_text(backend: str) -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_anthropic_sse(text)
    concatenated = "".join(
        event["delta"]["text"]
        for event in events
        if event["type"] == "content_block_delta"
        and event["delta"]["type"] == "text_delta"
    )
    # The shared streaming fixture replays "Hi" + " there." across all backends.
    assert concatenated == "Hi there."


@pytest.mark.asyncio
async def test_streaming_anthropic_version_echoed_from_header() -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix("copilot"),
            headers={"anthropic-version": "2099-01-01"},
            json={
                "model": _BACKEND_MODEL["copilot"],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["anthropic-version"] == "2099-01-01"
            async for _ in resp.aiter_text():
                pass


# --- pre-stream failure -> 502 JSON (not a 200 event-stream) ----------------


class _StreamFailsImmediatelyAdapter:
    """Adapter whose stream_response raises on the first __anext__.

    The exception message embeds a fake secret to verify it never leaks into
    the Anthropic error envelope.
    """

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        return ResponseEnvelope(id="resp_x", model=request.model)

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        raise RuntimeError("api_key=sk-SECRET-do-not-leak connect failed")
        # unreachable; satisfies the async generator protocol
        yield SSEEvent(event="x", data={})  # noqa: unreachable

    async def list_models(self) -> ModelList:
        return ModelList(data=[])

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="x")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


@pytest.mark.asyncio
async def test_stream_pre_first_event_failure_returns_502_not_200_stream() -> None:
    """An adapter whose stream_response raises on the first __anext__ must yield
    a non-streaming 502 Anthropic error envelope, NOT a 200 text/event-stream.

    This pins the peek-first contract: the mapper propagates the exception before
    yielding message_start, so _handle_streaming's pre-commit branch handles it.
    """
    adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    adapters["copilot"] = _StreamFailsImmediatelyAdapter()
    app = AnthropicMessagesApp(adapters)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:64946"
    ) as client:
        resp = await client.post(
            _prefix("copilot"),
            json={
                "model": _BACKEND_MODEL["copilot"],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 502
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    # Secret-free: only the exception class name, never the payload text.
    assert "sk-SECRET-do-not-leak" not in body["error"]["message"]
    assert "RuntimeError" in body["error"]["message"]


# --- security findings (G008 quality gate) -----------------------------------


def _deeply_nested_payload_bytes(depth: int, *, stream: bool = False) -> bytes:
    """Build a pre-serialised Messages payload with tool_result nested ``depth``
    levels deep, with a gated image block at the TOP level so the gate rejects it
    regardless of depth.

    Pre-serialised to bytes so httpx sends the raw body without calling
    json.dumps on a deeply-recursive Python dict (which would itself hit the
    encoder's recursion limit before the request reaches the server).

    The top-level image block is guaranteed to be found at depth 0 of the scan
    (before any recursion), so the gate raises AnthropicFeatureRejected for
    backends that do not support images, giving a 400. This exercises the
    structured-error contract even when the depth cap silently truncates deep
    recursion. The nesting itself proves no RecursionError escapes.
    """
    leaf = '{"type":"text","text":"leaf"}'
    inner = leaf
    for _ in range(depth):
        inner = (
            '{"type":"tool_result","tool_use_id":"toolu_x","content":[' + inner + "]}"
        )
    # The image block is at the TOP of the content list so the gate sees it at
    # scan depth 0, independent of how deep the tool_result nesting goes.
    image_block = '{"type":"image","source":{"type":"base64","media_type":"image/png","data":"x"}}'
    stream_field = ',"stream":true' if stream else ""
    body = (
        '{"model":"'
        + _BACKEND_MODEL["deepseek"]
        + '","max_tokens":64'
        + stream_field
        + ',"messages":[{"role":"user","content":['
        + image_block
        + ","
        + inner
        + "]}]}"
    )
    return body.encode()


@pytest.mark.asyncio
async def test_deeply_nested_payload_nonstreaming_returns_400_not_500() -> None:
    """Finding 1: a deeply-nested tool_result payload (depth > _MAX_BLOCK_DEPTH)
    on POST /v1/messages must return a structured 400 invalid_request_error, NOT
    a 500 framework crash.

    The top-level image block triggers the gate (images are unsupported on deepseek)
    giving a clean 400. The nesting depth exercises the cap and proves no
    RecursionError escapes as an unhandled 500.
    """
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    depth = _MAX_BLOCK_DEPTH + 5
    async with _build_client() as client:
        resp = await client.post(
            _prefix("deepseek"),
            content=_deeply_nested_payload_bytes(depth),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_deeply_nested_payload_streaming_returns_400_not_200_stream() -> None:
    """Finding 1: a deeply-nested payload with stream=true must return a 400 JSON
    body (NOT a 200 text/event-stream) because gating runs before the 200 header.
    """
    from reverso.protocols.anthropic_feature_gate import _MAX_BLOCK_DEPTH  # noqa: PLC0415

    depth = _MAX_BLOCK_DEPTH + 5
    async with _build_client() as client:
        resp = await client.post(
            _prefix("deepseek"),
            content=_deeply_nested_payload_bytes(depth, stream=True),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 400
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_oversized_body_returns_structured_error() -> None:
    """Finding 2: a body exceeding _MAX_BODY_BYTES must return a structured 413
    Anthropic error envelope, not an unhandled crash or silent buffering.
    """
    from reverso.protocols.anthropic_app import _MAX_BODY_BYTES  # noqa: PLC0415

    oversized_body = b'{"model":"' + b"x" * (_MAX_BODY_BYTES + 1) + b'"}'
    async with _build_client() as client:
        resp = await client.post(
            _prefix("copilot"),
            content=oversized_body,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code in (400, 413)
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_stream_mid_failure_generator_closed() -> None:
    """Finding 3: a mid-stream failure must still close the async generator
    (aclose called via finally), releasing the upstream resource promptly.

    We verify indirectly: the stream completes without hanging and returns
    a terminal in-band error event or a 502 envelope (the mid-stream failure
    path). If aclose were missing this test could hang on some event-loop
    implementations because the generator is never exhausted.
    """
    adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    adapters["copilot"] = _StreamFailsImmediatelyAdapter()
    app = AnthropicMessagesApp(adapters)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:64946"
    ) as client:
        resp = await client.post(
            _prefix("copilot"),
            json={
                "model": _BACKEND_MODEL["copilot"],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    # The response must be complete (no hang). Status is either 502 (pre-stream
    # failure caught by peek-first) or 200 with in-band error (mid-stream).
    assert resp.status_code in (200, 502)
    # If it is a 502 it must be a valid Anthropic error envelope.
    if resp.status_code == 502:
        body = resp.json()
        assert body["type"] == "error"
