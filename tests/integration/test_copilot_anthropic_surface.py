"""Copilot chat-completions backend on the Anthropic Messages surface (ADR 0011).

These tests drive ``AnthropicMessagesApp`` with a real ``CopilotAdapter`` whose
auth is faked and whose HTTP backend is an ``httpx.MockTransport`` (no real
GitHub Copilot credential, endpoint, or network is touched). They pin the
0011 contract for the chat path:

- routing: POST /v1/messages with ``copilot/claude-sonnet-4`` resolves to the
  copilot backend, the adapter strips the prefix, and the bare ``claude-sonnet-4``
  id takes the translated /chat/completions path (non-streaming + streaming);
- the upstream /chat/completions body carries translated ``messages`` (never the
  Responses ``input`` shape) and the streamed path sets
  ``stream_options.include_usage``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from reverso.protocols.adapters.copilot import CopilotAdapter
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.auth import AuthResolution

FAKE_BEARER_TOKEN = "tid=FAKEbearerINTEGRATIONtoken1234567890"


class _FakeAuth:
    def resolve(self) -> AuthResolution:  # pragma: no cover - not exercised here
        return AuthResolution(authenticated=True, method="copilot_oauth")

    async def bearer_token(self) -> str:
        return FAKE_BEARER_TOKEN


def _chat_completion_body(text: str = "hello from claude") -> dict[str, Any]:
    return {
        "id": "chatcmpl-int-1",
        "model": "claude-sonnet-4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


_STREAM_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"content":"hello "}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":"there"}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":4,'
    b'"completion_tokens":2,"total_tokens":6}}\n\n'
    b"data: [DONE]\n\n"
)


def _build_client(handler) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        if request.content:
            captured["body"] = json.loads(request.content.decode("utf-8"))
        return handler(request)

    transport = httpx.MockTransport(wrapped)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    adapter = CopilotAdapter(auth=_FakeAuth(), client_factory=factory)
    app = build_anthropic_app({"copilot": adapter})
    asgi = httpx.ASGITransport(app=app)
    return (
        httpx.AsyncClient(transport=asgi, base_url="http://127.0.0.1:64947"),
        captured,
    )


def _messages_body(model: str, text: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": text}],
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_copilot_claude_nonstreaming_routes_to_chat_completions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_completion_body())

    client, captured = _build_client(handler)
    async with client:
        resp = await client.post(
            "/v1/messages", json=_messages_body("copilot/claude-sonnet-4", "Say hi.")
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    text = "".join(b["text"] for b in body["content"] if b["type"] == "text")
    assert text == "hello from claude"
    # The upstream call hit /chat/completions with translated messages, and the
    # bare id (prefix stripped by routing) was sent, not copilot/claude-sonnet-4.
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "claude-sonnet-4"
    # The Anthropic surface translates the user turn into a role-tagged
    # Responses input item; flatten_input role-labels it (matching deepseek),
    # so the upstream chat message carries the labeled user text.
    messages = captured["body"]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert "Say hi." in messages[0]["content"]
    assert "input" not in captured["body"]


@pytest.mark.asyncio
async def test_copilot_claude_streaming_routes_to_chat_completions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_STREAM_SSE, headers={"content-type": "text/event-stream"}
        )

    client, captured = _build_client(handler)
    async with client:
        async with client.stream(
            "POST",
            "/v1/messages",
            json=_messages_body("copilot/claude-sonnet-4", "Say hi.", stream=True),
        ) as resp:
            assert resp.status_code == 200, resp.text
            assert "text/event-stream" in resp.headers["content-type"]
            text = "".join([chunk async for chunk in resp.aiter_text()])

    types = [event["type"] for event in _parse_sse(text)]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert types.count("ping") == 1
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events
