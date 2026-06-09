"""Invariant tests for the shared Responses replay seam.

The replay interface is the test surface: the canonical event sequence and the
store-before-drain invariant are asserted ONCE here, against
``reverso.protocols.replay``, instead of once per buffered adapter. The Claude
regression test at the bottom locks in the latent store-after-drain defect that
the seam fixed (Claude previously stored only after response.completed).
"""

from __future__ import annotations

from reverso.protocols.adapter import ResponseEnvelope, ResponsesRequest
from reverso.protocols.adapters.claude import ClaudeAdapter, OAUTH_METHOD
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
    # The item events replay output[0]; response.completed carries everything.
    store = ResponseStore()
    envelope = _envelope("With tools.")
    envelope.output.append(
        {
            "id": new_message_id(),
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

    done = next(e for e in events if e.event == "response.output_item.done")
    assert done.data["item"] == envelope.output[0]
    completed = events[-1].data["response"]
    assert len(completed["output"]) == 2
    assert completed["output"][1]["type"] == "function_call"


def test_flatten_input_handles_string_items_and_parts() -> None:
    assert flatten_input(None) == ""
    assert flatten_input("plain") == "plain"
    assert (
        flatten_input(
            [
                "bare string",
                {"content": "dict content"},
                {"content": [{"text": "part text"}, "part string"]},
                {"text": "fallback text"},
            ]
        )
        == "bare string\ndict content\npart text\npart string\nfallback text"
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
