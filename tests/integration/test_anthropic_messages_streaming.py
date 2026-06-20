"""Streaming /v1/messages integration tests over the FixtureAdapter (G004).

These drive the real AnthropicMessagesApp streaming path (stream=true) through
httpx.ASGITransport, backed by the UNCHANGED FixtureAdapter from the Responses
parity harness (no real provider, process, or credential). They pin, per
Anthropic-surface backend (copilot/deepseek/auggie): a 200 text/event-stream
response with a valid Anthropic event order, that concatenated text_deltas equal
the full output text, and that anthropic-version is echoed.

The FixtureAdapter.stream_response replays the SAME Codex-observed streaming
fixture for every backend, so the Anthropic mapping is exercised identically
across backends without changing the frozen adapters or the parity harness.
"""

from __future__ import annotations

import json
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


def _build_client() -> httpx.AsyncClient:
    adapters = {b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS}
    app = AnthropicMessagesApp(adapters)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _prefix(backend: str) -> str:
    return f"/{backend}/v1/messages"


def _parse_anthropic_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def _collapse_repeated_deltas(types: list[str]) -> list[str]:
    """Collapse consecutive content_block_delta events into one delta phase.

    Adapters legitimately chunk text deltas at different granularities, so the
    canonical Anthropic event order treats any run of deltas as one delta phase.
    """
    collapsed: list[str] = []
    for event_type in types:
        if (
            event_type == "content_block_delta"
            and collapsed
            and collapsed[-1] == "content_block_delta"
        ):
            continue
        collapsed.append(event_type)
    return collapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_streaming_valid_anthropic_event_order(backend: str) -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            assert resp.headers["anthropic-version"] == "2023-06-01"
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_anthropic_sse(text)
    types = [event["type"] for event in events]

    assert _collapse_repeated_deltas(types) == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Exactly one ping, immediately after message_start.
    assert types.count("ping") == 1
    assert types[1] == "ping"
    assert events[0]["message"]["role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ANTHROPIC_BACKENDS)
async def test_streaming_concatenated_text_deltas_equal_full_text(backend: str) -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix(backend),
            json={
                "model": _BACKEND_MODEL[backend],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            text = "".join([chunk async for chunk in resp.aiter_text()])

    events = _parse_anthropic_sse(text)
    concatenated = "".join(
        event["delta"]["text"]
        for event in events
        if event["type"] == "content_block_delta"
        and event["delta"]["type"] == "text_delta"
    )
    # The shared streaming fixture replays "Hi" + " there." across all backends.
    assert concatenated == "Hi there."


@pytest.mark.asyncio
async def test_streaming_anthropic_version_echoed_from_header() -> None:
    async with _build_client() as client:
        async with client.stream(
            "POST",
            _prefix("copilot"),
            headers={"anthropic-version": "2099-01-01"},
            json={
                "model": _BACKEND_MODEL["copilot"],
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["anthropic-version"] == "2099-01-01"
            async for _ in resp.aiter_text():
                pass
