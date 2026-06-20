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

Peek-first protocol: message_start and ping are NOT yielded until the first
upstream event is in hand. This preserves the ASGI caller's ability to return a
non-streaming 502 error envelope when the upstream raises on the very first
``__anext__`` (connect/auth/setup failure), because the 200 text/event-stream
header must not be committed before that exception surfaces to the caller.

  - Pre-stream failure (exception on the first __anext__): NOT caught here;
    propagates to the ASGI caller, which can still return a 502 JSON body.
  - Empty upstream (StopAsyncIteration on the first peek, zero events): treated
    as success, NOT a failure; yields the minimal well-formed 200 stream
    (message_start -> ping -> message_delta(end_turn) -> message_stop).
  - Mid-stream failure (exception after the first event): caught here, closes any
    open block, emits a terminal Anthropic ``error`` event, stops cleanly. At this
    point the 200 header is already committed, so an in-band error is the only
    safe channel.
  - Truncated upstream (ends before response.completed, at least one event seen):
    synthesizes the same minimal terminal sequence as the empty case (end_turn,
    zero output tokens) so the stream is always client-interpretable.
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

    Peek-first: the first upstream ``__anext__`` is awaited BEFORE yielding
    message_start, so a pre-stream exception (connect/auth/setup failure) escapes
    to the ASGI caller rather than being swallowed after a 200 header is sent. An
    empty upstream (StopAsyncIteration on the peek) is NOT an error; it still
    produces the minimal well-formed terminal sequence. Mid-stream failures (after
    the first event is in hand) are caught and converted to a terminal Anthropic
    error event; at that point the 200 header is committed and in-band is the only
    safe channel.

    The sequence is always well-formed: message_start, exactly one ping, zero or
    more paired content blocks, a single message_delta and message_stop on success,
    or a terminal error event on mid-stream failure.
    """
    state = _StreamState(model=model, message_id=message_id)
    it = events.__aiter__()

    # Peek the first upstream event. This call is NOT inside a try/except so that
    # a pre-stream exception (e.g. auth failure, connection error) propagates to
    # the ASGI caller before we yield anything. The caller can then return a 502
    # JSON body because the 200 text/event-stream header has not been sent yet.
    try:
        first_event = await it.__anext__()
    except StopAsyncIteration:
        # Empty upstream: not an error. Synthesize the minimal well-formed stream
        # so a client always receives an interpretable response.
        yield state.message_start_event()
        yield _ping_event()
        yield state.message_delta_event()
        yield _message_stop_event()
        return

    # First event is in hand: commit message_start and the single ping, then
    # process it and continue the loop. Mid-stream failures from this point are
    # caught below and converted to in-band terminal error events.
    yield state.message_start_event()
    yield _ping_event()

    try:
        for out in state.consume(first_event):
            yield out
        if not state.completed:
            async for event in it:
                for out in state.consume(event):
                    yield out
                if state.completed:
                    break
    except Exception as exc:  # noqa: BLE001 - mid-stream failure -> in-band error
        # Mid-stream: 200 header already committed. Close any open block and emit
        # a terminal Anthropic error event. Class name only (no payload) to keep
        # the message secret-free, mirroring the non-streaming 502 envelope.
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

    # Normal completion OR truncated upstream (no response.completed seen): the
    # terminal sequence is synthesized either way so the stream is always
    # well-formed. A truncated stream falls back to end_turn with zero tokens.
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
            # Coerce missing call_id/name to "" rather than None: a null id or
            # name in content_block_start is malformed for Anthropic clients.
            call_id = item.get("call_id") or ""
            name = item.get("name") or ""
            out.append(
                {
                    "type": "content_block_start",
                    "index": self._block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
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
