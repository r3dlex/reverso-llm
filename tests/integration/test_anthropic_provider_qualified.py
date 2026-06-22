"""Integration tests for provider-qualified model ids on the Anthropic surface.

A client may put the provider up front (``provider/model``) to disambiguate when
two backends would otherwise share a model name. These tests drive the real
AnthropicMessagesApp and pin two guarantees end to end:
  - a qualified id routes to the backend named by its prefix (not a 404), and
  - the downstream adapter receives the BARE upstream model id (prefix stripped),
    so the provider prefix never leaks into the provider call.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.adapter import ResponsesRequest

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "codex"]


class _RecordingAdapter(FixtureAdapter):
    """FixtureAdapter that records the model id of the last request it served."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        self.seen_models: list[str] = []

    async def create_response(self, request: ResponsesRequest) -> Any:
        self.seen_models.append(request.model)
        return await super().create_response(request)


def _client_with(adapters: dict[str, _RecordingAdapter]) -> httpx.AsyncClient:
    app = build_anthropic_app(adapters)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
    }


@pytest.mark.asyncio
async def test_qualified_id_routes_and_adapter_sees_bare_model() -> None:
    adapters = {b: _RecordingAdapter(b) for b in ANTHROPIC_BACKENDS}
    async with _client_with(adapters) as client:
        resp = await client.post("/v1/messages", json=_body("deepseek/deepseek-v4-pro"))
    assert resp.status_code == 200
    # Only the deepseek backend was dispatched, and it saw the bare model id.
    assert adapters["deepseek"].seen_models == ["deepseek-v4-pro"]
    assert adapters["codex"].seen_models == []


@pytest.mark.asyncio
async def test_bare_id_still_routes_unchanged() -> None:
    adapters = {b: _RecordingAdapter(b) for b in ANTHROPIC_BACKENDS}
    async with _client_with(adapters) as client:
        resp = await client.post("/v1/messages", json=_body("deepseek-v4-pro"))
    assert resp.status_code == 200
    assert adapters["deepseek"].seen_models == ["deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_qualified_mismatch_is_404() -> None:
    adapters = {b: _RecordingAdapter(b) for b in ANTHROPIC_BACKENDS}
    async with _client_with(adapters) as client:
        # prefix says deepseek but gpt-5.5 is a codex model: conflict, fail closed.
        resp = await client.post("/v1/messages", json=_body("deepseek/gpt-5.5"))
    assert resp.status_code == 404
    assert all(not a.seen_models for a in adapters.values())


@pytest.mark.asyncio
async def test_claude_qualified_is_404() -> None:
    adapters = {b: _RecordingAdapter(b) for b in ANTHROPIC_BACKENDS}
    async with _client_with(adapters) as client:
        resp = await client.post("/v1/messages", json=_body("claude/claude-opus-4-8"))
    assert resp.status_code == 404
    assert all(not a.seen_models for a in adapters.values())
