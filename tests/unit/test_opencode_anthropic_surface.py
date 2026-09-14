"""OCG-G5: OpenCode Go on the Anthropic surface.

This is the first real consumer of the catalog-owning backend kind added in G2
(ADR 0020). Until now `_CATALOG_OWNING_BACKENDS` shipped empty, so these tests
also prove the mechanism works against a live catalog rather than a fixture.
"""

from __future__ import annotations

from reverso.protocols.adapters.opencode.adapter import OpenCodeAdapter
from reverso.protocols.adapters.opencode.catalog import FALLBACK_MODEL_IDS
from reverso.protocols.anthropic_native import AnthropicNativeAdapter
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    list_anthropic_discovery_aliases,
    resolve_anthropic_backend,
)


def test_exposed_on_the_anthropic_surface() -> None:
    assert "opencode" in SURFACE_BACKENDS["anthropic"]


def test_registered_as_an_anthropic_backend() -> None:
    from reverso.protocols.anthropic_app import build_anthropic_adapters

    adapters = build_anthropic_adapters()
    assert isinstance(adapters["opencode"], OpenCodeAdapter)


def test_the_adapter_serves_the_native_facet() -> None:
    """Native dispatch avoids the Anthropic -> Responses -> Anthropic round trip."""
    assert isinstance(OpenCodeAdapter(), AnthropicNativeAdapter)


def test_qualified_routing_reaches_a_contested_id() -> None:
    """The whole point of ADR 0020: kimi owns bare kimi-k3, and the prefix still
    reaches OpenCode's own kimi-k3."""
    assert resolve_anthropic_backend("opencode/kimi-k3") == "opencode"
    assert resolve_anthropic_backend("kimi-k3") == "kimi"


def test_qualified_routing_fails_closed_outside_the_catalog() -> None:
    assert resolve_anthropic_backend("opencode/not-in-catalog") is None


def test_bare_routing_for_a_unique_id() -> None:
    assert resolve_anthropic_backend("glm-5") == "opencode"


def test_no_pre_existing_bare_id_changed_backend() -> None:
    """The incumbency guarantee, stated as a test rather than trusted."""
    assert resolve_anthropic_backend("deepseek-v4-pro") == "deepseek"
    assert resolve_anthropic_backend("deepseek-v4-flash") == "deepseek"
    assert resolve_anthropic_backend("kimi-k3") == "kimi"


def test_every_catalog_id_has_a_discovery_alias() -> None:
    """All 29 must be selectable in the picker, including the contested ones,
    and generated from the catalog rather than a hand-maintained tuple."""
    aliases = {row["id"] for row in list_anthropic_discovery_aliases()}
    for model_id in FALLBACK_MODEL_IDS:
        assert f"anthropic-opencode-{model_id}" in aliases


def test_discovery_aliases_route_back_to_opencode() -> None:
    for model_id in ("glm-5", "kimi-k3"):
        assert resolve_anthropic_backend(f"anthropic-opencode-{model_id}") == "opencode"


def test_alias_generation_is_not_a_curated_tuple() -> None:
    """A curated list would silently miss ids as the catalog grows."""
    from reverso.protocols.surface_registry import _DISCOVERY_ROWLESS_MODELS

    assert "opencode" not in _DISCOVERY_ROWLESS_MODELS


def test_existing_backends_keep_their_aliases() -> None:
    aliases = {row["id"] for row in list_anthropic_discovery_aliases()}
    assert any(a.startswith("anthropic-copilot-") for a in aliases)
    assert any(a.startswith("anthropic-deepseek-") for a in aliases)
