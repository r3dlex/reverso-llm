"""Unit tests for the PURE Responses-SSE -> Anthropic-SSE stream mapper (G004).

These exercise responses_sse_to_anthropic directly over hand-built SSEEvent
iterators (no FixtureAdapter, no ASGI, no network), so they pin the Anthropic
streaming grammar at the event level:

  - text turn: exact event order including EXACTLY ONE ping;
  - tool-call turn: input_json_delta + stop_reason tool_use;
  - multi-block text(idx0) + tool_use(idx1): exact order with PAIRED stops;
  - Copilot verbatim superset: refusal.delta -> text_delta,
    reasoning_summary_text.delta + unknown event both DROPPED, NEVER raise;
  - mid-stream failure (response.failed or upstream exception after first event)
    -> terminal in-band error event;
  - pre-stream failure (exception on the FIRST __anext__) -> propagates out of
    the mapper, NO events yielded before the exception;
  - truncated upstream (no response.completed) -> still ends with message_delta +
    message_stop;
  - empty upstream -> minimal terminal sequence;
  - orphan delta before any output_item.added -> safely dropped, well-formed;
  - MINOR-1: missing call_id/name in function_call item -> coerced to "".
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from reverso.protocols.adapter import SSEEvent
from reverso.protocols.anthropic_stream import responses_sse_to_anthropic

MODEL = "copilot-gpt-5"
MESSAGE_ID = "msg_test"


def _evt(event_type: str, **body: Any) -> SSEEvent:
    """Build a Responses SSEEvent whose data carries its own type (replay shape)."""
    data = {"type": event_type}
    data.update(body)
    return SSEEvent(event=event_type, data=data)


async def _aiter(events: list[SSEEvent]) -> AsyncIterator[SSEEvent]:
    for event in events:
        yield event


async def _collect(events: list[SSEEvent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for event in responses_sse_to_anthropic(
        _aiter(events), model=MODEL, message_id=MESSAGE_ID
    ):
        out.append(event)
    return out


def _types(out: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in out]


# --- canonical Responses helpers (mirror replay.py per-item event shapes) ----


def _text_item_events(text: str, *, output_index: int = 0) -> list[SSEEvent]:
    """The canonical message-item SSE events as replay.py emits them."""
    message_id = "msg_item"
    return [
        _evt(
            "response.output_item.added",
            output_index=output_index,
            item={
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        ),
        _evt(
            "response.content_part.added",
            item_id=message_id,
            output_index=output_index,
            content_index=0,
            part={"type": "output_text", "text": "", "annotations": []},
        ),
        _evt(
            "response.output_text.delta",
            item_id=message_id,
            output_index=output_index,
            content_index=0,
            delta=text,
        ),
        _evt(
            "response.output_text.done",
            item_id=message_id,
            output_index=output_index,
            content_index=0,
            text=text,
        ),
        _evt(
            "response.content_part.done",
            item_id=message_id,
            output_index=output_index,
            content_index=0,
            part={"type": "output_text", "text": text, "annotations": []},
        ),
        _evt(
            "response.output_item.done",
            output_index=output_index,
            item={
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ),
    ]


def _tool_item_events(
    *,
    call_id: str,
    name: str,
    arguments: str,
    output_index: int,
) -> list[SSEEvent]:
    """The canonical function_call-item SSE events as replay.py emits them."""
    item_id = "fc_item"
    return [
        _evt(
            "response.output_item.added",
            output_index=output_index,
            item={
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            },
        ),
        _evt(
            "response.function_call_arguments.delta",
            item_id=item_id,
            output_index=output_index,
            delta=arguments,
        ),
        _evt(
            "response.function_call_arguments.done",
            item_id=item_id,
            output_index=output_index,
            arguments=arguments,
        ),
        _evt(
            "response.output_item.done",
            output_index=output_index,
            item={
                "id": item_id,
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            },
        ),
    ]


def _prelude() -> list[SSEEvent]:
    base = {
        "id": "resp_x",
        "object": "response",
        "status": "in_progress",
        "model": MODEL,
    }
    return [
        _evt("response.created", response=dict(base)),
        _evt("response.in_progress", response=dict(base)),
    ]


def _completed(output: list[dict[str, Any]], *, usage: dict[str, Any]) -> SSEEvent:
    return _evt(
        "response.completed",
        response={
            "id": "resp_x",
            "object": "response",
            "status": "completed",
            "model": MODEL,
            "output": output,
            "usage": usage,
        },
    )


# --- text turn --------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_turn_exact_order_with_exactly_one_ping() -> None:
    message_output = [
        {
            "id": "msg_item",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "Hi there.", "annotations": []}
            ],
        }
    ]
    events = [
        *_prelude(),
        *_text_item_events("Hi there."),
        _completed(message_output, usage={"input_tokens": 3, "output_tokens": 2}),
    ]
    out = await _collect(events)

    assert _types(out) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Exactly one ping, immediately after message_start.
    assert _types(out).count("ping") == 1
    assert _types(out)[1] == "ping"

    start = out[0]["message"]
    assert start["id"] == MESSAGE_ID
    assert start["role"] == "assistant"
    assert start["model"] == MODEL
    assert start["content"] == []

    block_start = out[2]
    assert block_start["index"] == 0
    assert block_start["content_block"] == {"type": "text", "text": ""}

    delta = out[3]
    assert delta["index"] == 0
    assert delta["delta"] == {"type": "text_delta", "text": "Hi there."}

    assert out[4] == {"type": "content_block_stop", "index": 0}

    message_delta = out[5]
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["delta"]["stop_sequence"] is None
    # message_delta usage carries output_tokens ONLY.
    assert message_delta["usage"] == {"output_tokens": 2}


# --- tool-call turn ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_turn_input_json_delta_and_stop_reason_tool_use() -> None:
    tool_output = [
        {
            "id": "fc_item",
            "type": "function_call",
            "status": "completed",
            "call_id": "toolu_1",
            "name": "get_weather",
            "arguments": '{"city":"Berlin"}',
        }
    ]
    events = [
        *_prelude(),
        *_tool_item_events(
            call_id="toolu_1",
            name="get_weather",
            arguments='{"city":"Berlin"}',
            output_index=0,
        ),
        _completed(tool_output, usage={"input_tokens": 5, "output_tokens": 4}),
    ]
    out = await _collect(events)

    assert _types(out) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    block_start = out[2]
    assert block_start["index"] == 0
    assert block_start["content_block"] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_weather",
        "input": {},
    }
    delta = out[3]
    assert delta["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"city":"Berlin"}',
    }
    assert out[5]["delta"]["stop_reason"] == "tool_use"


# --- multi-block: text(idx0) + tool_use(idx1) -------------------------------


@pytest.mark.asyncio
async def test_multi_block_text_then_tool_use_paired_stops() -> None:
    output = [
        {
            "id": "msg_item",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "Let me check.", "annotations": []}
            ],
        },
        {
            "id": "fc_item",
            "type": "function_call",
            "status": "completed",
            "call_id": "toolu_9",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    events = [
        *_prelude(),
        *_text_item_events("Let me check.", output_index=0),
        *_tool_item_events(
            call_id="toolu_9", name="lookup", arguments="{}", output_index=1
        ),
        _completed(output, usage={"input_tokens": 7, "output_tokens": 6}),
    ]
    out = await _collect(events)

    assert _types(out) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Block 0 is text, block 1 is tool_use; each start is paired with its stop.
    assert out[2]["index"] == 0
    assert out[2]["content_block"]["type"] == "text"
    assert out[4] == {"type": "content_block_stop", "index": 0}
    assert out[5]["index"] == 1
    assert out[5]["content_block"]["type"] == "tool_use"
    assert out[7] == {"type": "content_block_stop", "index": 1}
    assert out[8]["delta"]["stop_reason"] == "tool_use"


# --- Copilot verbatim superset tolerance ------------------------------------


@pytest.mark.asyncio
async def test_superset_refusal_to_text_reasoning_and_unknown_dropped() -> None:
    message_output = [
        {
            "id": "msg_item",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        }
    ]
    events = [
        *_prelude(),
        _evt(
            "response.output_item.added",
            output_index=0,
            item={
                "id": "msg_item",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        ),
        # reasoning summary deltas are dropped (structurally-impossible thinking).
        _evt("response.reasoning_summary_text.delta", delta="thinking..."),
        # an entirely unknown event is dropped, never raised.
        _evt("response.some_unknown_event", foo="bar"),
        # a refusal delta is surfaced as text.
        _evt("response.refusal.delta", delta="I cannot help with that."),
        _evt(
            "response.output_item.done",
            output_index=0,
            item=message_output[0],
        ),
        _completed(message_output, usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    out = await _collect(events)

    # No raise; reasoning + unknown produced no events; refusal became a text_delta.
    text_deltas = [
        event
        for event in out
        if event["type"] == "content_block_delta"
        and event["delta"]["type"] == "text_delta"
    ]
    assert len(text_deltas) == 1
    assert text_deltas[0]["delta"]["text"] == "I cannot help with that."
    assert _types(out)[-2:] == ["message_delta", "message_stop"]


# --- mid-stream failure -----------------------------------------------------


@pytest.mark.asyncio
async def test_response_failed_emits_terminal_error_event() -> None:
    events = [
        *_prelude(),
        *_text_item_events("partial"),
        _evt(
            "response.failed",
            response={"status": "failed", "error": {"message": "secret upstream url"}},
        ),
    ]
    out = await _collect(events)

    # An open block (the text block) is closed, then a terminal error event; no
    # message_delta/message_stop after a failure.
    assert out[-1]["type"] == "error"
    assert out[-1]["error"]["type"] == "api_error"
    # Secret-free: the upstream error text is never echoed.
    assert "secret upstream url" not in out[-1]["error"]["message"]
    assert "message_stop" not in _types(out)


@pytest.mark.asyncio
async def test_upstream_exception_emits_terminal_error_event() -> None:
    async def _boom() -> AsyncIterator[SSEEvent]:
        for event in [*_prelude(), *_text_item_events("partial")]:
            yield event
        raise RuntimeError("connection reset to 10.0.0.1")

    out: list[dict[str, Any]] = []
    async for event in responses_sse_to_anthropic(
        _boom(), model=MODEL, message_id=MESSAGE_ID
    ):
        out.append(event)

    assert out[-1]["type"] == "error"
    # The open text block was closed before the error.
    assert {"type": "content_block_stop", "index": 0} in out
    # Secret-free: only the exception class name is surfaced.
    assert "10.0.0.1" not in out[-1]["error"]["message"]
    assert "RuntimeError" in out[-1]["error"]["message"]


# --- truncated upstream (no response.completed) -----------------------------


@pytest.mark.asyncio
async def test_truncated_upstream_still_ends_with_message_delta_and_stop() -> None:
    events = [
        *_prelude(),
        *_text_item_events("half a th"),
        # stream ends here: NO response.completed.
    ]
    out = await _collect(events)

    assert _types(out)[-2:] == ["message_delta", "message_stop"]
    # A truncated stream falls back to end_turn and emits no usage tokens.
    assert out[-2]["delta"]["stop_reason"] == "end_turn"
    assert out[-2]["usage"] == {"output_tokens": 0}
    # The open block is still closed before the terminal sequence.
    assert {"type": "content_block_stop", "index": 0} in out


# --- empty upstream ---------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_upstream_minimal_terminal_sequence() -> None:
    out = await _collect([])
    assert _types(out) == ["message_start", "ping", "message_delta", "message_stop"]
    assert out[2]["delta"]["stop_reason"] == "end_turn"


# --- peek-first: pre-stream exception propagates ----------------------------


@pytest.mark.asyncio
async def test_pre_stream_exception_propagates_before_any_yield() -> None:
    """A failure on the very first __anext__ must escape the mapper unhandled.

    The ASGI caller awaits the first yield from the mapper before committing the
    200 header; if the first __anext__ raises, the mapper yields nothing, and the
    caller returns a 502 JSON body instead.
    """

    async def _fail_immediately() -> AsyncIterator[SSEEvent]:
        raise RuntimeError("auth failure sk-SECRET")
        # unreachable; satisfies the type checker
        yield SSEEvent(event="x", data={})  # noqa: unreachable

    collected: list[dict[str, Any]] = []
    with pytest.raises(RuntimeError, match="auth failure sk-SECRET"):
        async for event in responses_sse_to_anthropic(
            _fail_immediately(), model=MODEL, message_id=MESSAGE_ID
        ):
            collected.append(event)

    # No events must have been yielded before the exception.
    assert collected == []


# --- MINOR-2: orphan delta before any output_item.added ---------------------


@pytest.mark.asyncio
async def test_orphan_text_delta_before_output_item_added_is_dropped() -> None:
    """An output_text.delta arriving before any output_item.added is dropped.

    No open block exists at that point (_open_kind is None), so the delta cannot
    be attributed to a block. The stream must not crash and must remain well-formed.
    """
    message_output = [
        {
            "id": "msg_item",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hello", "annotations": []}],
        }
    ]
    events = [
        *_prelude(),
        # orphan delta: no output_item.added has been seen yet
        _evt(
            "response.output_text.delta",
            item_id="msg_item",
            output_index=0,
            content_index=0,
            delta="orphan text",
        ),
        # normal item sequence follows
        *_text_item_events("hello"),
        _completed(message_output, usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    out = await _collect(events)

    # Stream must be well-formed and must not contain the orphan text.
    assert _types(out)[-2:] == ["message_delta", "message_stop"]
    all_text = "".join(
        e["delta"]["text"]
        for e in out
        if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"
    )
    assert "orphan text" not in all_text
    assert "hello" in all_text


# --- MINOR-1: missing call_id/name coerced to "" ----------------------------


@pytest.mark.asyncio
async def test_tool_use_missing_call_id_and_name_coerced_to_empty_string() -> None:
    """A function_call item lacking call_id/name must not emit null in the block.

    A null id or name in content_block_start is malformed for Anthropic clients;
    missing values are coerced to "" so the block is at least structurally valid.
    """
    item_id = "fc_degraded"
    events = [
        *_prelude(),
        _evt(
            "response.output_item.added",
            output_index=0,
            item={
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                # call_id and name intentionally absent
                "arguments": "",
            },
        ),
        _evt(
            "response.function_call_arguments.delta",
            item_id=item_id,
            output_index=0,
            delta="{}",
        ),
        _evt(
            "response.output_item.done",
            output_index=0,
            item={"id": item_id, "type": "function_call", "status": "completed"},
        ),
        _completed(
            [{"id": item_id, "type": "function_call", "call_id": None, "name": None}],
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    out = await _collect(events)

    block_start = next(e for e in out if e["type"] == "content_block_start")
    assert block_start["content_block"]["type"] == "tool_use"
    assert block_start["content_block"]["id"] == ""
    assert block_start["content_block"]["name"] == ""
    # Stream must still be well-formed.
    assert _types(out)[-2:] == ["message_delta", "message_stop"]
