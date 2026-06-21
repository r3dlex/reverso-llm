"""Negative conformance suite: codex is NEVER reachable on the Responses surface.

This is the EXACT MIRROR of test_anthropic_claude_exclusion. ADR 0007 makes codex
an Anthropic-surface-ONLY backend (gpt models served first-party through
`codex exec` under ChatGPT OAuth, the reverse of claude which is Responses-surface
only). Coexisting with the legacy LiteLLM CLI provider gpt-on-the-Responses route is removed
by the clean cut, so this suite asserts, through the real Responses gateway wiring,
that NO Responses-surface route, prefix, adapter, or listing ever resolves gpt/codex:

  - split_provider_path('/codex/v1/responses') is None: codex is NOT one of the
    Responses gateway's APP_PROVIDER_PREFIXES, so the composition root never routes
    a /codex/... path to the Responses gateway.
  - codex is absent from compose.build_adapters(): the real Responses gateway
    adapter registry holds only the four Responses backends, never codex.
  - a gpt-* model id is not served by any codex adapter on the Responses surface:
    no Responses-side route resolves gpt to codex (gpt lives only on the Anthropic
    surface, asserted by the parity matrix + test_codex_anthropic_surface).
  - GET /<provider>/v1/models on the Responses surface advertises no codex/gpt
    provider, the mirror of the claude-exclusion model-listing assertion.

No real provider, process, or credential is touched; the Responses gateway is
built over the deterministic FixtureAdapter seam (UNCHANGED). This test is
FALSIFIABLE: it would FAIL if codex were added to APP_PROVIDER_PREFIXES or to
compose.build_adapters().
"""

from __future__ import annotations

import httpx
import pytest

from conftest import FixtureAdapter
from reverso.protocols.responses_app import (
    APP_PROVIDER_PREFIXES,
    build_app,
    split_provider_path,
)
from reverso.proxy.compose import build_adapters

# The Responses-surface providers (claude is Responses-only here; codex is NOT).
RESPONSES_PROVIDERS = ["claude", "copilot", "auggie", "deepseek"]

# The five gpt ids and the codex backend key that must NEVER be on the Responses
# surface (they are Anthropic-surface-only, ADR 0007).
_GPT_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "gpt-4.1"]
_CODEX_KEY = "codex"


def _responses_client() -> httpx.AsyncClient:
    app = build_app({p: FixtureAdapter(p) for p in RESPONSES_PROVIDERS})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


# --- codex is not a Responses APP_PROVIDER_PREFIX ----------------------------


def test_codex_is_not_a_responses_app_prefix() -> None:
    """codex is absent from the Responses gateway's APP_PROVIDER_PREFIXES."""
    assert _CODEX_KEY not in APP_PROVIDER_PREFIXES


@pytest.mark.parametrize(
    "path",
    [
        "/codex/v1/responses",
        "/codex/v1/models",
        "/codex/v1/responses/resp_123",
    ],
)
def test_split_provider_path_rejects_codex_prefix(path: str) -> None:
    """split_provider_path returns None for any /codex/... Responses path."""
    assert split_provider_path(path) is None


# --- codex is absent from the real Responses adapter registry ----------------


def test_build_adapters_excludes_codex() -> None:
    """The real Responses gateway adapter registry never constructs codex."""
    adapters = build_adapters()
    assert _CODEX_KEY not in adapters
    assert set(adapters) == set(RESPONSES_PROVIDERS)
    for adapter in adapters.values():
        assert type(adapter).__name__ != "CodexAdapter"


# --- no gpt-* model is served by a codex adapter on the Responses surface -----


@pytest.mark.asyncio
@pytest.mark.parametrize("model", _GPT_MODELS)
async def test_gpt_model_not_served_by_codex_on_responses(model: str) -> None:
    """No /codex/v1/responses route resolves a gpt model (codex prefix is unrouted).

    The Responses gateway 404s the /codex prefix because it is not an
    APP_PROVIDER_PREFIX, so a gpt model can never reach a codex adapter here. The
    mirror of a claude model being unreachable on the Anthropic surface.
    """
    async with _responses_client() as client:
        resp = await client.post(
            "/codex/v1/responses",
            json={"model": model, "input": "Say hi."},
        )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"


# --- codex/gpt is not advertised as a Responses provider ---------------------


@pytest.mark.asyncio
async def test_codex_models_listing_unrouted_on_responses() -> None:
    """GET /codex/v1/models is unrouted on the Responses surface (404).

    The per-provider listing is keyed by the Responses prefix; because codex is
    not an APP_PROVIDER_PREFIX, there is no codex provider whose listing the
    Responses gateway could serve, the mirror of claude having no Anthropic listing.
    """
    async with _responses_client() as client:
        resp = await client.get("/codex/v1/models")
    assert resp.status_code == 404


def test_no_responses_provider_is_codex() -> None:
    """No Responses-surface provider key is codex (codex advertises no provider here).

    The per-provider /v1/models listing on the Responses surface is keyed by the
    provider prefix, so the set of advertised providers is exactly the registry
    keys; codex is absent from both, so it is never advertised as a Responses
    provider. (The FixtureAdapter replays a shared fixture body that happens to
    contain a gpt id, so the listed model ids are a fixture artifact, NOT a
    surface grant; the grant is the provider key set asserted here and the
    adapter registry asserted above.)
    """
    assert _CODEX_KEY not in set(build_adapters())
    assert _CODEX_KEY not in APP_PROVIDER_PREFIXES
