"""Non-streaming /v1/messages integration tests over the FixtureAdapter (G003).

These drive the real AnthropicMessagesApp dispatch + the G003 translation core
through httpx.ASGITransport, backed by the UNCHANGED FixtureAdapter from the
Responses parity harness (no real provider, process, or credential). They pin:
a valid non-streaming Anthropic message body per Anthropic-surface backend
(copilot/deepseek/auggie); tool_use OUTPUT on copilot and deepseek; auggie
text-only; stream=true -> 501 TODO(G004); and a backend failure -> 502 with a
secret-free Anthropic error envelope.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import AnthropicMessagesApp

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie"]

# Models that resolve to each Anthropic-surface backend through surface_registry.
_BACKEND_MODEL = {
    "copilot": "copilot-gpt-5",
    "deepseek": "deepseek-v4-pro",
    "auggie": "prism-a",
}


def _build_client(adapters: dict[str, Any] | None = None) -> httpx.AsyncClient:
    if adapters is None:
        adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    app = AnthropicMessagesApp(adapters)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(backend: str) -> str:
    return f"/{backend}/v1/messages"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_nonstreaming_text_turn_valid_anthropic_body(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["id"], str) and body["id"].startswith("msg_")
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert isinstance(body["content"], list)
    assert body["stop_reason"] in {
        "end_turn",
        "tool_use",
        "max_tokens",
        "stop_sequence",
    }
    assert "input_tokens" in body["usage"]
    assert "output_tokens" in body["usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["copilot", "deepseek"])
async def test_tool_use_output_on_copilot_and_deepseek(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    tool_use = next(b for b in body["content"] if b["type"] == "tool_use")
    assert isinstance(tool_use["id"], str) and tool_use["id"]
    assert tool_use["name"] == "get_weather"
    assert isinstance(tool_use["input"], dict)
    assert body["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_auggie_text_only() -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix("auggie"),
            json={
                "model": _BACKEND_MODEL["auggie"],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "input_schema": {"type": "object"},
                    }
                ],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    # auggie has a text-only ceiling: the FixtureAdapter returns a text message
    # and no function_call, so the translated body carries only text blocks.
    assert all(b["type"] != "tool_use" for b in body["content"])
    assert any(b["type"] == "text" and b["text"] for b in body["content"])
    assert body["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_stream_true_returns_501_not_implemented() -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix("deepseek"),
            json={
                "model": _BACKEND_MODEL["deepseek"],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 501
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_implemented"


class _FailingAdapter:
    """Adapter whose create_response raises, to exercise the 502 path.

    The exception message embeds a fake secret so the test can assert it never
    leaks into the Anthropic error envelope.
    """

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        raise RuntimeError("api_key=sk-SECRET-do-not-leak upstream 500")

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(event="response.completed", data={"type": "response.completed"})

    async def list_models(self) -> ModelList:
        return ModelList(data=[])

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="x")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


@pytest.mark.asyncio
async def test_backend_failure_returns_502_secret_free() -> None:
    adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    adapters["deepseek"] = _FailingAdapter()
    async with _build_client(adapters) as client:
        resp = await client.post(
            _prefix("deepseek"),
            json={
                "model": _BACKEND_MODEL["deepseek"],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 502
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    serialized = repr(body)
    assert "sk-SECRET" not in serialized
    assert "api_key" not in serialized
    assert "RuntimeError" in body["error"]["message"]
