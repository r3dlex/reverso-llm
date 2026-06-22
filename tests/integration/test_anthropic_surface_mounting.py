"""CompositionRoot mounting integration for the Anthropic surface (ADR 0006, G002).

Asserts, through the real composition root over an httpx ASGITransport:
  - POST /claude/v1/messages is served first-party by the claude backend and is
    NOT delegated to the legacy LiteLLM app (claude served, ADR 0008);
  - POST /v1/responses routing is byte-unchanged: it still reaches the first-party
    Responses gateway (FixtureAdapter), never the Anthropic app or legacy app.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.responses_app import build_app
from reverso.proxy.compose import CompositionRoot

RESPONSES_PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]
ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "claude"]


def _build_root() -> tuple[CompositionRoot, list[str]]:
    """Build a CompositionRoot with real-ish gateways and a legacy tripwire."""
    legacy_calls: list[str] = []

    async def _legacy_tripwire(scope: Any, receive: Any, send: Any) -> None:
        legacy_calls.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    gateway = build_app({p: FixtureAdapter(p) for p in RESPONSES_PROVIDERS})
    anthropic = build_anthropic_app({b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS})
    root = CompositionRoot(
        gateway=gateway, anthropic_app=anthropic, legacy_app=_legacy_tripwire
    )
    return root, legacy_calls


def _client(root: CompositionRoot) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=root)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


@pytest.mark.asyncio
async def test_claude_messages_served_first_party_not_legacy() -> None:
    root, legacy_calls = _build_root()
    async with _client(root) as client:
        resp = await client.post(
            "/claude/v1/messages", json={"model": "claude-opus", "messages": []}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert legacy_calls == [], (
        "/claude/v1/messages must be answered by the Anthropic app, never the "
        f"legacy LiteLLM app; observed {legacy_calls!r}"
    )


@pytest.mark.asyncio
async def test_responses_routing_byte_unchanged() -> None:
    """POST /v1/responses still reaches the first-party Responses gateway.

    The Anthropic surface mount is added BEFORE the Responses split but only
    claims /v1/messages and /<profile>/v1/messages, so /<profile>/v1/responses is
    untouched: it returns the gateway's fixture response, not a 404 or legacy.
    """
    root, legacy_calls = _build_root()
    async with _client(root) as client:
        resp = await client.post(
            "/deepseek/v1/responses",
            json={"model": "deepseek-v4-pro", "input": "hi"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert legacy_calls == [], (
        "/deepseek/v1/responses must reach the first-party gateway, not legacy; "
        f"observed {legacy_calls!r}"
    )


@pytest.mark.asyncio
async def test_unknown_messages_model_404_through_root() -> None:
    root, legacy_calls = _build_root()
    async with _client(root) as client:
        resp = await client.post(
            "/v1/messages", json={"model": "totally-unknown", "messages": []}
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"
    assert legacy_calls == []
