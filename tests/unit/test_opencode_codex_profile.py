"""OCG-G8: the isolated Codex profile for OpenCode Go.

Deferred from G4 and G6 because adding the route prefix made codex-sync REQUIRE
live OpenCode discovery for every sync, failing closed for anyone without the
subscription. Treating discovery as optional (as ollama already is) is what makes
the route safe to add.
"""

from __future__ import annotations

from reverso.codex_sync import OPTIONAL_DISCOVERY_PREFIXES
from reverso.opencode_catalog_artifact import load_catalog_ids
from reverso.protocols.model_exposure import (
    OPENCODE_CODEX_PROFILE_DEFAULT,
    REVERSO_ROUTED_CODEX_PROFILE_PREFIXES,
    codex_profile_default_model,
    codex_responses_compatible_model_ids,
    reverso_codex_profile_spec,
)


def test_the_route_prefix_is_registered() -> None:
    assert "opencode" in REVERSO_ROUTED_CODEX_PROFILE_PREFIXES


def test_discovery_is_optional() -> None:
    """A paid subscription most deployments lack must not fail every sync."""
    assert "opencode" in OPTIONAL_DISCOVERY_PREFIXES


def test_the_default_model_is_not_a_gated_one() -> None:
    """The generic default is models[0], which for this catalog sorts to
    deepseek-v4-flash -- one of the ids gated behind a workspace opt-in. That
    default would 403 on first use, so an explicit default is required."""
    catalog = load_catalog_ids()
    assert catalog[0] == "deepseek-v4-flash"
    resolved = codex_profile_default_model("opencode", catalog)
    assert "deepseek-v4-flash" not in resolved
    assert OPENCODE_CODEX_PROFILE_DEFAULT in resolved


def test_the_default_model_is_dual_protocol_and_ungated() -> None:
    from reverso.protocols.adapters.opencode.catalog import (
        ANTHROPIC_UNSUPPORTED_MODELS,
    )

    assert OPENCODE_CODEX_PROFILE_DEFAULT in load_catalog_ids()
    assert OPENCODE_CODEX_PROFILE_DEFAULT not in ANTHROPIC_UNSUPPORTED_MODELS


def test_the_explicit_default_is_ignored_when_absent_from_discovery() -> None:
    """A stale default must not be pinned when upstream stops serving it."""
    resolved = codex_profile_default_model("opencode", ("hy3", "glm-5.3"))
    assert "hy3" in resolved


def test_the_profile_spec_routes_through_reverso() -> None:
    spec = reverso_codex_profile_spec("opencode", load_catalog_ids())
    assert spec.prefix == "opencode"
    assert spec.model_provider == "reverso_opencode"
    assert spec.uses_model_catalog is True


def test_the_profile_carries_the_default_models_context_window() -> None:
    """Codex sizes context management from the profile, and the profile pins one
    model, so unlike the multi-model Claude launcher this window can be exact."""
    from reverso.protocols.adapters.opencode.metadata import limits_for

    spec = reverso_codex_profile_spec("opencode", load_catalog_ids())
    expected = limits_for(OPENCODE_CODEX_PROFILE_DEFAULT)
    assert expected is not None
    assert spec.model_context_window == expected.context
    assert spec.model_auto_compact_token_limit == expected.context * 9 // 10


def test_every_catalog_id_stays_codex_selectable() -> None:
    """Publish-all holds on this surface too: a gated model surfaces its opt-in
    error rather than vanishing from the picker."""
    catalog = load_catalog_ids()
    assert codex_responses_compatible_model_ids("opencode", catalog) == catalog


def test_other_prefixes_keep_their_defaults() -> None:
    assert "deepseek-v4-pro" in codex_profile_default_model(
        "deepseek", ("deepseek-chat", "deepseek-v4-pro")
    )
    assert "kimi-k3" in codex_profile_default_model("kimi", ("kimi-k3",))
