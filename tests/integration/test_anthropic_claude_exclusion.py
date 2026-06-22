"""Conformance suite: claude IS served on the Anthropic surface (ADR 0008).

ADR 0008 supersedes ADR 0006 D2: claude is now SERVED on the inbound Anthropic
Messages surface via the local claude CLI under subscription OAuth. The
circularity concern is mitigated because Reverso's process env carries no
ANTHROPIC_BASE_URL and the claude adapter scrubs routing/auth env from the
spawned CLI's child env (see tests/unit/test_claude_oauth_gate.py for the scrub).
This module asserts, through the real CompositionRoot and the real
AnthropicMessagesApp, that claude routes resolve to the claude backend:

  - POST /claude/v1/messages (and its mixed-case /CLAUDE, /Claude variants) ->
    served first-party by the claude backend, NEVER delegated to the legacy app.
  - /claude/v1/messages/count_tokens -> a 200 input_tokens sizing response.
  - a claude model id on the BARE /v1/messages and on bare /v1/messages/count_tokens
    -> served by the claude backend (auto-resolution maps it to claude).
  - GET /v1/models contains the claude model ids (the registry indexes them).
  - build_anthropic_adapters constructs a ClaudeAdapter and the app accepts it.

No real provider, process, or credential is touched; the Responses gateway and the
Anthropic app are built over the deterministic FixtureAdapter seam (UNCHANGED).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import (
    AnthropicMessagesApp,
    build_anthropic_adapters,
    build_anthropic_app,
)
from reverso.protocols.responses_app import build_app
from reverso.protocols.surface_registry import list_anthropic_surface_models
from reverso.proxy.compose import CompositionRoot

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "codex", "claude"]
RESPONSES_PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]

# A claude model id; the family marker is what the resolver detects.
_CLAUDE_MODEL = "claude-opus-4-8"


def _build_root() -> tuple[CompositionRoot, list[str]]:
    """A CompositionRoot with a legacy tripwire that records any delegated path."""
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


def _root_client(root: CompositionRoot) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=root)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _anthropic_client() -> httpx.AsyncClient:
    app = build_anthropic_app({b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _assert_anthropic_message(resp: httpx.Response) -> None:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"


# --- /claude/v1/messages prefix (incl mixed case), served first-party -------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/claude/v1/messages", "/CLAUDE/v1/messages", "/Claude/v1/messages"],
)
async def test_claude_messages_prefix_served_first_party_not_legacy(
    path: str,
) -> None:
    """The claude Messages prefix (any casing) is served first-party, never legacy."""
    root, legacy_calls = _build_root()
    async with _root_client(root) as client:
        resp = await client.post(path, json={"model": _CLAUDE_MODEL, "messages": []})
    _assert_anthropic_message(resp)
    assert legacy_calls == [], (
        f"{path} must be answered by the Anthropic app, never the legacy app; "
        f"observed {legacy_calls!r}"
    )


@pytest.mark.asyncio
async def test_claude_count_tokens_prefix_served_first_party_not_legacy() -> None:
    """The claude count_tokens prefix is served first-party with a sizing response."""
    root, legacy_calls = _build_root()
    async with _root_client(root) as client:
        resp = await client.post(
            "/claude/v1/messages/count_tokens",
            json={
                "model": _CLAUDE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"input_tokens"}
    assert legacy_calls == [], (
        "/claude/v1/messages/count_tokens must be answered first-party, never the "
        f"legacy app; observed {legacy_calls!r}"
    )


# --- claude MODEL id on the bare auto-routing paths -------------------------


@pytest.mark.asyncio
async def test_claude_model_on_bare_messages_served() -> None:
    """A claude model id on bare /v1/messages auto-routes to the claude backend."""
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": _CLAUDE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    _assert_anthropic_message(resp)


@pytest.mark.asyncio
async def test_claude_model_on_bare_count_tokens_served() -> None:
    """A claude model id on bare /v1/messages/count_tokens sizes a 200 response."""
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": _CLAUDE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"input_tokens"}


# --- /v1/models lists the claude models -------------------------------------


@pytest.mark.asyncio
async def test_models_listing_contains_claude_models() -> None:
    """GET /v1/models on the Anthropic surface lists the claude model ids."""
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()["data"]]
    assert ids, "expected at least one listed model"
    assert any(
        "claude" in model_id.lower() for model_id in ids
    ), f"claude models must appear on the Anthropic surface listing; got {ids!r}"


def test_surface_registry_set_contains_claude_models() -> None:
    """The surface_registry authority indexes claude rows mapping to the claude backend."""
    rows = list_anthropic_surface_models()
    claude_rows = [row for row in rows if "claude" in row["id"].lower()]
    assert claude_rows, "expected claude rows in the surface_registry listing"
    assert all(row["backend"] == "claude" for row in claude_rows)


# --- build_anthropic_adapters constructs ClaudeAdapter ----------------------


def test_build_anthropic_adapters_includes_claude() -> None:
    """The real adapter factory builds the surface backends, including claude."""
    adapters = build_anthropic_adapters()
    assert "claude" in adapters
    assert set(adapters) == set(ANTHROPIC_BACKENDS)


def test_build_anthropic_adapters_constructs_claude_adapter() -> None:
    """The claude entry is a ClaudeAdapter instance (ADR 0008)."""
    from reverso.protocols.adapters.claude import ClaudeAdapter

    adapters = build_anthropic_adapters()
    assert isinstance(adapters["claude"], ClaudeAdapter)


def test_anthropic_app_accepts_injected_claude_adapter() -> None:
    """Injecting a claude adapter is now permitted (claude is served, ADR 0008)."""

    class _StubAdapter:
        pass

    app = AnthropicMessagesApp({"claude": _StubAdapter()})
    assert "claude" in app._adapters
