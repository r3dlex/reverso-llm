"""Integration tests for the two auxiliary Anthropic endpoints (ADR 0006 G006).

Covers POST /v1/messages/count_tokens (a documented word-count APPROXIMATION, not
a real tokenizer) and the bare GET /v1/models (the Anthropic-shaped listing of the
surface_registry Anthropic-surface model set, now including claude rows per ADR
0008), both driven through the real AnthropicMessagesApp and the real CompositionRoot.

The CompositionRoot coexistence test pins the key G006 risk: the bare GET
/v1/models now routes to the Anthropic surface, while /deepseek/v1/models and
/v1/responses still reach the Responses gateway byte-unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.protocols.responses_app import build_app
from reverso.protocols.surface_registry import (
    list_anthropic_discovery_aliases,
    list_anthropic_surface_models,
)
from reverso.proxy.compose import CompositionRoot

ANTHROPIC_BACKENDS = ["copilot", "deepseek", "auggie", "claude"]
RESPONSES_PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]

# A real deepseek model id from litellm_config so count_tokens resolves a backend.
KNOWN_MODEL = "deepseek-v4-pro"


def _anthropic_client() -> httpx.AsyncClient:
    app = build_anthropic_app({b: FixtureAdapter(b) for b in ANTHROPIC_BACKENDS})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _messages_body(model: str, text: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 64,
    }


# --- count_tokens -----------------------------------------------------------


@pytest.mark.asyncio
async def test_count_tokens_returns_positive_input_tokens() -> None:
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json=_messages_body(KNOWN_MODEL, "hello there general kenobi"),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"input_tokens"}
    assert isinstance(body["input_tokens"], int)
    assert body["input_tokens"] > 0


@pytest.mark.asyncio
async def test_count_tokens_increases_with_content_words() -> None:
    """More content words yield a strictly larger input_tokens approximation.

    The estimator word-counts the flattened prompt, so a prompt with strictly more
    words sizes strictly larger; this pins the word-count behavior (not a real
    tokenizer), per the G006 review nit.
    """
    short = _messages_body(KNOWN_MODEL, "one two three")
    longer = _messages_body(
        KNOWN_MODEL, "one two three four five six seven eight nine ten"
    )
    async with _anthropic_client() as client:
        short_resp = await client.post("/v1/messages/count_tokens", json=short)
        long_resp = await client.post("/v1/messages/count_tokens", json=longer)
    assert short_resp.status_code == 200
    assert long_resp.status_code == 200
    short_tokens = short_resp.json()["input_tokens"]
    long_tokens = long_resp.json()["input_tokens"]
    assert long_tokens >= short_tokens
    assert long_tokens > short_tokens, (
        "a strictly longer word-count prompt should size strictly larger under "
        "the word-count approximation"
    )


@pytest.mark.asyncio
async def test_count_tokens_unknown_model_404() -> None:
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json=_messages_body("totally-unknown-model", "hi"),
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_count_tokens_claude_model_served() -> None:
    # A claude model now resolves to the claude backend (ADR 0009); count_tokens
    # sizes it the same as any served model.
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json=_messages_body("claude-opus", "hi"),
        )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"input_tokens"}


# --- /v1/models -------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_returns_anthropic_listing_shape() -> None:
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "first_id", "last_id", "has_more"}
    assert body["has_more"] is False
    assert isinstance(body["data"], list)
    assert body["data"], "the Anthropic-surface model listing must not be empty"
    for row in body["data"]:
        assert row["type"] == "model"
        assert isinstance(row["id"], str) and row["id"]
        assert isinstance(row["display_name"], str) and row["display_name"]
        assert isinstance(row["created_at"], str) and row["created_at"]
    assert body["first_id"] == body["data"][0]["id"]
    assert body["last_id"] == body["data"][-1]["id"]


@pytest.mark.asyncio
async def test_models_includes_claude() -> None:
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    ids = [row["id"] for row in resp.json()["data"]]
    assert ids, "expected at least one listed model"
    assert any(
        "claude" in model_id.lower() for model_id in ids
    ), f"claude models must appear on the Anthropic surface listing (ADR 0009); got {ids!r}"


@pytest.mark.asyncio
async def test_models_match_surface_registry_set() -> None:
    """The listed ids are exactly the bare surface set PLUS the discovery aliases.

    The bare surface listing is the canonical set; the anthropic--<backend>-- aliases
    are additive so non-claude backends pass Claude Code's /model discovery filter.
    """
    expected = {row["id"] for row in list_anthropic_surface_models()} | {
        row["id"] for row in list_anthropic_discovery_aliases()
    }
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    listed = {row["id"] for row in resp.json()["data"]}
    assert listed == expected


@pytest.mark.asyncio
async def test_models_discovery_aliases_pass_claude_code_filter() -> None:
    """Every non-claude backend is selectable in /model: each has an id beginning with
    'anthropic' (Claude Code's gateway discovery drops anything else)."""
    async with _anthropic_client() as client:
        resp = await client.get("/v1/models")
    ids = [row["id"] for row in resp.json()["data"]]
    discoverable = [m for m in ids if m.lower().startswith(("claude", "anthropic"))]
    # codex/deepseek/copilot/auggie are reachable only via their anthropic- aliases.
    for backend in ("codex", "deepseek", "copilot", "auggie"):
        assert any(
            m.startswith(f"anthropic-{backend}-") for m in discoverable
        ), f"{backend} has no discovery alias in /v1/models; got {discoverable!r}"


# --- CompositionRoot coexistence (the key G006 risk) ------------------------


def _build_root() -> tuple[CompositionRoot, list[str]]:
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


@pytest.mark.asyncio
async def test_bare_models_hits_anthropic_surface_others_unchanged() -> None:
    """Bare GET /v1/models routes to the Anthropic surface; Responses unchanged.

    The bare listing must reach the Anthropic app (Anthropic shape, never legacy),
    while /deepseek/v1/models (per-provider Responses listing) and /v1/responses
    (the Responses create path) still reach the first-party Responses gateway
    byte-unchanged and never the Anthropic app or the legacy LiteLLM app.
    """
    root, legacy_calls = _build_root()
    async with _root_client(root) as client:
        bare_models = await client.get("/v1/models")
        provider_models = await client.get("/deepseek/v1/models")
        responses = await client.post(
            "/deepseek/v1/responses",
            json={"model": "deepseek-v4-pro", "input": "hi"},
        )

    # Bare /v1/models is the Anthropic surface: Anthropic listing shape, not the
    # Responses ModelList object shape and not the legacy fallthrough.
    assert bare_models.status_code == 200
    bare_body = bare_models.json()
    assert set(bare_body) == {"data", "first_id", "last_id", "has_more"}
    assert bare_body["data"][0]["type"] == "model"

    # /deepseek/v1/models is the Responses gateway listing, byte-unchanged: it is
    # the Responses ModelList shape (object == "list"), not the Anthropic shape.
    assert provider_models.status_code == 200
    provider_body = provider_models.json()
    assert provider_body["object"] == "list"
    assert "first_id" not in provider_body

    # /v1/responses still reaches the Responses gateway and completes a turn.
    assert responses.status_code == 200
    assert responses.json()["object"] == "response"

    assert legacy_calls == [], (
        "bare /v1/models, /deepseek/v1/models, and /v1/responses must all be "
        f"served first-party, never the legacy app; observed {legacy_calls!r}"
    )
