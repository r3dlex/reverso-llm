"""Unit tests for the first-party DeepSeek adapter.

All HTTP traffic is FAKE: the injected client_factory uses an httpx.MockTransport
so no real network call is made. Coverage proves the survival of the params the
legacy LiteLLM stack stripped: ``response_format`` reaches the outbound DeepSeek
body unchanged, and ``reasoning_content`` is carried across a two-turn chain.
Secret non-leakage is asserted with a unique DEEPSEEK_API_KEY sentinel.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
)
from reverso.protocols.adapters.deepseek import (
    DeepSeekAdapter,
    DeepSeekError,
)
from reverso.protocols.store import ResponseStore

API_KEY_SENTINEL = "sk-DEEPSEEKsentinelKEY-do-not-leak-9f8e7d6c"


def _mock_client(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


def _chat_response(text: str = "hello", **extra) -> dict:
    message = {"role": "assistant", "content": text}
    message.update(extra)
    return {
        "id": "chatcmpl-fake",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _adapter(handler, store=None) -> DeepSeekAdapter:
    return DeepSeekAdapter(store=store, client_factory=_mock_client(handler))


class _AsyncBytes(httpx.AsyncByteStream):
    """Async byte stream wrapper so MockTransport works with client.stream()."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data

    async def aclose(self) -> None:
        return None


def _sse_response(chunks: list[dict], status: int = 200) -> httpx.Response:
    """Build an httpx.Response whose body streams chat-style SSE chunks.

    Each item in ``chunks`` becomes a ``data: {...}\\n\\n`` block; the stream
    terminates with ``data: [DONE]\\n\\n`` (mirroring deepseek's OpenAI-compat
    layer). Used by the new streaming tests to mock the upstream stream that
    _call_upstream_stream consumes. The body is wrapped in an AsyncByteStream
    so MockTransport satisfies httpx.AsyncClient.stream()'s contract.
    """
    parts: list[bytes] = []
    for chunk in chunks:
        parts.append(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
    parts.append(b"data: [DONE]\n\n")
    return httpx.Response(status, stream=_AsyncBytes(b"".join(parts)))


def _delta_chunk(
    text: str = "",
    *,
    reasoning: str | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str | None = None,
) -> dict:
    delta: dict[str, Any] = {}
    if text:
        delta["content"] = text
    if reasoning:
        delta["reasoning_content"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-streamed",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage_chunk(usage: dict[str, int]) -> dict:
    """Terminal upstream chunk carrying only the usage block (include_usage)."""
    return {
        "id": "chatcmpl-streamed",
        "model": "deepseek-chat",
        "choices": [],
        "usage": usage,
    }


def test_adapter_satisfies_protocol(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    adapter = _adapter(lambda r: httpx.Response(200, json=_chat_response()))
    assert isinstance(adapter, ProviderAdapter)


async def test_create_response_maps_text_and_stores(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_chat_response(text="hi there"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": [{"role": "user", "content": "hi"}]}
    )

    envelope = await adapter.create_response(request)

    assert isinstance(envelope, ResponseEnvelope)
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == f"Bearer {API_KEY_SENTINEL}"
    text = envelope.output[0]["content"][0]["text"]
    assert text == "hi there"

    stored = await adapter.get_response(envelope.id)
    assert stored.id == envelope.id
    items = await adapter.list_input_items(envelope.id)
    assert isinstance(items, InputItemList)
    assert items.data == [{"role": "user", "content": "hi"}]


async def test_stream_response_yields_completed_sequence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk("streamed"),
                _delta_chunk(finish_reason="stop"),
                _usage_chunk(
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                ),
            ]
        )

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    assert events
    assert events[0].event == "response.created"
    assert events[-1].event == "response.completed"
    deltas = [
        e.data["delta"] for e in events if e.event == "response.output_text.delta"
    ]
    assert "streamed" in deltas
    completed = events[-1].data["response"]
    assert completed["status"] == "completed"


async def test_list_models_returns_live_upstream_listing(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "deepseek-v4-flash",
                        "object": "model",
                        "owned_by": "deepseek",
                    },
                    {
                        "id": "deepseek-v4-pro",
                        "object": "model",
                        "owned_by": "deepseek",
                    },
                ],
            },
        )

    adapter = _adapter(handler)

    models = await adapter.list_models()

    assert isinstance(models, ModelList)
    assert models.object == "list"
    ids = [m["id"] for m in models.data]
    assert ids == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert all(m["owned_by"] == "deepseek" for m in models.data)
    assert all(m["object"] == "model" for m in models.data)


async def test_list_models_falls_back_to_static_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream must not be called without a key")

    adapter = _adapter(handler)

    models = await adapter.list_models()

    ids = [m["id"] for m in models.data]
    assert ids == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-reasoner",
        "deepseek-chat",
    ]


async def test_response_format_survives_to_upstream_body(monkeypatch):
    # Falsifiable JSON-mode survival: the legacy stack stripped response_format;
    # the first-party adapter MUST forward it unchanged.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "give me json",
            "response_format": {"type": "json_object"},
        }
    )

    await adapter.create_response(request)

    assert captured["body"]["response_format"] == {"type": "json_object"}


async def test_inbound_messages_in_extra_cannot_clobber_translated_body(monkeypatch):
    # A stray ``messages`` key in the inbound body lands in request.extra (it is
    # not a typed Responses field). The adapter owns ``messages`` from its
    # input translation, so the carry-through must NOT let the inbound value
    # overwrite it. Falsifiable: forwarding ``messages`` from extra would put
    # "INJECTED" on the wire and drop the real prompt.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "real prompt",
            "messages": [{"role": "user", "content": "INJECTED"}],
        }
    )

    await adapter.create_response(request)

    sent = captured["body"]["messages"]
    assert sent == [{"role": "user", "content": "real prompt"}]
    assert all("INJECTED" not in str(m.get("content", "")) for m in sent)


async def test_stream_store_write_happens_at_finalize_not_before_first_delta(
    monkeypatch,
):
    """Pins the ADR 0004 store-before-drain RELAXATION for the streamed path.

    The buffered path writes BEFORE the first yield (replay_turn invariant);
    the incremental path writes at finalize-time, AFTER the last delta and
    BEFORE response.completed. Falsifiable: a future refactor that
    re-tightens the invariant by storing before the first event would make
    the assertion that the store is empty after the first delta fail; a
    refactor that relaxed it further (storing only after response.completed)
    would make get_response raise after draining the full stream.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk("streamed"),
                _delta_chunk(finish_reason="stop"),
                _usage_chunk(
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                ),
            ]
        )

    store = ResponseStore()
    adapter = _adapter(handler, store=store)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    gen = adapter.stream_response(request)
    first = await gen.__anext__()
    response_id = first.data["response"]["id"]
    # Drain enough events to confirm at least one delta landed without
    # triggering the finalize step.
    saw_delta = False
    while True:
        event = await gen.__anext__()
        if event.event == "response.output_text.delta":
            saw_delta = True
            break
    assert saw_delta
    # ADR 0004 relaxed invariant: the store is EMPTY between the first delta
    # and the finalize step. A future refactor that re-tightens this must
    # update both ADR 0004 and this assertion.
    assert store.get_response(response_id) is None

    # Drain to completion; finalize must populate the store.
    async for _ in gen:
        pass
    assert store.get_response(response_id) is not None


@pytest.mark.parametrize(
    "profile_model, expected_upstream",
    [
        ("gpt-5.5", "deepseek-v4-pro"),
        ("gpt-5.4", "deepseek-v4-pro"),
        ("gpt-5.4-mini", "deepseek-v4-flash"),
        ("gpt-5.3-codex-spark", "deepseek-v4-flash"),
        ("gpt-4.1", "deepseek-v4-flash"),
        ("deepseek-chat", "deepseek-chat"),
        ("deepseek-v4-pro", "deepseek-v4-pro"),
    ],
)
async def test_gpt_profile_models_resolve_to_deepseek_ids(
    monkeypatch, profile_model, expected_upstream
):
    # The first-party /deepseek path bypasses ProfileRoutingMiddleware, so the
    # adapter itself must resolve GPT-level Codex profile names to concrete
    # DeepSeek model ids (test-spec item: "model aliases are resolved for the
    # DeepSeek provider prefix"). Falsifiable: forwarding request.model verbatim
    # would put "gpt-5.5" on the wire and fail. Real DeepSeek ids pass through.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload({"model": profile_model, "input": "hi"})

    await adapter.create_response(request)

    assert captured["body"]["model"] == expected_upstream


async def test_non_streaming_payload_is_responses_shaped_not_chat(monkeypatch):
    # DeepSeek is not Responses-native, so the client-facing body must be a
    # Responses object (object == "response" with an output array), never the
    # upstream chat-completions shape. Falsifiable: serving envelope.raw as the
    # chat body would surface choices/no-output here and fail.
    from reverso.protocols.responses_app import _envelope_to_payload

    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    adapter = _adapter(lambda r: httpx.Response(200, json=_chat_response(text="hi")))
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    envelope = await adapter.create_response(request)
    payload = _envelope_to_payload(envelope)

    assert payload["object"] == "response"
    assert "choices" not in payload
    assert isinstance(payload["output"], list) and payload["output"]
    assert payload["output"][0]["content"][0]["text"] == "hi"


async def test_envelope_echoes_requested_model_alias(monkeypatch):
    # A caller that sends a GPT-level profile alias must see that alias echoed
    # back, not the resolved DeepSeek id or the upstream-reported model. Matches
    # the Auggie adapter and avoids leaking the resolution into profile chaining.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    adapter = _adapter(lambda r: httpx.Response(200, json=_chat_response()))
    request = ResponsesRequest.from_payload({"model": "gpt-5.5", "input": "hi"})

    envelope = await adapter.create_response(request)

    assert envelope.model == "gpt-5.5"
    assert envelope.raw["model"] == "gpt-5.5"


async def test_reasoning_content_carries_across_two_turns(monkeypatch):
    # Falsifiable thinking-mode survival: turn 1 returns reasoning_content; turn 2
    # chains via previous_response_id and MUST re-inject it into the outbound
    # messages.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    store = ResponseStore()
    reasoning = "step 1: consider X; step 2: conclude Y"
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        if len(bodies) == 1:
            return httpx.Response(
                200, json=_chat_response(text="turn1", reasoning_content=reasoning)
            )
        return httpx.Response(200, json=_chat_response(text="turn2"))

    adapter = _adapter(handler, store=store)

    turn1 = await adapter.create_response(
        ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "first"})
    )
    assert turn1.raw["reasoning_content"] == reasoning

    await adapter.create_response(
        ResponsesRequest.from_payload(
            {
                "model": "deepseek-chat",
                "input": "second",
                "previous_response_id": turn1.id,
            }
        )
    )

    turn2_messages = bodies[1]["messages"]
    carried = [m for m in turn2_messages if m.get("reasoning_content") == reasoning]
    assert carried, "prior reasoning_content was not carried into turn-2 messages"


async def test_tool_calls_surfaced_not_executed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    calls = {"n": 0}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_chat_response(text="", tool_calls=tool_calls))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "weather?"}
    )

    envelope = await adapter.create_response(request)

    function_calls = [o for o in envelope.output if o["type"] == "function_call"]
    assert function_calls
    assert function_calls[0]["name"] == "get_weather"
    assert function_calls[0]["arguments"] == '{"city":"Paris"}'
    # Exactly one upstream call: surfaced, never executed.
    assert calls["n"] == 1


async def test_responses_format_tools_converted_to_chat_format(monkeypatch):
    """Codex sends flat Responses tools; DeepSeek needs nested ``function``.

    Without conversion DeepSeek returns 400 (missing field `function`), which
    broke every codex -p deepseek run (codex always declares its tools).
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "run a command",
                    "parameters": {"type": "object"},
                    "strict": False,
                },
                {
                    "type": "function",
                    "function": {"name": "already_chat", "parameters": {}},
                },
                {"type": "web_search"},
            ],
            "tool_choice": {"type": "function", "name": "shell"},
        }
    )

    await adapter.create_response(request)

    sent_tools = bodies[0]["tools"]
    assert sent_tools == [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "run a command",
                "parameters": {"type": "object"},
            },
        },
        {"type": "function", "function": {"name": "already_chat", "parameters": {}}},
    ]
    assert bodies[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "shell"},
    }


async def test_user_field_forwarded_via_extra_carry_through(monkeypatch):
    """D2: a request `user` field reaches the upstream chat body verbatim.

    `user` is not a typed field on ResponsesRequest, so `from_payload` lands
    it in `extra`. `_build_body`'s extra-loop carries non-denied keys through
    unchanged, so a Responses request with `user="abc-123"` produces an
    outbound DeepSeek chat-completions body that contains `"user": "abc-123"`.
    The D2 architect probe confirmed the deepseek upstream accepts the field
    and returns a normal completion, so the gate now classifies the cell as
    `translated` and the field reaches upstream untouched.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(text="ok"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "user": "abc-123"}
    )

    await adapter.create_response(request)

    assert bodies[0]["user"] == "abc-123"


async def test_secret_never_leaks_on_success(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(text="ok"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    with caplog.at_level(logging.DEBUG):
        envelope = await adapter.create_response(request)

    assert API_KEY_SENTINEL not in caplog.text
    assert API_KEY_SENTINEL not in json.dumps(envelope.raw)
    assert API_KEY_SENTINEL not in json.dumps(envelope.output)


async def test_secret_never_leaks_on_error(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DeepSeekError) as excinfo:
            await adapter.create_response(request)

    assert API_KEY_SENTINEL not in caplog.text
    assert API_KEY_SENTINEL not in str(excinfo.value)


async def test_missing_api_key_raises_bounded_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    adapter = _adapter(lambda r: httpx.Response(200, json=_chat_response()))
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    with pytest.raises(DeepSeekError) as excinfo:
        await adapter.create_response(request)

    assert API_KEY_SENTINEL not in str(excinfo.value)


async def test_non_2xx_upstream_raises_bounded_error(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    with pytest.raises(DeepSeekError) as excinfo:
        await adapter.create_response(request)

    assert "429" in str(excinfo.value)
    assert API_KEY_SENTINEL not in str(excinfo.value)


async def test_b4_text_format_json_schema_translates_to_response_format(monkeypatch):
    """Responses text.format.json_schema becomes chat response_format.json_schema.

    The B4 lane maps Responses-shape JSON-mode declarations to DeepSeek's
    chat-completions response_format. Falsifiable: forwarding text verbatim
    would put a chat-API-unknown ``text`` field on the wire (and skip the
    response_format the user actually wants).
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "structured please",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
        }
    )

    await adapter.create_response(request)

    assert "text" not in captured["body"]
    assert captured["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "schema": {"type": "object"},
            "strict": True,
        },
    }


async def test_b4_text_format_json_object_translates_to_response_format(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "json please",
            "text": {"format": {"type": "json_object"}},
        }
    )

    await adapter.create_response(request)

    assert "text" not in captured["body"]
    assert captured["body"]["response_format"] == {"type": "json_object"}


async def test_b4_max_output_tokens_translates_to_max_tokens(monkeypatch):
    """Responses max_output_tokens becomes chat max_tokens for DeepSeek.

    DeepSeek's chat-completions surface uses the OpenAI chat name (max_tokens),
    not the Responses name (max_output_tokens). Falsifiable: forwarding the
    raw key would make DeepSeek ignore the budget and the wrong/missing field
    name would silently no-op.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "hi",
            "max_output_tokens": 256,
        }
    )

    await adapter.create_response(request)

    assert "max_output_tokens" not in captured["body"]
    assert captured["body"]["max_tokens"] == 256


async def test_b4_sampling_and_parallel_tool_calls_pass_through(monkeypatch):
    """Sampling and parallel_tool_calls already use chat names and pass through.

    These extras share names between Responses and chat-completions surfaces,
    so the carry-through forwards them unchanged. Falsifiable: a future
    over-eager translation step that filtered these would drop user-controlled
    sampling, masking changes in model behavior.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "hi",
            "temperature": 0.2,
            "top_p": 0.9,
            "parallel_tool_calls": True,
        }
    )

    await adapter.create_response(request)

    body = captured["body"]
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9
    assert body["parallel_tool_calls"] is True


async def test_b4_explicit_response_format_wins_over_text_format(monkeypatch):
    """An explicit response_format in extra is preserved over a translated text.format.

    A caller that already speaks chat-shape should be able to override the
    derived shape. Falsifiable: blindly overwriting response_format with the
    translation would lose user-intended overrides; falling back to text-only
    when both are present would defeat the explicit response_format.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "hi",
            "response_format": {"type": "json_object"},
            "text": {"format": {"type": "text"}},
        }
    )

    await adapter.create_response(request)

    assert captured["body"]["response_format"] == {"type": "json_object"}


# --- D1: deepseek incremental streaming ----------------------------------


async def test_stream_response_consumes_upstream_streaming_and_emits_incremental_deltas(
    monkeypatch,
):
    """Three upstream chat chunks become three Responses output_text deltas.

    Pins the D1 contract: the adapter parses upstream SSE lines into chunk
    dicts, hands them to replay_incremental, and the gateway client sees one
    response.output_text.delta per upstream content chunk. The outbound body
    must declare stream=true AND stream_options.include_usage=true.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured_body: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.append(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            [
                _delta_chunk("He"),
                _delta_chunk("llo "),
                _delta_chunk("world"),
                _delta_chunk(finish_reason="stop"),
                _usage_chunk(
                    {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
                ),
            ]
        )

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]
    deltas = [
        e.data["delta"] for e in events if e.event == "response.output_text.delta"
    ]
    assert deltas == ["He", "llo ", "world"]

    completed = next(e for e in events if e.event == "response.completed")
    output_text = completed.data["response"]["output"][0]["content"][0]["text"]
    assert output_text == "Hello world"

    assert captured_body[0]["stream"] is True
    assert captured_body[0]["stream_options"] == {"include_usage": True}


async def test_stream_response_terminal_chunk_usage_lands_on_completed_envelope(
    monkeypatch,
):
    """Usage from the terminal upstream chunk reaches response.completed.

    Pins Architect REVISE point 2: without stream_options.include_usage the
    deepseek OpenAI-compat layer leaves usage null. The streaming adapter
    forces the option on (in _build_body) and the chunk parser surfaces the
    terminal usage block onto the completed envelope.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk("He"),
                _delta_chunk("llo"),
                _delta_chunk(finish_reason="stop"),
                _usage_chunk(
                    {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
                ),
            ]
        )

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    completed_response = events[-1].data["response"]
    usage = completed_response["usage"]
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 10


async def test_stream_response_preserves_reasoning_content_on_stream(monkeypatch):
    """Reasoning chunks accumulate into envelope.raw and chain via previous_response_id.

    DeepSeek's streaming reasoner emits delta.reasoning_content alongside
    delta.content. The chunk parser accumulates reasoning text without
    emitting user-visible output_text deltas, then the finalize step lands it
    on envelope.raw so _prior_turn can re-inject the thinking on the next
    chained turn (gating DeepSeek thinking mode).
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(reasoning="think "),
                _delta_chunk("Hello "),
                _delta_chunk(reasoning="some more"),
                _delta_chunk("world"),
                _delta_chunk(finish_reason="stop"),
                _usage_chunk(
                    {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
                ),
            ]
        )

    store = ResponseStore()
    adapter = _adapter(handler, store=store)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]
    response_id = events[0].data["response"]["id"]
    stored = store.get_response(response_id)
    assert stored is not None
    assert stored.raw.get("reasoning_content") == "think some more"

    # previous_response_id chaining surfaces the reasoning block back upstream.
    def passthrough_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        passthrough_handler.captured = body  # type: ignore[attr-defined]
        return httpx.Response(200, json=_chat_response(text="ok"))

    adapter_chain = DeepSeekAdapter(
        store=store, client_factory=_mock_client(passthrough_handler)
    )
    chain_request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "and then?",
            "previous_response_id": response_id,
        }
    )
    await adapter_chain.create_response(chain_request)
    chained_messages = passthrough_handler.captured["messages"]  # type: ignore[attr-defined]
    prior_assistant = next(m for m in chained_messages if m["role"] == "assistant")
    assert prior_assistant.get("reasoning_content") == "think some more"


async def test_stream_response_401_before_first_delta_returns_structured_error(
    monkeypatch,
):
    """401 at response headers raises DeepSeekError BEFORE any event is yielded.

    Pre-emission branch (Architect REVISE point 4): if upstream returns a
    non-2xx status before any SSE byte ships, the adapter must raise rather
    than emit a partial stream. The gateway's responses_app then renders a
    structured 502 with zero SSE bytes on the wire.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=_AsyncBytes(b'{"error":"unauthorized"}'))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    gen = adapter.stream_response(request)
    with pytest.raises(DeepSeekError) as excinfo:
        await gen.__anext__()
    assert "401" in str(excinfo.value)


async def test_stream_response_401_race_after_first_delta_surfaces_response_failed(
    monkeypatch,
):
    """A 401 received DURING body iteration propagates after the first delta.

    Post-emission branch (Architect REVISE point 4): once a content chunk has
    been emitted, an upstream failure cannot silently fall back. The adapter
    yields the delta and then re-raises so responses_app._stream translates
    the exception into response.failed + [DONE] (pinned by the integration
    counterpart). At the adapter level we only need to assert: one delta is
    yielded and then a raised exception breaks the generator.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    class _BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"x","model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
            raise httpx.RemoteProtocolError("connection broke mid-stream")

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BrokenStream())

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    gen = adapter.stream_response(request)
    collected = []
    with pytest.raises(DeepSeekError):
        async for event in gen:
            collected.append(event)
    deltas = [
        event.data["delta"]
        for event in collected
        if event.event == "response.output_text.delta"
    ]
    assert deltas == ["Hel"]


async def test_stream_response_surfaces_streamed_tool_call_via_replay_incremental(
    monkeypatch,
):
    """Streamed tool_call deltas accumulate and surface via per-item events.

    Coordination point with task #12: function_call items discovered at
    finalize go through the same per-item helpers replay_turn uses, so the
    streamed contract matches the buffered one. Pins that upstream tool_call
    deltas (split across chunks by index) collapse into one function_call
    output item that gets canonical output_item.added /
    function_call_arguments.delta / .done / output_item.done events.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_stream_99",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"loc'},
                        }
                    ]
                ),
                _delta_chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "function": {"arguments": 'ation":"Paris"}'},
                        }
                    ]
                ),
                _delta_chunk(finish_reason="tool_calls"),
                _usage_chunk(
                    {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}
                ),
            ]
        )

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": "weather please",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    events = [event async for event in adapter.stream_response(request)]
    types = [event.event for event in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    args_done = next(
        event
        for event in events
        if event.event == "response.function_call_arguments.done"
    )
    assert args_done.data["arguments"] == '{"location":"Paris"}'
    completed = events[-1].data["response"]
    function_call_items = [
        item for item in completed["output"] if item["type"] == "function_call"
    ]
    assert len(function_call_items) == 1
    assert function_call_items[0]["call_id"] == "call_stream_99"
    assert function_call_items[0]["name"] == "get_weather"
    assert function_call_items[0]["arguments"] == '{"location":"Paris"}'


async def test_tool_loop_second_leg_input_walks_into_chat_tool_messages(monkeypatch):
    """Codex's second-leg input must surface as assistant tool_calls + role=tool.

    E2E run3 evidence: the deepseek workspace cell received codex's tool result
    in the Responses input list as a function_call echo plus a
    function_call_output item. The legacy _build_messages flattened those into
    one user blob, so deepseek never saw the assistant tool_calls message or
    the role=tool result and kept re-issuing the same call indefinitely.
    Falsifiable: a regression that reverts to flatten_input would emit a
    single user message and BOTH the assistant tool_calls and the role=tool
    asserts below would miss.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response("done"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "read marker.txt"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_tool_42",
                    "name": "shell",
                    "arguments": '{"command":["cat","marker.txt"]}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_tool_42",
                    "output": "MARKER-DEEPSEEK-7QX1",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    await adapter.create_response(request)

    messages = captured["body"]["messages"]
    assert messages[0] == {"role": "user", "content": "read marker.txt"}
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == ""
    assert assistant["tool_calls"] == [
        {
            "id": "call_tool_42",
            "type": "function",
            "function": {
                "name": "shell",
                "arguments": '{"command":["cat","marker.txt"]}',
            },
        }
    ]
    tool_msg = messages[2]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": "call_tool_42",
        "content": "MARKER-DEEPSEEK-7QX1",
    }


async def test_function_call_output_dict_is_json_dumps_coerced(monkeypatch):
    """Structured tool outputs (dict/list) round-trip as a JSON string.

    Reviewer NIT: defaulting non-string outputs to "" silently drops
    structured tool payloads. Coerce dict/list outputs via json.dumps so
    chat tools returning structured results survive to upstream. None and
    non-serialisable values still fall back to "" (conservative default).
    Falsifiable: a regression that keeps the empty-string fallback would
    leave content == "" and the assertion below would miss the JSON payload.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response("done"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "use the tool"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_struct_7",
                    "name": "lookup",
                    "arguments": '{"q":"x"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_struct_7",
                    "output": {"status": "ok", "rows": [1, 2, 3]},
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    await adapter.create_response(request)

    tool_msg = captured["body"]["messages"][2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_struct_7"
    # The content is a JSON string the chat API can consume; round-trip it to
    # assert structure equality rather than depending on dict key ordering.
    assert json.loads(tool_msg["content"]) == {"status": "ok", "rows": [1, 2, 3]}


async def test_text_only_input_list_still_flattens_to_single_user_message(monkeypatch):
    """Regression pin: a text-only message list keeps the flatten_input shape.

    The tool-loop branch ONLY activates when the input list carries a
    function_call(_output) item; an input list of plain message items must
    keep producing the single flattened user message the existing fixtures
    pin (no behaviour change for the message-only path).
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_chat_response("ok"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {
            "model": "deepseek-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "world"}],
                },
            ],
        }
    )

    await adapter.create_response(request)

    messages = captured["body"]["messages"]
    assert messages == [{"role": "user", "content": "User: hello\n\nUser: world"}]
