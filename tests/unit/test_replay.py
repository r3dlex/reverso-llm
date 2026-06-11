"""Invariant tests for the shared Responses replay seam.

The replay interface is the test surface: the canonical event sequence and the
store-before-drain invariant are asserted ONCE here, against
``reverso.protocols.replay``, instead of once per buffered adapter. The Claude
regression test at the bottom locks in the latent store-after-drain defect that
the seam fixed (Claude previously stored only after response.completed).
"""

from __future__ import annotations

import pytest

from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest
from reverso.protocols.adapters.claude import (
    ClaudeAdapter,
    ClaudeAuthError,
    OAUTH_METHOD,
)
from reverso.protocols.auth import AuthResolution
from reverso.protocols.replay import (
    CANONICAL_EVENT_SEQUENCE,
    build_prompt,
    estimate_usage,
    flatten_input,
    message_item,
    new_message_id,
    new_response_id,
    record_input_items,
    replay_incremental,
    replay_turn,
    sse_event,
)
from reverso.protocols.store import ResponseStore


def _envelope(text: str = "Replayed body.") -> ResponseEnvelope:
    return ResponseEnvelope(
        id=new_response_id(),
        model="test-model",
        output=[message_item(new_message_id(), text)],
        status="completed",
        usage=estimate_usage("prompt words", text),
    )


async def test_replay_turn_yields_canonical_sequence() -> None:
    store = ResponseStore()
    events = [
        event async for event in replay_turn(_envelope(), store=store, input_items=[])
    ]

    assert tuple(event.event for event in events) == CANONICAL_EVENT_SEQUENCE
    # OpenAI shape: every event's data carries its own type.
    for event in events:
        assert event.data["type"] == event.event


async def test_replay_turn_stores_before_first_yield() -> None:
    # Falsifiable: storing after response.completed would make get_response
    # return None here because the stream is closed after one event.
    store = ResponseStore()
    envelope = _envelope()
    items = [{"role": "user", "content": "hi"}]

    gen = replay_turn(envelope, store=store, input_items=items)
    first = await gen.__anext__()
    await gen.aclose()

    assert first.event == "response.created"
    stored = store.get_response(envelope.id)
    assert stored is not None
    assert stored.id == envelope.id
    recorded = store.get_input_items(envelope.id)
    assert recorded is not None
    assert recorded.data == items


async def test_replay_turn_event_payloads_carry_turn_text() -> None:
    store = ResponseStore()
    envelope = _envelope("Hello replay.")
    events = [
        event async for event in replay_turn(envelope, store=store, input_items=[])
    ]
    by_type = {event.event: event.data for event in events}

    message_id = envelope.output[0]["id"]
    assert by_type["response.created"]["response"]["id"] == envelope.id
    assert by_type["response.created"]["response"]["status"] == "in_progress"
    assert by_type["response.output_text.delta"]["delta"] == "Hello replay."
    assert by_type["response.output_text.done"]["text"] == "Hello replay."
    assert by_type["response.output_item.added"]["item"]["id"] == message_id
    assert by_type["response.output_item.done"]["item"] == envelope.output[0]
    completed = by_type["response.completed"]["response"]
    assert completed["status"] == "completed"
    assert completed["output"] == envelope.output
    assert completed["usage"] == envelope.usage


async def test_replay_turn_completed_includes_full_output_list() -> None:
    # A turn may carry trailing non-message items (e.g. surfaced tool calls).
    # Every output item now emits its own canonical per-item SSE sequence;
    # response.completed still carries the full output array.
    store = ResponseStore()
    envelope = _envelope("With tools.")
    function_call_id = new_message_id()
    envelope.output.append(
        {
            "id": function_call_id,
            "type": "function_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{}",
        }
    )
    events = [
        event async for event in replay_turn(envelope, store=store, input_items=[])
    ]

    done_items = [e for e in events if e.event == "response.output_item.done"]
    assert [e.data["item"] for e in done_items] == envelope.output
    assert [e.data["output_index"] for e in done_items] == [0, 1]
    completed = events[-1].data["response"]
    assert len(completed["output"]) == 2
    assert completed["output"][1]["type"] == "function_call"


async def test_replay_turn_emits_function_call_item_events_for_mixed_output() -> None:
    """Mixed message+function_call output produces per-item canonical events.

    Regression test for the bug C1 surfaced: a streamed deepseek turn with a
    tool call only exposed the function_call inside response.completed, so the
    Codex tool loop never saw it. Each function_call item must announce itself
    via response.output_item.added (type=function_call, status=in_progress,
    empty arguments), emit its arguments via
    response.function_call_arguments.delta and .done, and resolve via
    response.output_item.done with the completed item. output_index must
    advance per item.
    """
    store = ResponseStore()
    envelope = _envelope("Hello before the tool call.")
    function_call_id = new_message_id()
    envelope.output.append(
        {
            "id": function_call_id,
            "type": "function_call",
            "status": "completed",
            "call_id": "call_42",
            "name": "get_weather",
            "arguments": '{"location":"Paris"}',
        }
    )
    events = [
        event async for event in replay_turn(envelope, store=store, input_items=[])
    ]
    types = [event.event for event in events]

    assert types == [
        "response.created",
        "response.in_progress",
        # message item at output_index=0
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        # function_call item at output_index=1
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]

    added_events = [e for e in events if e.event == "response.output_item.added"]
    assert added_events[0].data["output_index"] == 0
    assert added_events[0].data["item"]["type"] == "message"
    assert added_events[1].data["output_index"] == 1
    fn_added = added_events[1].data["item"]
    assert fn_added["type"] == "function_call"
    assert fn_added["status"] == "in_progress"
    assert fn_added["id"] == function_call_id
    assert fn_added["call_id"] == "call_42"
    assert fn_added["name"] == "get_weather"
    # Arguments are empty on the in_progress shell; the actual payload arrives
    # via function_call_arguments.delta below.
    assert fn_added["arguments"] == ""

    args_delta = next(
        e for e in events if e.event == "response.function_call_arguments.delta"
    )
    args_done = next(
        e for e in events if e.event == "response.function_call_arguments.done"
    )
    assert args_delta.data["item_id"] == function_call_id
    assert args_delta.data["output_index"] == 1
    assert args_delta.data["delta"] == '{"location":"Paris"}'
    assert args_done.data["item_id"] == function_call_id
    assert args_done.data["output_index"] == 1
    assert args_done.data["arguments"] == '{"location":"Paris"}'

    done_events = [e for e in events if e.event == "response.output_item.done"]
    assert [e.data["output_index"] for e in done_events] == [0, 1]
    assert done_events[1].data["item"]["arguments"] == '{"location":"Paris"}'
    assert done_events[1].data["item"]["status"] == "completed"


async def test_replay_turn_handles_function_call_only_output() -> None:
    """An envelope whose only output item is a function_call must not crash.

    Before the per-item replay fix, replay_turn dereferenced
    output[0]["content"][0]["text"] unconditionally, which crashed when
    output[0] was a function_call. The replay seam now dispatches by item
    type so a tools-only turn streams cleanly.
    """
    store = ResponseStore()
    function_call_id = new_message_id()
    envelope = ResponseEnvelope(
        id=new_response_id(),
        model="test-model",
        output=[
            {
                "id": function_call_id,
                "type": "function_call",
                "status": "completed",
                "call_id": "call_lone",
                "name": "lookup",
                "arguments": '{"q":"x"}',
            }
        ],
        status="completed",
        usage=estimate_usage("prompt", ""),
    )

    events = [
        event async for event in replay_turn(envelope, store=store, input_items=[])
    ]
    types = [event.event for event in events]

    assert types == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    added = next(e for e in events if e.event == "response.output_item.added")
    assert added.data["output_index"] == 0
    assert added.data["item"]["type"] == "function_call"
    completed = events[-1].data["response"]
    assert completed["output"][0]["type"] == "function_call"


async def test_replay_turn_single_message_event_sequence_is_regression_pinned() -> None:
    """The single-message envelope keeps the historical 9-event sequence.

    Pins the buffered claude/auggie replay shape: anything that changes the
    order, count, or shape of these nine events for a message-only envelope
    breaks the existing canonical contract and must fail this test.
    """
    store = ResponseStore()
    envelope = _envelope("Single message.")
    events = [
        event async for event in replay_turn(envelope, store=store, input_items=[])
    ]
    assert tuple(event.event for event in events) == CANONICAL_EVENT_SEQUENCE
    # Every event for the single-message envelope is anchored at output_index 0.
    item_added = next(e for e in events if e.event == "response.output_item.added")
    assert item_added.data["output_index"] == 0
    item_done = next(e for e in events if e.event == "response.output_item.done")
    assert item_done.data["output_index"] == 0


def test_flatten_input_handles_string_items_and_parts() -> None:
    """Untagged items concatenate with blank-line separators between items.

    B4 lifts the join to ``\\n\\n`` so a multi-item input reads as discrete
    segments in the CLI prompt rather than running together. Within a single
    item's ``content`` parts the inner join stays ``\\n`` so an authored part
    list is not over-spaced.
    """
    assert flatten_input(None) == ""
    assert flatten_input("plain") == "plain"
    assert flatten_input(
        [
            "bare string",
            {"content": "dict content"},
            {"content": [{"text": "part text"}, "part string"]},
            {"text": "fallback text"},
        ]
    ) == "\n\n".join(
        [
            "bare string",
            "dict content",
            "part text\npart string",
            "fallback text",
        ]
    )


def test_build_prompt_combines_instructions_and_input() -> None:
    request = ResponsesRequest(model="m", input="question", instructions="system")
    assert build_prompt(request) == "system\n\nquestion"
    assert build_prompt(ResponsesRequest(model="m", input="question")) == "question"
    assert (
        build_prompt(ResponsesRequest(model="m", input=None, instructions="system"))
        == "system"
    )


def test_message_item_shape() -> None:
    item = message_item("msg_1", "hello")
    assert item == {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }


def test_sse_event_data_carries_type() -> None:
    event = sse_event("response.created", {"response": {"id": "resp_1"}})
    assert event.event == "response.created"
    assert event.data == {"type": "response.created", "response": {"id": "resp_1"}}


def test_id_generators_use_responses_prefixes() -> None:
    assert new_response_id().startswith("resp_")
    assert new_message_id().startswith("msg_")
    assert new_response_id() != new_response_id()


def test_record_input_items_list_and_string_forms() -> None:
    listed = ResponsesRequest(
        model="m", input=[{"role": "user", "content": "hi"}, "stray string"]
    )
    assert record_input_items(listed) == [{"role": "user", "content": "hi"}]
    assert record_input_items(ResponsesRequest(model="m", input=None)) == []
    assert record_input_items(ResponsesRequest(model="m", input="hi")) == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
    ]


def test_estimate_usage_counts_words() -> None:
    assert estimate_usage("one two", "three") == {
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    }


class _FakeClaudeAuth:
    """Always-authenticated OAuth resolution; no real credential is touched."""

    def resolve(self) -> AuthResolution:
        return AuthResolution(authenticated=True, method=OAUTH_METHOD)


async def test_claude_stream_stores_response_before_full_drain() -> None:
    # Regression: before the replay seam, the Claude adapter stored the
    # envelope only AFTER response.completed, so a client disconnect mid-stream
    # lost the turn for previous_response_id chaining. Falsifiable: reverting
    # to store-after-drain makes get_response raise here.
    adapter = ClaudeAdapter(
        auth=_FakeClaudeAuth(),
        cli_runner=lambda prompt, model: "Streamed body.",
    )
    request = ResponsesRequest(model="claude-test", input="hi", stream=True)

    gen = adapter.stream_response(request)
    first = await gen.__anext__()
    response_id = first.data["response"]["id"]
    await gen.aclose()

    stored = await adapter.get_response(response_id)
    assert stored.id == response_id


class _UnauthenticatedClaudeAuth:
    """Auth that never resolves; no real credential is touched."""

    def resolve(self) -> AuthResolution:
        return AuthResolution(
            authenticated=False,
            method="none",
            details={"reason": "no_credential"},
        )


async def test_claude_stream_auth_error_surfaces_on_first_drain() -> None:
    # stream_response returns an async generator without executing any body
    # code, so a missing credential must NOT raise at call time; the
    # ClaudeAuthError surfaces when the app drains the first event.
    runs: list[str] = []

    def cli_runner(prompt: str, model: str) -> str:
        runs.append(prompt)
        return "never reached"

    adapter = ClaudeAdapter(auth=_UnauthenticatedClaudeAuth(), cli_runner=cli_runner)
    request = ResponsesRequest(model="claude-test", input="hi", stream=True)

    gen = adapter.stream_response(request)

    with pytest.raises(ClaudeAuthError):
        await gen.__anext__()
    assert runs == []


def test_b4_build_prompt_translates_role_tagged_message_list() -> None:
    """Multi-turn message lists become role-labeled segments for CLI spines.

    The claude and auggie adapters drive single-shot CLIs that take one prompt
    string. The B4 lane translates a multi-turn ``input`` list into labeled
    segments so the model can tell speakers apart. Falsifiable: dropping the
    role labels would collapse a back-and-forth into an undifferentiated wall
    of text and the model would lose the conversation structure.
    """
    request = ResponsesRequest(
        model="m",
        input=[
            {"role": "user", "content": "What is the capital of France?"},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Paris."}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "And of Spain?"}],
            },
        ],
        instructions="You are a geography tutor.",
    )

    prompt = build_prompt(request)

    assert prompt == (
        "You are a geography tutor.\n\n"
        "User: What is the capital of France?\n\n"
        "Assistant: Paris.\n\n"
        "User: And of Spain?"
    )


def test_b4_build_prompt_preserves_untagged_items_verbatim() -> None:
    """Items without a role keep their raw text (no spurious labels)."""
    request = ResponsesRequest(
        model="m",
        input=[
            "preformatted line 1",
            {"text": "preformatted line 2"},
        ],
    )

    assert build_prompt(request) == "preformatted line 1\n\npreformatted line 2"


# --- D1: replay_incremental ----------------------------------------------


def _make_finalize(response_id: str, message_id: str):
    """Build a finalize closure that synthesises a completed envelope.

    Mirrors the deepseek adapter's _finalize_streaming_envelope shape so the
    replay seam tests do not depend on the adapter; the envelope id and the
    primary message id come back exactly the way replay_incremental announced
    them on the wire (regression-pinning the id-threading contract).
    """

    def finalize(
        *,
        full_text: str,
        full_reasoning: str | None,
        usage: dict | None,
        tool_calls: list[dict],
    ) -> ResponseEnvelope:
        envelope = ResponseEnvelope(
            id=response_id,
            model="test-model",
            output=[message_item(message_id, full_text)],
            status="completed",
            usage=usage,
        )
        envelope.raw = {"id": response_id, "object": "response"}
        if full_reasoning:
            envelope.raw["reasoning_content"] = full_reasoning
        return envelope

    return finalize


async def _async_iter(chunks):
    for chunk in chunks:
        yield chunk


async def test_replay_incremental_emits_canonical_envelope_around_per_chunk_deltas() -> (
    None
):
    """Three upstream text chunks become three Responses output_text deltas.

    Pins the canonical envelope sequence around incremental deltas: created
    + in_progress + output_item.added + content_part.added prelude, three
    output_text.delta events (one per chunk), terminal output_text.done +
    content_part.done + output_item.done + response.completed, and the
    completed envelope carries the concatenated text. The store write is
    issued exactly once at finalize-time.
    """
    response_id = "resp_inc_test"
    message_id = "msg_inc_test"
    store = ResponseStore()

    chunks = [
        {
            "text": "He",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": False,
        },
        {
            "text": "llo ",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": False,
        },
        {
            "text": "world",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": False,
        },
        {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            "done": True,
        },
    ]

    events = [
        event
        async for event in replay_incremental(
            _async_iter(chunks),
            response_id=response_id,
            message_id=message_id,
            model="test-model",
            store=store,
            input_items=[],
            finalize=_make_finalize(response_id, message_id),
        )
    ]
    types = [event.event for event in events]
    deltas = [
        event.data["delta"]
        for event in events
        if event.event == "response.output_text.delta"
    ]
    assert deltas == ["He", "llo ", "world"]

    # The canonical envelope sequence (with chunked deltas) is preserved.
    def collapse(seq):
        out = []
        for event_type in seq:
            if (
                event_type == "response.output_text.delta"
                and out
                and out[-1] == "response.output_text.delta"
            ):
                continue
            out.append(event_type)
        return out

    assert collapse(types) == list(CANONICAL_EVENT_SEQUENCE)
    completed = events[-1].data["response"]
    assert completed["output"][0]["content"][0]["text"] == "Hello world"
    assert completed["usage"]["input_tokens"] == 4

    stored = store.get_response(response_id)
    assert stored is not None
    assert stored.output[0]["content"][0]["text"] == "Hello world"


async def test_replay_incremental_store_write_happens_at_finalize_not_before_first_delta() -> (
    None
):
    """ADR 0004: the store stays empty until finalize, then populates exactly once.

    Pins the relaxed store-before-drain invariant. Falsifiable in both
    directions: a future refactor that wrote BEFORE the first delta fails
    the "empty after first delta" assertion; a refactor that wrote only
    AFTER response.completed fails the "non-empty after finalize" assertion.
    """
    response_id = "resp_finalize"
    message_id = "msg_finalize"
    store = ResponseStore()

    chunks = [
        {
            "text": "He",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": False,
        },
        {
            "text": "llo",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": False,
        },
        {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": True,
        },
    ]

    gen = replay_incremental(
        _async_iter(chunks),
        response_id=response_id,
        message_id=message_id,
        model="test-model",
        store=store,
        input_items=[],
        finalize=_make_finalize(response_id, message_id),
    )

    # Drain up to the first response.output_text.delta and assert the store
    # is STILL empty (relaxed invariant).
    saw_delta = False
    while True:
        event = await gen.__anext__()
        if event.event == "response.output_text.delta":
            saw_delta = True
            break
    assert saw_delta
    assert store.get_response(response_id) is None

    # Drain to completion; the store MUST now contain the envelope.
    async for _ in gen:
        pass
    stored = store.get_response(response_id)
    assert stored is not None
    assert stored.id == response_id


async def test_replay_incremental_passes_function_call_items_through_per_item_helpers() -> (
    None
):
    """Function_call items discovered at finalize get canonical per-item events.

    Coordination point with the replay_turn multi-item fix (task #12): when
    finalize returns an envelope with both a message and a function_call
    item, replay_incremental emits the message events for output[0] AND the
    function_call events for output[1] before response.completed.
    """
    response_id = "resp_fc_inc"
    message_id = "msg_fc_inc"
    store = ResponseStore()
    function_call_id = "fc_inc_1"

    def finalize(
        *,
        full_text: str,
        full_reasoning: str | None,
        usage: dict | None,
        tool_calls: list[dict],
    ) -> ResponseEnvelope:
        envelope = ResponseEnvelope(
            id=response_id,
            model="test-model",
            output=[
                message_item(message_id, full_text),
                {
                    "id": function_call_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tool_calls[0]["id"] if tool_calls else "call_x",
                    "name": tool_calls[0]["function"]["name"]
                    if tool_calls
                    else "lookup",
                    "arguments": tool_calls[0]["function"]["arguments"]
                    if tool_calls
                    else "{}",
                },
            ],
            status="completed",
            usage=usage,
        )
        return envelope

    chunks = [
        {
            "text": "Looking it up.",
            "reasoning_text": "",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_xyz",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }
            ],
            "usage": None,
            "done": False,
        },
        {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": True,
        },
    ]

    events = [
        event
        async for event in replay_incremental(
            _async_iter(chunks),
            response_id=response_id,
            message_id=message_id,
            model="test-model",
            store=store,
            input_items=[],
            finalize=finalize,
        )
    ]
    types = [event.event for event in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    args_done = next(
        e for e in events if e.event == "response.function_call_arguments.done"
    )
    assert args_done.data["arguments"] == '{"q":"x"}'
    completed = events[-1].data["response"]
    item_types = [item["type"] for item in completed["output"]]
    assert item_types == ["message", "function_call"]


async def test_replay_incremental_indexless_tool_call_deltas_accumulate_into_one_slot() -> (
    None
):
    """Index-less continuation deltas reuse the last-seen index, not len(buffer).

    Reviewer NIT: the previous fallback ``index = len(tool_calls_buffer)`` could
    silently re-key an already-populated slot when an upstream omits ``index``
    on follow-up deltas (deepseek always sends one, so behavior on the live
    matrix is unchanged). This test pins that two deltas for the SAME call
    accumulate into ONE final tool_call when only the first carries an index.
    Falsifiable: the previous fallback would write both deltas under index=0
    on the first chunk (the slot is empty, len==0) and overwrite it under
    index=0 again on the second chunk (len==1 -> tries to setdefault index=1
    which would yield a fresh slot, splitting the arguments across two calls).
    """
    response_id = "resp_idxless"
    message_id = "msg_idxless"
    store = ResponseStore()

    def finalize(
        *,
        full_text: str,
        full_reasoning: str | None,
        usage: dict | None,
        tool_calls: list[dict],
    ) -> ResponseEnvelope:
        assert len(tool_calls) == 1, (
            "index-less continuation deltas must collapse into ONE tool_call"
        )
        call = tool_calls[0]
        assert call["id"] == "call_idxless_1"
        assert call["function"]["name"] == "lookup"
        assert call["function"]["arguments"] == '{"q":"x"}'
        return ResponseEnvelope(
            id=response_id,
            model="test-model",
            output=[
                message_item(message_id, full_text),
                {
                    "id": "fc_idxless_1",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                },
            ],
            status="completed",
            usage=usage,
        )

    chunks = [
        {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_idxless_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":'},
                }
            ],
            "usage": None,
            "done": False,
        },
        {
            "text": "",
            "reasoning_text": "",
            # Continuation delta with NO index: must reuse last-seen index 0
            # so the arguments append to the same slot instead of opening a
            # second tool_call at index 1.
            "tool_calls": [{"function": {"arguments": '"x"}'}}],
            "usage": None,
            "done": False,
        },
        {
            "text": "",
            "reasoning_text": "",
            "tool_calls": [],
            "usage": None,
            "done": True,
        },
    ]

    events = [
        event
        async for event in replay_incremental(
            _async_iter(chunks),
            response_id=response_id,
            message_id=message_id,
            model="test-model",
            store=store,
            input_items=[],
            finalize=finalize,
        )
    ]
    args_done = next(
        e for e in events if e.event == "response.function_call_arguments.done"
    )
    assert args_done.data["arguments"] == '{"q":"x"}'
    completed = events[-1].data["response"]
    function_call_items = [
        item for item in completed["output"] if item["type"] == "function_call"
    ]
    assert len(function_call_items) == 1
    assert function_call_items[0]["arguments"] == '{"q":"x"}'
