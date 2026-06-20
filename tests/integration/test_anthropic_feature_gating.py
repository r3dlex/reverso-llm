"""Per-backend capability gating integration tests over the FixtureAdapter (G005).

These drive the real AnthropicMessagesApp through httpx.ASGITransport, backed by
the UNCHANGED FixtureAdapter, and pin the ADR 0006 capability ceiling end to end:
image is native on copilot but a 400 invalid_request_error on deepseek/auggie;
extended thinking (param OR content block) is a 400 on every backend;
cache_control on a message / system / tool-definition / nested tool_result block
is a 400 on every backend; tools degrade (auggie text-only, copilot/deepseek emit
tool_use); and a streaming request that requests an unsupported feature is
rejected with a 400 JSON body BEFORE the stream opens (never a 200 event-stream).
Error envelopes are secret-free and name the feature and backend.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import AnthropicMessagesApp

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie"]

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


def _image_block() -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="},
    }


def _assert_invalid_request(body: dict[str, Any], backend: str, feature: str) -> None:
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    message = body["error"]["message"]
    assert feature in message
    assert backend in message
    # Envelope must never carry secret-shaped material.
    serialized = repr(body)
    assert "sk-" not in serialized
    assert "api_key" not in serialized


# --- image ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_on_copilot_translated_200() -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix("copilot"),
            json={
                "model": _BACKEND_MODEL["copilot"],
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": [_image_block()]},
                ],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["deepseek", "auggie"])
async def test_image_rejected_on_deepseek_and_auggie(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": [_image_block()]}],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "input.image")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["deepseek", "auggie"])
async def test_nested_image_in_tool_result_rejected(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [_image_block()],
                            }
                        ],
                    }
                ],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "input.image")


# --- extended thinking ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_thinking_param_rejected_on_all_backends(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [{"role": "user", "content": "think hard"}],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "thinking")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_thinking_content_block_rejected_on_all_backends(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "step"}],
                    }
                ],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "thinking")


# --- cache_control ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_cache_control_on_message_block_rejected(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hi",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "caching.cache_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_cache_control_on_system_block_rejected(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "system": [
                    {
                        "type": "text",
                        "text": "rules",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "caching.cache_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_cache_control_on_tool_definition_rejected(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "input_schema": {"type": "object"},
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "caching.cache_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_cache_control_nested_in_tool_result_rejected(backend: str) -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "result",
                                        "cache_control": {"type": "ephemeral"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        )
    assert resp.status_code == 400
    _assert_invalid_request(resp.json(), backend, "caching.cache_control")


# --- tools degradation (regression guard) -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["copilot", "deepseek"])
async def test_tools_emit_tool_use_on_copilot_and_deepseek(backend: str) -> None:
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
    assert any(b["type"] == "tool_use" for b in body["content"])


@pytest.mark.asyncio
async def test_tools_on_auggie_degrade_to_text_only_200() -> None:
    async with _build_client() as client:
        resp = await client.post(
            _prefix("auggie"),
            json={
                "model": _BACKEND_MODEL["auggie"],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert all(b["type"] != "tool_use" for b in body["content"])
    assert any(b["type"] == "text" and b["text"] for b in body["content"])


# --- streaming gate (reject before the stream opens) ------------------------


@pytest.mark.asyncio
async def test_streaming_cache_control_rejected_with_json_400_not_event_stream() -> (
    None
):
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix("deepseek"),
            json={
                "model": _BACKEND_MODEL["deepseek"],
                "max_tokens": 64,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hi",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            },
        ) as resp:
            assert resp.status_code == 400
            assert "text/event-stream" not in resp.headers.get("content-type", "")
            assert "application/json" in resp.headers["content-type"]
            await resp.aread()
            body = resp.json()
    _assert_invalid_request(body, "deepseek", "caching.cache_control")
