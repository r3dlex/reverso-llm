"""Negative conformance suite: claude is NEVER reachable on the Anthropic surface.

ADR 0006 D2 excludes claude from the Anthropic Messages surface (Claude Code
talking to a claude backend through Reverso is circular: the claude backend is the
claude CLI itself). This module consolidates the claude-exclusion conformance into
ONE durable suite asserting, through the real CompositionRoot and the real
AnthropicMessagesApp, that NO Anthropic-surface route or listed model ever resolves
to a claude backend:

  - POST /claude/v1/messages (and its mixed-case /CLAUDE, /Claude variants) ->
    Anthropic not_found_error 404, answered first-party and NEVER delegated to the
    legacy LiteLLM app.
  - /claude/v1/messages/count_tokens -> the same not_found_error 404 (the pinned
    claude prefix is claimed but never served on either Messages-family path).
  - a claude model id on the BARE /v1/messages and on bare /v1/messages/count_tokens
    -> not_found_error 404 (auto-resolution fails closed for the claude family).
  - GET /v1/models contains no claude model id (the registry index excludes it).
  - build_anthropic_adapters never constructs a ClaudeAdapter and rejects a claude
    adapter if one is injected.

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

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "codex"]
RESPONSES_PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]

# A claude model id; the family marker is what the fail-closed resolver detects.
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


def _assert_anthropic_404(resp: httpx.Response) -> None:
    assert resp.status_code == 404
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "not_found_error"


# --- /claude/v1/messages prefix (incl mixed case), never legacy -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/claude/v1/messages", "/CLAUDE/v1/messages", "/Claude/v1/messages"],
)
async def test_claude_messages_prefix_returns_anthropic_404_not_legacy(
    path: str,
) -> None:
    """The claude Messages prefix (any casing) is a first-party Anthropic 404."""
    root, legacy_calls = _build_root()
    async with _root_client(root) as client:
        resp = await client.post(path, json={"model": _CLAUDE_MODEL, "messages": []})
    _assert_anthropic_404(resp)
    assert legacy_calls == [], (
        f"{path} must be answered by the Anthropic app, never the legacy app; "
        f"observed {legacy_calls!r}"
    )


@pytest.mark.asyncio
async def test_claude_count_tokens_prefix_returns_anthropic_404_not_legacy() -> None:
    """The claude count_tokens prefix is also claimed and 404s first-party."""
    root, legacy_calls = _build_root()
    async with _root_client(root) as client:
        resp = await client.post(
            "/claude/v1/messages/count_tokens",
            json={"model": _CLAUDE_MODEL, "messages": []},
        )
    _assert_anthropic_404(resp)
    assert legacy_calls == [], (
        "/claude/v1/messages/count_tokens must be answered first-party, never the "
        f"legacy app; observed {legacy_calls!r}"
    )


# --- claude MODEL id on the bare auto-routing paths -------------------------


@pytest.mark.asyncio
async def test_claude_model_on_bare_messages_returns_404() -> None:
    """A claude model id on bare /v1/messages fails closed with a 404."""
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": _CLAUDE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    _assert_anthropic_404(resp)


@pytest.mark.asyncio
async def test_claude_model_on_bare_count_tokens_returns_404() -> None:
    """A claude model id on bare /v1/messages/count_tokens fails closed with a 404."""
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": _CLAUDE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    _assert_anthropic_404(resp)


# --- /v1/models lists no claude model ---------------------------------------


@pytest.mark.asyncio
async def test_models_listing_contains_no_claude_model() -> None:
    """GET /v1/models on the Anthropic surface lists no claude model id."""
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()["data"]]
    assert ids, "expected at least one listed model"
    assert all(
        "claude" not in model_id.lower() for model_id in ids
    ), f"no claude model may appear on the Anthropic surface listing; got {ids!r}"


def test_surface_registry_set_contains_no_claude_model() -> None:
    """The surface_registry authority itself indexes no claude model (fail-closed)."""
    ids = [row["id"] for row in list_anthropic_surface_models()]
    assert all("claude" not in model_id.lower() for model_id in ids)


# --- build_anthropic_adapters never constructs ClaudeAdapter ----------------


def test_build_anthropic_adapters_excludes_claude() -> None:
    """The real adapter factory builds only the surface backends, never claude."""
    adapters = build_anthropic_adapters()
    assert "claude" not in adapters
    assert set(adapters) == set(ANTHROPIC_BACKENDS)
    for adapter in adapters.values():
        assert type(adapter).__name__ != "ClaudeAdapter"


def test_build_anthropic_adapters_never_imports_claude_adapter() -> None:
    """No constructed adapter is a ClaudeAdapter instance.

    Imports ClaudeAdapter only to compare types here (the negative assertion);
    build_anthropic_adapters must never instantiate it.
    """
    from reverso.protocols.adapters.claude import ClaudeAdapter

    adapters = build_anthropic_adapters()
    assert not any(isinstance(adapter, ClaudeAdapter) for adapter in adapters.values())


def test_anthropic_app_rejects_injected_claude_adapter() -> None:
    """Injecting a claude adapter is a hard ValueError (claude is excluded, D2)."""

    class _StubAdapter:
        pass

    with pytest.raises(ValueError, match="claude"):
        AnthropicMessagesApp({"claude": _StubAdapter()})
