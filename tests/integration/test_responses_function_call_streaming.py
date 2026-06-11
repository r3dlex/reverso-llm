"""Streamed deepseek tool calls must surface as canonical function_call events.

C1 evidence: when a translated provider (deepseek) returns a tool call on a
streamed request, the Codex tool loop dies because the function_call only
appears inside response.completed and never gets its own
response.output_item.added / response.function_call_arguments events. This
integration test wires the REAL DeepSeekAdapter into the gateway over a mocked
httpx transport and asserts the canonical per-item function_call events appear
in the SSE body so a streaming Responses client can drive the tool loop.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reverso.protocols.adapters.deepseek import DeepSeekAdapter
from reverso.protocols.responses_app import build_app

BASE_URL = "http://127.0.0.1:64946"
API_KEY_SENTINEL = "sk-DEEPSEEKsentinelKEY-streamed-tools"


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


class _AsyncBytes(httpx.AsyncByteStream):
    """Async byte stream so MockTransport satisfies httpx.AsyncClient.stream()."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data

    async def aclose(self) -> None:
        return None


def _streamed_tool_call_sse() -> bytes:
    """SSE body carrying a streamed function tool_call across two chunks.

    Mirrors deepseek's upstream wire shape for a tool-calling turn: the
    tool_call deltas arrive across multiple chunks (split by index), then a
    chunk with finish_reason="tool_calls" closes the choice, then the
    terminal include_usage chunk delivers token counts, then [DONE].
    """
    chunks: list[dict] = [
        {
            "id": "chatcmpl-tool-streamed",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_streamed_42",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"loc',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool-streamed",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'ation":"Paris"}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool-streamed",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
        {
            "id": "chatcmpl-tool-streamed",
            "model": "deepseek-v4-flash",
            "choices": [],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "total_tokens": 12,
            },
        },
    ]
    parts = [
        b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n" for chunk in chunks
    ]
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def _parse_sse_events(body_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body_text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
    return events


@pytest.mark.asyncio
async def test_streamed_deepseek_function_call_emits_canonical_item_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streamed SSE body must carry the function_call per-item events.

    Asserts: response.output_item.added with type=function_call,
    response.function_call_arguments.delta carrying the arguments payload,
    response.function_call_arguments.done with the final arguments string, and
    response.output_item.done resolving the completed item. The final
    response.completed must still carry the full output list (message +
    function_call) so non-streaming inspection of the envelope is unchanged.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, stream=_AsyncBytes(_streamed_tool_call_sse()))

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    app = build_app({"deepseek": adapter})
    transport = httpx.ASGITransport(app=app)

    payload = {
        "model": "deepseek-v4-flash",
        "input": "What is the weather in Paris?",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            }
        ],
    }

    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        async with client.stream(
            "POST", "/deepseek/v1/responses", json=payload
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body_text = "".join([chunk async for chunk in resp.aiter_text()])

    assert "data: [DONE]" in body_text, "stream must terminate with [DONE]"
    events = _parse_sse_events(body_text)
    types = [event["type"] for event in events]

    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types

    added_events = [
        event for event in events if event["type"] == "response.output_item.added"
    ]
    function_call_added = next(
        event
        for event in added_events
        if event.get("item", {}).get("type") == "function_call"
    )
    assert function_call_added["item"]["call_id"] == "call_streamed_42"
    assert function_call_added["item"]["name"] == "get_weather"
    assert function_call_added["item"]["status"] == "in_progress"
    assert function_call_added["item"]["arguments"] == ""

    args_done = next(
        event
        for event in events
        if event["type"] == "response.function_call_arguments.done"
    )
    assert args_done["arguments"] == '{"location":"Paris"}'

    done_events = [
        event for event in events if event["type"] == "response.output_item.done"
    ]
    function_call_done = next(
        event
        for event in done_events
        if event.get("item", {}).get("type") == "function_call"
    )
    assert function_call_done["item"]["status"] == "completed"
    assert function_call_done["item"]["arguments"] == '{"location":"Paris"}'

    completed = next(event for event in events if event["type"] == "response.completed")
    output_types = [item["type"] for item in completed["response"]["output"]]
    assert "function_call" in output_types, (
        "response.completed must still carry the function_call in output for "
        "non-streaming inspection"
    )


def _final_text_sse() -> bytes:
    """Streamed body for the second-leg request: a short final text + usage.

    The second leg mocks deepseek answering the tool result with a normal
    assistant message and NO new tool_calls. If the adapter still flattened
    the prior turn (legacy bug), the upstream would never see the tool result
    and would re-emit the same function_call; this fixture pins that with the
    tool result in the request body, upstream returns text and the envelope
    closes without a new function_call.
    """
    chunks: list[dict] = [
        {
            "id": "chatcmpl-tool-resolved",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "MARKER seen."},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-tool-resolved",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-tool-resolved",
            "model": "deepseek-v4-flash",
            "choices": [],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
            },
        },
    ]
    parts = [
        b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n" for chunk in chunks
    ]
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


@pytest.mark.asyncio
async def test_second_leg_request_translates_tool_result_into_chat_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second-leg request must hand deepseek the tool result, not re-prompt.

    E2E run3 evidence: when codex resent the prior turn's transcript
    (function_call echo + function_call_output) on the next /responses POST,
    the legacy _build_messages flattened those into one user blob and
    deepseek kept re-issuing the same function_call until timeout. This test
    pins that the outbound chat body carries the assistant tool_calls message
    AND the role=tool result message, and that with the tool result delivered
    the completed envelope has the final text and NO new function_call.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY_SENTINEL)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, stream=_AsyncBytes(_final_text_sse()))

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    app = build_app({"deepseek": adapter})
    transport = httpx.ASGITransport(app=app)

    payload = {
        "model": "deepseek-v4-flash",
        "stream": True,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "read marker.txt"}],
            },
            {
                "type": "function_call",
                "call_id": "call_tool_99",
                "name": "shell",
                "arguments": '{"command":["cat","marker.txt"]}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_tool_99",
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

    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        async with client.stream(
            "POST", "/deepseek/v1/responses", json=payload
        ) as resp:
            assert resp.status_code == 200
            body_text = "".join([chunk async for chunk in resp.aiter_text()])

    sent = captured["body"]["messages"]
    assert sent[0] == {"role": "user", "content": "read marker.txt"}
    assert sent[1]["role"] == "assistant"
    assert sent[1]["tool_calls"] == [
        {
            "id": "call_tool_99",
            "type": "function",
            "function": {
                "name": "shell",
                "arguments": '{"command":["cat","marker.txt"]}',
            },
        }
    ]
    assert sent[2] == {
        "role": "tool",
        "tool_call_id": "call_tool_99",
        "content": "MARKER-DEEPSEEK-7QX1",
    }

    events = _parse_sse_events(body_text)
    completed = next(event for event in events if event["type"] == "response.completed")
    output = completed["response"]["output"]
    output_types = [item["type"] for item in output]
    assert "function_call" not in output_types, (
        "second leg must NOT re-issue a function_call once the tool result "
        "has been delivered upstream"
    )
    message_items = [item for item in output if item["type"] == "message"]
    assert message_items, "second leg must surface assistant text output"
    text_parts = [
        part.get("text", "")
        for item in message_items
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ]
    assert "MARKER seen." in "".join(text_parts)
