"""Shared Responses replay seam for buffered provider adapters (ADR 0002 11.3).

Providers that produce a COMPLETE assistant turn before any event is emitted
(Claude, Auggie, DeepSeek) all replay that turn as the same canonical Responses
SSE sequence. Before this module each adapter carried its own copy of the
sequence and its own storage timing; the store-before-drain defect had to be
fixed separately per adapter. This module is the single owner of:

  * input flattening (Responses ``input`` -> prompt text),
  * the assistant ``message`` output item shape,
  * SSE event encoding (data carries its own ``type``, OpenAI shape),
  * response/message id generation,
  * the stored input-item record for ``previous_response_id`` chaining,
  * the canonical nine-event replay sequence, and
  * the store-before-drain invariant: the completed envelope is stored
    BEFORE the first event is yielded, so a client disconnect mid-stream
    never loses the response for later ``previous_response_id`` chaining or
    ``get_response``/``input_items`` lookups.

The canonical sequence is::

    response.created
    response.in_progress
    response.output_item.added
    response.content_part.added
    response.output_text.delta
    response.output_text.done
    response.content_part.done
    response.output_item.done
    response.completed

The Copilot adapter does NOT consume this seam: it forwards raw upstream SSE
blocks verbatim and never buffers a full turn.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest, SSEEvent
from reverso.protocols.store import ResponseStore

CANONICAL_EVENT_SEQUENCE = (
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
    "response.completed",
)


def flatten_input(value: Any) -> str:
    """Flatten a Responses ``input`` (string or item list) into prompt text.

    A bare string passes through. A list translates each item: message items
    with a role get a role-prefixed segment ("User: ...", "Assistant: ...")
    so the single-shot CLI spines (claude, auggie) can tell speakers apart
    when the caller sent a multi-turn message list; bare-string and untagged
    items remain unlabeled so callers that pre-format their prompt still see
    verbatim text.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    if isinstance(value, list):
        for item in value:
            parts.append(_input_item_to_text(item))
    else:
        parts.append(str(value))
    return "\n\n".join(part for part in parts if part)


_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "developer": "Developer",
}


def _input_item_to_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    text = _input_content_text(item)
    role = item.get("role")
    if isinstance(role, str):
        label = _ROLE_LABELS.get(role)
        if label is not None and text:
            return f"{label}: {text}"
    return text


def _input_content_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    text = item.get("text")
    return text if isinstance(text, str) else ""


def build_prompt(request: ResponsesRequest) -> str:
    """Combine instructions and input into a single prompt string."""
    text = flatten_input(request.input)
    if request.instructions:
        return f"{request.instructions}\n\n{text}" if text else request.instructions
    return text


def message_item(item_id: str, text: str) -> dict[str, Any]:
    """Build a completed Responses assistant message output item."""
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def sse_event(event_type: str, body: dict[str, Any]) -> SSEEvent:
    """Build an SSEEvent whose data carries its own ``type`` (OpenAI shape)."""
    data = {"type": event_type}
    data.update(body)
    return SSEEvent(event=event_type, data=data)


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def record_input_items(request: ResponsesRequest) -> list[dict[str, Any]]:
    """Build the stored input-item record for previous_response_id chaining."""
    value = request.input
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if value is None:
        return []
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": flatten_input(value)}],
        }
    ]


def estimate_usage(prompt: str, output: str) -> dict[str, int]:
    """Approximate token usage from word counts (no upstream usage available)."""
    input_tokens = len(prompt.split())
    output_tokens = len(output.split())
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def buffered_envelope(
    request: ResponsesRequest, *, prompt: str, text: str
) -> ResponseEnvelope:
    """Synthesize the completed envelope for a buffered turn.

    CLI-backed adapters (claude, auggie) produce only assistant text, so the
    envelope shape is fully determined here: fresh response and message ids,
    a single message output item, status completed, and word-count usage.
    The caller still owns storage timing (put_response or replay_turn).
    DeepSeek must NOT use this helper: its envelope carries reasoning_content
    and tool-call items on .raw, which this text-only shape cannot represent.
    """
    return ResponseEnvelope(
        id=new_response_id(),
        model=request.model,
        output=[message_item(new_message_id(), text)],
        status="completed",
        usage=estimate_usage(prompt, text),
        previous_response_id=request.previous_response_id,
    )


def _message_item_events(item: dict[str, Any], *, output_index: int) -> list[SSEEvent]:
    """Per-item SSE sequence for an assistant message output item.

    Emits the six events that frame a single message item between the envelope
    prelude and the terminal response.completed: output_item.added (in_progress
    shell), content_part.added (empty output_text), output_text.delta with the
    full text as one delta, output_text.done, content_part.done, and
    output_item.done with the completed item. The message-only envelope
    contract that the buffered replay has always served is bytewise unchanged
    when this is the only item and output_index is 0.
    """
    message_id = item["id"]
    text = item["content"][0]["text"]
    return [
        sse_event(
            "response.output_item.added",
            {
                "output_index": output_index,
                "item": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        ),
        sse_event(
            "response.content_part.added",
            {
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
        sse_event(
            "response.output_text.delta",
            {
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "delta": text,
            },
        ),
        sse_event(
            "response.output_text.done",
            {
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "text": text,
            },
        ),
        sse_event(
            "response.content_part.done",
            {
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            },
        ),
        sse_event(
            "response.output_item.done",
            {"output_index": output_index, "item": item},
        ),
    ]


def _function_call_item_events(
    item: dict[str, Any], *, output_index: int
) -> list[SSEEvent]:
    """Per-item SSE sequence for a function_call output item.

    Codex (and any Responses-API client) expects every output item to be
    announced via response.output_item.added and resolved via
    response.output_item.done; function_call items also carry their arguments
    payload via response.function_call_arguments.delta/done. Without these
    events the streamed tool loop never sees the tool call (the function_call
    only appears inside response.completed, after the client has already
    decided no tool was requested). The full arguments string is emitted as
    one delta because the buffered envelope only knows the final argument
    payload; the incremental streaming path may emit multiple deltas through
    the same item_id when it has chunk-level deltas to forward.
    """
    item_id = item.get("id")
    call_id = item.get("call_id")
    name = item.get("name")
    arguments = item.get("arguments") or ""
    in_progress_item = {
        "id": item_id,
        "type": "function_call",
        "status": "in_progress",
        "call_id": call_id,
        "name": name,
        "arguments": "",
    }
    return [
        sse_event(
            "response.output_item.added",
            {"output_index": output_index, "item": in_progress_item},
        ),
        sse_event(
            "response.function_call_arguments.delta",
            {
                "item_id": item_id,
                "output_index": output_index,
                "delta": arguments,
            },
        ),
        sse_event(
            "response.function_call_arguments.done",
            {
                "item_id": item_id,
                "output_index": output_index,
                "arguments": arguments,
            },
        ),
        sse_event(
            "response.output_item.done",
            {"output_index": output_index, "item": item},
        ),
    ]


def _item_events(item: dict[str, Any], *, output_index: int) -> list[SSEEvent]:
    """Dispatch per-item SSE emission by item type."""
    item_type = item.get("type")
    if item_type == "function_call":
        return _function_call_item_events(item, output_index=output_index)
    return _message_item_events(item, output_index=output_index)


async def replay_turn(
    envelope: ResponseEnvelope,
    *,
    store: ResponseStore,
    input_items: list[dict[str, Any]],
) -> AsyncIterator[SSEEvent]:
    """Store ``envelope`` then yield the canonical Responses event sequence.

    ``envelope`` is a COMPLETED turn; every item in ``envelope.output`` gets
    its own canonical per-item SSE sequence between the envelope prelude
    (response.created, response.in_progress) and the terminal
    response.completed. Message items emit the six-event message shape;
    function_call items emit the four-event function_call shape (added,
    function_call_arguments.delta, function_call_arguments.done, done) so a
    streaming Responses client can see the tool call as it is produced. The
    single-message envelope case keeps the historical nine-event sequence
    bytewise unchanged (regression-pinned in tests/unit/test_replay.py and
    tests/unit/test_responses_sse_completion.py).

    The store write happens before the first yield (store-before-drain
    invariant), so an early ``aclose`` from a disconnecting client cannot
    lose the response. The incremental streaming replay
    (``replay_incremental``) relaxes this for the streamed branch and is
    documented separately.
    """
    store.put_response(envelope, input_items)

    base_response = {
        "id": envelope.id,
        "object": "response",
        "status": "in_progress",
        "model": envelope.model,
    }
    yield sse_event("response.created", {"response": dict(base_response)})
    yield sse_event("response.in_progress", {"response": dict(base_response)})
    for index, item in enumerate(envelope.output):
        for event in _item_events(item, output_index=index):
            yield event
    completed_response = {
        "id": envelope.id,
        "object": "response",
        "status": "completed",
        "model": envelope.model,
        "output": envelope.output,
        "usage": envelope.usage,
    }
    yield sse_event("response.completed", {"response": completed_response})


async def replay_incremental(
    upstream_chunks: AsyncIterator[dict[str, Any]],
    *,
    response_id: str,
    message_id: str,
    model: str,
    store: ResponseStore,
    input_items: list[dict[str, Any]],
    finalize: Any,
) -> AsyncIterator[SSEEvent]:
    """Stream incremental Responses events from an upstream chunk iterator.

    Sister to ``replay_turn`` for providers whose upstream supports incremental
    token deltas (deepseek under D1). The adapter contributes ONLY a chunk
    async iterator (each chunk a dict carrying ``text``, ``reasoning_text``,
    ``done``, ``usage``, and optional ``tool_calls``) and a ``finalize``
    callable that builds the completed ``ResponseEnvelope`` from the
    accumulated state. This helper owns canonical envelope event emission and
    the finalize step.

    Event sequence: ``response.created`` and ``response.in_progress`` fire
    immediately so the gateway commits the 200 header at first byte; then
    ``response.output_item.added`` (message shell, status=in_progress) and
    ``response.content_part.added`` (empty output_text). Each upstream chunk
    carrying non-empty ``text`` becomes one ``response.output_text.delta``;
    reasoning text accumulates without emitting events; tool-call deltas
    accumulate into a per-call argument buffer keyed by index. The terminal
    chunk (``done=True``) triggers a single call to ``finalize(full_text,
    full_reasoning, usage, accumulated_tool_calls)`` to build the envelope,
    after which the store write happens and the terminal envelope events fire
    in the shape ``replay_turn`` emits (output_text.done, content_part.done,
    output_item.done, then per-extra-item events for any function_call items
    discovered at finalize, then response.completed).

    Store-before-drain is RELAXED for this path (see ADR 0004): the store
    write moves from "before first yield" to "at finalize, after the last
    delta and before response.completed". A client that aborts between the
    last delta and response.completed will not find the envelope in the store.
    Pinned by tests/unit/test_replay.py::test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta
    so a future refactor cannot silently tighten or further relax it.

    Mid-stream upstream failures propagate unwrapped after the first delta
    has been emitted, so the gateway's ``responses_app._stream`` translates
    them into ``response.failed`` + ``[DONE]``. Pre-emission failures must
    raise BEFORE this generator yields its first event so the gateway can
    synthesise a structured 502 instead of a truncated 200 stream.
    """
    base_response = {
        "id": response_id,
        "object": "response",
        "status": "in_progress",
        "model": model,
    }
    yield sse_event("response.created", {"response": dict(base_response)})
    yield sse_event("response.in_progress", {"response": dict(base_response)})
    yield sse_event(
        "response.output_item.added",
        {
            "output_index": 0,
            "item": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
    )
    yield sse_event(
        "response.content_part.added",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_buffer: dict[int, dict[str, Any]] = {}
    last_tool_call_index: int | None = None
    usage: dict[str, Any] | None = None

    async for chunk in upstream_chunks:
        chunk_text = chunk.get("text") or ""
        chunk_reasoning = chunk.get("reasoning_text") or ""
        chunk_tool_calls = chunk.get("tool_calls") or []
        chunk_usage = chunk.get("usage")
        done = bool(chunk.get("done"))

        if chunk_reasoning:
            reasoning_parts.append(chunk_reasoning)
        if isinstance(chunk_tool_calls, list):
            for raw_call in chunk_tool_calls:
                if not isinstance(raw_call, dict):
                    continue
                index = raw_call.get("index")
                if not isinstance(index, int):
                    # Index-less continuation delta: deepseek always sends an
                    # index so this only fires on upstreams that omit it on
                    # follow-up deltas. Re-use the last-seen index so the
                    # continuation accumulates into the same slot instead of
                    # silently re-keying an existing one via len(buffer).
                    if last_tool_call_index is None:
                        index = len(tool_calls_buffer)
                    else:
                        index = last_tool_call_index
                last_tool_call_index = index
                slot = tool_calls_buffer.setdefault(
                    index,
                    {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if raw_call.get("id") is not None:
                    slot["id"] = raw_call.get("id")
                if raw_call.get("type") is not None:
                    slot["type"] = raw_call.get("type")
                function_delta = raw_call.get("function")
                if isinstance(function_delta, dict):
                    name_delta = function_delta.get("name")
                    if isinstance(name_delta, str) and name_delta:
                        slot["function"]["name"] = (
                            slot["function"].get("name") or ""
                        ) + name_delta
                    args_delta = function_delta.get("arguments")
                    if isinstance(args_delta, str) and args_delta:
                        slot["function"]["arguments"] = (
                            slot["function"].get("arguments") or ""
                        ) + args_delta
        if chunk_usage is not None:
            usage = chunk_usage
        if chunk_text:
            text_parts.append(chunk_text)
            yield sse_event(
                "response.output_text.delta",
                {
                    "item_id": message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": chunk_text,
                },
            )
        if done:
            break

    full_text = "".join(text_parts)
    full_reasoning = "".join(reasoning_parts) if reasoning_parts else None
    accumulated_tool_calls = [
        tool_calls_buffer[index] for index in sorted(tool_calls_buffer)
    ]

    envelope: ResponseEnvelope = finalize(
        full_text=full_text,
        full_reasoning=full_reasoning,
        usage=usage,
        tool_calls=accumulated_tool_calls,
    )

    # Store-before-drain RELAXED: write at finalize-time, after the last
    # delta and before response.completed. See ADR 0004.
    store.put_response(envelope, input_items)

    primary_item = (
        envelope.output[0]
        if envelope.output
        else {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}],
        }
    )
    yield sse_event(
        "response.output_text.done",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": full_text,
        },
    )
    yield sse_event(
        "response.content_part.done",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        },
    )
    yield sse_event(
        "response.output_item.done",
        {"output_index": 0, "item": primary_item},
    )
    # Extra output items beyond the message (function_call surfacing) reuse
    # the per-item helpers from replay_turn, so the streamed function_call
    # contract matches the buffered one byte-for-byte at the event level.
    for extra_index, extra_item in enumerate(envelope.output[1:], start=1):
        for extra_event in _item_events(extra_item, output_index=extra_index):
            yield extra_event

    completed_response = {
        "id": envelope.id,
        "object": "response",
        "status": "completed",
        "model": envelope.model,
        "output": envelope.output,
        "usage": envelope.usage,
    }
    yield sse_event("response.completed", {"response": completed_response})
