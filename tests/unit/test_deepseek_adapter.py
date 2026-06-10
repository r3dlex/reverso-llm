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
        return httpx.Response(200, json=_chat_response(text="streamed"))

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
                    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
                    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
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


async def test_stream_stores_response_before_full_drain(monkeypatch):
    # A client that disconnects mid-stream must still leave the response stored
    # for later previous_response_id chaining. Falsifiable: storing only after
    # the final response.completed event (the prior ordering) would make
    # get_response raise here because the stream is closed after one event.
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(text="streamed"))

    adapter = _adapter(handler)
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    gen = adapter.stream_response(request)
    first = await gen.__anext__()
    response_id = first.data["response"]["id"]
    await gen.aclose()

    stored = await adapter.get_response(response_id)
    assert stored.id == response_id


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
