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
    """Flatten a Responses ``input`` (string or item list) into prompt text."""
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
    return "\n".join(part for part in parts if part)


def _input_item_to_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
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


async def replay_turn(
    envelope: ResponseEnvelope,
    *,
    store: ResponseStore,
    input_items: list[dict[str, Any]],
) -> AsyncIterator[SSEEvent]:
    """Store ``envelope`` then yield the canonical Responses event sequence.

    ``envelope`` must be a COMPLETED turn whose first output item is the
    assistant message (buffered providers construct it that way). The store
    write happens before the first yield (store-before-drain invariant), so an
    early ``aclose`` from a disconnecting client cannot lose the response.
    """
    store.put_response(envelope, input_items)

    primary = envelope.output[0]
    message_id = primary["id"]
    text = primary["content"][0]["text"]

    base_response = {
        "id": envelope.id,
        "object": "response",
        "status": "in_progress",
        "model": envelope.model,
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
    yield sse_event(
        "response.output_text.delta",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
    )
    yield sse_event(
        "response.output_text.done",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
    )
    yield sse_event(
        "response.content_part.done",
        {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
    )
    yield sse_event(
        "response.output_item.done",
        {"output_index": 0, "item": primary},
    )
    completed_response = {
        "id": envelope.id,
        "object": "response",
        "status": "completed",
        "model": envelope.model,
        "output": envelope.output,
        "usage": envelope.usage,
    }
    yield sse_event("response.completed", {"response": completed_response})
