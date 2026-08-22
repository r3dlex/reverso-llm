"""Unit tests for the OpenRouter Responses streaming + stateless continuation path (OR-G3, U7, I3).

These tests inject a fake transport and assert that the streaming Responses
path emits canonical SSE events, materializes typed continuation chains from
local storage, and refuses unknown / partial chains. CI uses injected
transports and stores; no real OpenRouter call is made.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any
from unittest import mock

import pytest

from reverso.protocols.adapter import ResponsesRequest, SSEEvent
from reverso.protocols.adapters.openrouter.adapter import OpenRouterAdapter
from reverso.protocols.adapters.openrouter.continuation import (
    ContinuationRejection,
    ReplayChain,
    ReplayItem,
    build_continuation_request,
    materialize_continuation,
)


# --- Test doubles --------------------------------------------------------------


# --- U7 streaming canonical events ----------------------------------------------


def test_stream_response_emits_canonical_events() -> None:
    events = [
        'data: {"type":"response.created","response":{"id":"r1","model":"m","status":"in_progress","output":[]}}',
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","status":"in_progress"}}',
        'data: {"type":"response.reasoning_text.delta","output_index":0,"item_id":"x","delta":"hi"}',
        'data: {"type":"response.completed","response":{"id":"r1","model":"m","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":2}}}',
        "data: [DONE]",
    ]
    transport = mock.Mock()
    transport.stream_response = lambda payload: _events_to_async_iter(events)
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    request = ResponsesRequest(model="m", input="ping", stream=True)
    out = _run_collect(adapter.stream_response(request))
    types = [e.event for e in out if isinstance(e, SSEEvent)]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.completed" in types
    assert any(t.startswith("response.reasoning_text.delta") for t in types)


def test_stream_response_drops_sse_comments() -> None:
    events = [
        ": keepalive",
        'data: {"type":"response.completed","response":{"id":"r2","model":"m","status":"completed","output":[]}}',
        "data: [DONE]",
    ]
    transport = mock.Mock()
    transport.stream_response = lambda payload: _events_to_async_iter(events)
    adapter = OpenRouterAdapter(transport=transport, credentials=transport)
    request = ResponsesRequest(model="m", input="ping", stream=True)
    out = _run_collect(adapter.stream_response(request))
    assert all(e.event != "" for e in out)


# --- I3: typed continuation chain --------------------------------------------


def test_continuation_materializes_typed_chain() -> None:
    store = mock.Mock()
    chain = ReplayChain(
        response_id="r1",
        items=[
            ReplayItem(role="user", content="ping"),
            ReplayItem(role="assistant", content="pong", reasoning="pong"),
            ReplayItem(
                role="tool",
                tool_call_id="tc-1",
                tool_name="lookup",
                tool_arguments={"q": "x"},
                tool_output={"answer": 42},
            ),
        ],
    )
    request = build_continuation_request(chain, model="m")
    # previous_response_id must be resolved locally and never forwarded.
    body = request
    assert body["model"] == "m"
    assert "previous_response_id" not in body
    assert body["input"]  # type contains the full typed items
    materialized = materialize_continuation(chain, store=store)
    assert materialized.has_tool_calls


def test_continuation_rejects_unknown_chain() -> None:
    with pytest.raises(ContinuationRejection):
        materialize_continuation(None, store=mock.Mock())  # type: ignore[arg-type]


def test_continuation_rejects_partial_chain() -> None:
    chain = ReplayChain(
        response_id="r1",
        items=[ReplayItem(role="user", content="ping")],  # no assistant reply
    )
    with pytest.raises(ContinuationRejection):
        materialize_continuation(chain, store=mock.Mock())


def test_continuation_rejects_chain_with_unknown_role() -> None:
    chain = ReplayChain(
        response_id="r1",
        items=[
            ReplayItem(role="user", content="ping"),
            ReplayItem(role="unknown", content="?"),
        ],
    )
    with pytest.raises(ContinuationRejection):
        materialize_continuation(chain, store=mock.Mock())


# --- Helpers ------------------------------------------------------------------


def _run_collect(coro) -> list[Any]:
    return asyncio.run(_collect(coro))


async def _collect(coro):
    out: list[Any] = []
    async for event in coro:
        out.append(event)
    return out


async def _events_to_async_iter(events: Iterable[str]):
    for line in events:
        if not line:
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            raw = line[len("data:") :].strip()
            if raw == "[DONE]":
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event_type = payload.get("type", "")
            yield SSEEvent(event=event_type, data=payload)
