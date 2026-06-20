"""Pure Responses-SSE -> Anthropic-Messages-SSE streaming mapper (ADR 0006, G004).

A single pure async generator, ``responses_sse_to_anthropic``, that consumes a
Responses ``SSEEvent`` iterator (the FROZEN replay seam from
``protocols/replay.py`` plus the Copilot verbatim superset) and yields
Anthropic-native streaming events as dicts. It performs NO ASGI or network work:
it is pure over its input iterator and is unit-tested directly over a hand-built
iterator. The ASGI app (anthropic_app) owns header commitment and wire encoding.

Anthropic streaming grammar emitted (Claude-Code-observed parity):
  message_start -> ping -> (content blocks) -> message_delta -> message_stop.

Content blocks:
  - text: content_block_start(text) / content_block_delta(text_delta) /
    content_block_stop, mapped from response.output_text.delta / .done.
  - tool_use: content_block_start(tool_use, id, name) /
    content_block_delta(input_json_delta) / content_block_stop, mapped from the
    SEPARATE function-call replay path (response.function_call_arguments.delta /
    .done). This is its own path, NOT one of the canonical nine text events.

Block indexing: a fresh Anthropic content-block index is assigned on each
response.output_item.added, so a multi-item turn (text then tool_use) yields
block index 0 then 1. Every content_block_start is paired with a
content_block_stop before the next block opens or the message ends.

Copilot verbatim SUPERSET tolerance (responses_parity_surface.json):
  - response.refusal.delta -> a text_delta (refusal text is surfaced as text).
  - response.reasoning_summary_text.delta -> DROP (reasoning deltas are discarded
    by the Responses seam; ADR 0006 streamed-thinking = structurally-impossible).
  - any UNKNOWN event -> default DROP with a debug log; NEVER raise.

Streamed thinking: NO thinking content_block is synthesized. ADR 0006 classes
streamed thinking deltas as structurally-impossible-M1 (the Responses replay seam
discards reasoning deltas), so the mapper does not invent a thinking block.

Self-protection (principled contract guarantees, not ad-hoc shims):
  - Mid-stream failure (response.failed, or an exception raised by the upstream
    iterator) closes any open block, emits a terminal Anthropic ``error`` event,
    and stops cleanly. A client never sees a half-open block.
  - Empty or truncated upstream (zero events, or the iterator ends before
    response.completed) still yields a minimal well-formed terminal sequence
    (message_start -> ping -> message_delta(end_turn) -> message_stop) so a client
    can always interpret the stream.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from reverso.protocols.adapter import SSEEvent
from reverso.protocols.anthropic_translate import map_stop_reason

logger = logging.getLogger(__name__)


async def responses_sse_to_anthropic(
    events: AsyncIterator[SSEEvent],
    *,
    model: str,
    message_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Map a Responses SSEEvent stream into Anthropic Messages streaming events.

    ``events`` is the Responses replay/forward iterator (canonical nine events,
    the function-call replay path, or the Copilot verbatim superset). ``model``
    is echoed in the message envelope; ``message_id`` is the ``msg_`` id minted by
    the caller. Yields Anthropic event dicts each carrying their own ``type``; the
    caller encodes them onto the wire as ``event: <type>\\ndata: <json>\\n\\n``.

    The sequence is always well-formed: message_start, exactly one ping, zero or
    more paired content blocks, a single message_delta, and message_stop, or a
    terminal error event on mid-stream failure.
    """
    state = _StreamState(model=model, message_id=message_id)

    # message_start + exactly one ping, emitted up front so a truncated or empty
    # upstream still produces a client-interpretable stream (self-protection).
    yield state.message_start_event()
    yield _ping_event()

    try:
        async for event in events:
            for out in state.consume(event):
                yield out
            if state.completed:
                break
    except Exception as exc:  # noqa: BLE001 - any upstream failure -> terminal error
        # Mid-stream failure: close any open block, emit a terminal Anthropic
        # error event, stop cleanly. The class name only (no payload) keeps the
        # message secret-free, mirroring the non-streaming 502 envelope.
        logger.warning("anthropic stream upstream failed: %s", type(exc).__name__)
        for out in state.close_open_block():
            yield out
        yield _error_event(f"upstream backend error ({type(exc).__name__})")
        return

    if state.failed:
        # response.failed was seen in-band: close the block and emit a terminal
        # error event rather than a normal message_delta/message_stop.
        for out in state.close_open_block():
            yield out
        yield _error_event(state.failure_message)
        return

    # Normal completion OR truncated/empty upstream (no response.completed): the
    # terminal sequence is synthesized either way so the stream is always
    # well-formed. A truncated stream falls back to end_turn.
    for out in state.close_open_block():
        yield out
    yield state.message_delta_event()
    yield _message_stop_event()


class _StreamState:
    """Mutable per-stream block/index bookkeeping for the pure mapper.

    Tracks the current Anthropic content-block index, whether a block is open and
    its kind, and the accumulated stop_reason / usage. Holds no I/O; ``consume``
    turns one Responses SSEEvent into zero or more Anthropic event dicts.
    """

    __slots__ = (
        "model",
        "message_id",
        "_block_index",
        "_open_kind",
        "_stop_reason",
        "_output_tokens",
        "completed",
        "failed",
        "failure_message",
    )

    def __init__(self, *, model: str, message_id: str) -> None:
        self.model = model
        self.message_id = message_id
        # -1 means no block has been opened yet; the first output_item.added
        # opens block index 0.
        self._block_index = -1
        self._open_kind: str | None = None
        self._stop_reason: str = "end_turn"
        self._output_tokens: int = 0
        self.completed = False
        self.failed = False
        self.failure_message = "upstream backend error"

    def message_start_event(self) -> dict[str, Any]:
        """The Anthropic message_start envelope (empty content, input placeholder)."""
        return {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                # input_tokens is a placeholder: buffered replay reports usage
                # only at response.completed, mapped into message_delta below.
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }

    def message_delta_event(self) -> dict[str, Any]:
        """The terminal message_delta carrying stop_reason and output usage only."""
        return {
            "type": "message_delta",
            "delta": {
                "stop_reason": self._stop_reason,
                "stop_sequence": None,
            },
            "usage": {"output_tokens": self._output_tokens},
        }

    def consume(self, event: SSEEvent) -> list[dict[str, Any]]:
        """Map one Responses SSEEvent into zero or more Anthropic events."""
        event_type = event.event or event.data.get("type", "")
        data = event.data if isinstance(event.data, dict) else {}

        if event_type == "response.output_item.added":
            return self._on_output_item_added(data)
        if event_type == "response.output_text.delta":
            return self._on_text_delta(data.get("delta"))
        if event_type == "response.refusal.delta":
            # Copilot superset: a refusal stream is surfaced as text so the client
            # still receives the model's refusal as readable content.
            return self._on_text_delta(data.get("delta"))
        if event_type == "response.function_call_arguments.delta":
            return self._on_tool_input_delta(data.get("delta"))
        if event_type == "response.output_item.done":
            return self.close_open_block()
        if event_type == "response.completed":
            self._absorb_completion(data)
            self.completed = True
            return []
        if event_type == "response.failed":
            self.failed = True
            # Secret-free: an upstream failure payload may carry internal detail,
            # so a generic backend-failure string is surfaced, never its text.
            self.failure_message = "upstream backend error"
            self.completed = True
            return []
        if event_type == "response.reasoning_summary_text.delta":
            # Streamed thinking is structurally-impossible-M1: reasoning deltas
            # are dropped, no thinking content_block is synthesized.
            return []
        if event_type in _SILENT_DROP_EVENTS:
            # Canonical envelope/scaffolding events with no Anthropic analogue.
            return []
        # Unknown event: default DROP with a debug log, never raise.
        logger.debug("anthropic stream dropping unknown event %r", event_type)
        return []

    def _on_output_item_added(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Open a new content block for an output item (text or tool_use).

        A new item closes any still-open block first (defensive: the canonical
        sequence pairs each added with a done, but the superset may not), then
        increments the Anthropic block index and emits content_block_start.
        """
        out = self.close_open_block()
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        item_type = item.get("type")
        self._block_index += 1
        if item_type == "function_call":
            self._open_kind = "tool_use"
            out.append(
                {
                    "type": "content_block_start",
                    "index": self._block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": item.get("call_id"),
                        "name": item.get("name"),
                        "input": {},
                    },
                }
            )
        else:
            self._open_kind = "text"
            out.append(
                {
                    "type": "content_block_start",
                    "index": self._block_index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        return out

    def _on_text_delta(self, delta: Any) -> list[dict[str, Any]]:
        """Emit a text_delta for the open text block, if any non-empty delta."""
        if self._open_kind != "text" or not isinstance(delta, str) or not delta:
            return []
        return [
            {
                "type": "content_block_delta",
                "index": self._block_index,
                "delta": {"type": "text_delta", "text": delta},
            }
        ]

    def _on_tool_input_delta(self, delta: Any) -> list[dict[str, Any]]:
        """Emit an input_json_delta for the open tool_use block."""
        if self._open_kind != "tool_use" or not isinstance(delta, str) or not delta:
            return []
        return [
            {
                "type": "content_block_delta",
                "index": self._block_index,
                "delta": {"type": "input_json_delta", "partial_json": delta},
            }
        ]

    def close_open_block(self) -> list[dict[str, Any]]:
        """Emit content_block_stop for the open block, if one is open."""
        if self._open_kind is None:
            return []
        index = self._block_index
        self._open_kind = None
        return [{"type": "content_block_stop", "index": index}]

    def _absorb_completion(self, data: dict[str, Any]) -> None:
        """Read stop_reason and output usage from a response.completed body."""
        response = data.get("response")
        if not isinstance(response, dict):
            return
        usage = response.get("usage")
        if isinstance(usage, dict):
            tokens = usage.get("output_tokens")
            if isinstance(tokens, int):
                self._output_tokens = tokens
        self._stop_reason = _stop_reason_from_response(response)


# Canonical Responses envelope events that carry no Anthropic streaming analogue.
# They frame the stream (created/in_progress) or restate text already streamed via
# deltas (content_part.*, output_text.done) so they are dropped silently rather
# than logged as unknown.
_SILENT_DROP_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.content_part.added",
        "response.output_text.done",
        "response.content_part.done",
        "response.function_call_arguments.done",
    }
)


def _stop_reason_from_response(response: dict[str, Any]) -> str:
    """Derive the Anthropic stop_reason from a completed Responses body.

    A turn whose output contains any function_call item stops with tool_use;
    otherwise an explicit Responses stop reason is mapped through the shared
    translator and unknown states fall back to end_turn.
    """
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return "tool_use"
    return map_stop_reason(response.get("stop_reason"))


def _ping_event() -> dict[str, Any]:
    return {"type": "ping"}


def _message_stop_event() -> dict[str, Any]:
    return {"type": "message_stop"}


def _error_event(message: str) -> dict[str, Any]:
    """Build the terminal Anthropic streaming error event.

    Shape mirrors the non-streaming error envelope:
    {"type": "error", "error": {"type": "api_error", "message": <msg>}}.
    """
    return {"type": "error", "error": {"type": "api_error", "message": message}}
