"""Tests for shared Codex model exposure policy."""

from __future__ import annotations

from reverso.protocols.model_exposure import (
    CODEX_BUILTIN_MODELS,
    CODEX_DEFAULT_MODEL,
    CODEX_DIRECT_BACKEND_ENV,
    CODEX_FRONTIER_MODELS,
    DIRECT_CODEX_PROFILE_SPECS,
    OPENAI_BACKEND_ENV,
    REVERSO_HOST_ENV,
    REVERSO_ROUTED_CODEX_PROFILE_PREFIXES,
    STALE_CODEX_VARIANT_PROFILE_STEMS,
    STATIC_CATALOG_SEEDS,
    claude_code_selector_model_id,
    codex_builtin_model_backends,
    codex_catalog_context_window,
    codex_profile_default_model,
    codex_responses_compatible_model_ids,
    direct_codex_profile_specs,
    provider_scoped_catalog_slug,
    reverso_codex_profile_spec,
    reverso_routed_codex_profile_prefixes,
    selector_model_id,
    stale_codex_variant_profile_stems,
)


def test_codex_builtin_models_stay_bare_and_default_first() -> None:
    assert CODEX_DEFAULT_MODEL == CODEX_BUILTIN_MODELS[0] == "gpt-5.5"
    assert all("/" not in model_id for model_id in CODEX_BUILTIN_MODELS)


def test_frontier_models_are_prefix_of_builtin_selector_order() -> None:
    assert CODEX_FRONTIER_MODELS == CODEX_BUILTIN_MODELS[:2]


def test_selector_model_id_prefixes_only_collision_prone_providers() -> None:
    assert selector_model_id("copilot", "gpt-5.5") == "copilot/gpt-5.5"
    assert selector_model_id("auggie", "gpt-5.5") == "auggie/gpt-5.5"
    assert selector_model_id("agy", "gpt-5.5") == "agy/gpt-5.5"
    assert selector_model_id("codex-direct", "gpt-5.5") == "codex-direct/gpt-5.5"
    assert (
        selector_model_id("openai-pass-through", "gpt-5.5")
        == "openai-pass-through/gpt-5.5"
    )
    assert selector_model_id("codex", "gpt-5.5") == "gpt-5.5"
    assert selector_model_id("deepseek", "deepseek-v4-pro") == "deepseek-v4-pro"
    assert selector_model_id("minimax", "MiniMax-M3") == "MiniMax-M3"
    assert selector_model_id("claude", "claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_claude_code_selector_uses_cli_safe_prefixes_for_conflicts() -> None:
    assert claude_code_selector_model_id("copilot", "opus-4-8") == "copilot-opus-4-8"
    assert claude_code_selector_model_id("auggie", "prism-a") == "auggie-prism-a"
    assert claude_code_selector_model_id("agy", "nova") == "agy-nova"
    assert (
        claude_code_selector_model_id("codex-direct", "gpt-5.5")
        == "codex-direct-gpt-5.5"
    )
    assert (
        claude_code_selector_model_id("openai-pass-through", "gpt-5.5")
        == "openai-pass-through-gpt-5.5"
    )
    assert claude_code_selector_model_id("claude", "sonnet") == "sonnet"
    assert (
        claude_code_selector_model_id("claude", "claude-opus-4-5") == "claude-opus-4-5"
    )
    assert (
        claude_code_selector_model_id("deepseek", "deepseek-v4-pro")
        == "deepseek-v4-pro"
    )


def test_static_catalog_seeds_are_unprefixed_for_builtin_codex() -> None:
    seeds_by_prefix = {seed.prefix: seed.model_ids for seed in STATIC_CATALOG_SEEDS}
    assert seeds_by_prefix["codex"] == CODEX_BUILTIN_MODELS
    assert "MiniMax-M3" in seeds_by_prefix["minimax"]
    assert "gemini-2.5-pro" in seeds_by_prefix["oauth"]


def test_codex_builtin_model_backends_share_surface_mapping() -> None:
    assert codex_builtin_model_backends() == {
        model_id: "codex" for model_id in CODEX_BUILTIN_MODELS
    }


def test_model_exposure_owns_codex_profile_sync_prefixes() -> None:
    assert reverso_routed_codex_profile_prefixes({}) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
        "codex-direct",
    )
    assert reverso_routed_codex_profile_prefixes(
        {}
    ) == REVERSO_ROUTED_CODEX_PROFILE_PREFIXES + ("codex-direct",)
    assert reverso_routed_codex_profile_prefixes({CODEX_DIRECT_BACKEND_ENV: "0"}) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
    )
    assert reverso_routed_codex_profile_prefixes({REVERSO_HOST_ENV: "0.0.0.0"}) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
    )
    assert reverso_routed_codex_profile_prefixes(
        {REVERSO_HOST_ENV: "0.0.0.0", CODEX_DIRECT_BACKEND_ENV: "1"}
    ) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
    )
    assert reverso_routed_codex_profile_prefixes({CODEX_DIRECT_BACKEND_ENV: "1"}) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
        "codex-direct",
    )
    assert reverso_routed_codex_profile_prefixes({OPENAI_BACKEND_ENV: "1"}) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
        "codex-direct",
        "openai-pass-through",
    )
    assert reverso_routed_codex_profile_prefixes(
        {OPENAI_BACKEND_ENV: "1", REVERSO_HOST_ENV: "0.0.0.0"}
    ) == (
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
    )
    direct = {spec.prefix: spec for spec in direct_codex_profile_specs()}
    assert direct_codex_profile_specs() == DIRECT_CODEX_PROFILE_SPECS
    assert direct["openai"].model_provider == "openai"
    assert direct["openai"].model == CODEX_DEFAULT_MODEL
    assert direct["openai"].uses_model_catalog is False
    assert direct["minimax"].model_provider == "minimax"
    assert direct["minimax"].model == "MiniMax-M3"
    assert direct["minimax"].model_context_window == 512000


def test_model_exposure_owns_codex_profile_default_model_policy() -> None:
    assert (
        codex_profile_default_model("deepseek", ("deepseek-v3", "deepseek-v4-pro"))
        == "deepseek-v4-pro"
    )
    assert (
        codex_profile_default_model("deepseek", ("deepseek-v3", "deepseek-r1"))
        == "deepseek-v3"
    )
    spec = reverso_codex_profile_spec("copilot", ("gpt-5.5", "gpt-4o"))
    assert spec.prefix == "copilot"
    assert spec.model == "gpt-5.5"
    assert spec.model_provider == "reverso_copilot"
    assert spec.uses_model_catalog is True
    kimi = reverso_codex_profile_spec("kimi", ("kimi-k3",))
    assert kimi.model == "kimi-k3"
    assert kimi.model_provider == "reverso_kimi"
    assert kimi.uses_model_catalog is True
    assert kimi.model_context_window == 1048576
    assert kimi.model_auto_compact_token_limit == 419430


def test_model_exposure_owns_codex_responses_model_eligibility() -> None:
    # copilot serves gpt-* on /responses AND claude-*/gemini-* on /chat
    # (ADR 0011, commit 4507019). The picker must expose both routes.
    assert codex_responses_compatible_model_ids(
        "copilot",
        (
            "claude-fable-5",  # chat route -> kept
            "gpt-4o",  # responses route -> kept
            "gpt-5.5",  # responses route -> kept
            "claude-opus-4.8",  # chat route -> kept
            "gemini-2.5-pro",  # chat route -> kept
            "gpt-5.4-mini",  # responses route -> kept
            "gpt５.５",  # fullwidth digit -> rejected (unsafe)
            "gpt-5.5\nmodel:claude-fable-5",  # injection -> rejected (unsafe)
            "totally-unknown-model",  # no route -> rejected
        ),
    ) == (
        "claude-fable-5",
        "gpt-4o",
        "gpt-5.5",
        "claude-opus-4.8",
        "gemini-2.5-pro",
        "gpt-5.4-mini",
    )
    # other prefixes are not filtered
    assert codex_responses_compatible_model_ids(
        "deepseek", ("deepseek-v3", "deepseek-r1")
    ) == ("deepseek-v3", "deepseek-r1")
    assert codex_responses_compatible_model_ids(
        "kimi", ("kimi-k2.5", "kimi-k3", "k3", "kimi-k3")
    ) == ("kimi-k3", "kimi-k3")


def test_model_exposure_owns_codex_catalog_and_stale_profile_policy() -> None:
    assert provider_scoped_catalog_slug("copilot", "gpt-5.5") == "copilot/gpt-5.5"
    assert provider_scoped_catalog_slug("auggie", "opus4.8") == "auggie/opus4.8"
    assert provider_scoped_catalog_slug("agy", "gemini-2.5-pro") == "agy/gemini-2.5-pro"
    assert provider_scoped_catalog_slug("codex", "gpt-5.5") == "gpt-5.5"
    assert (
        provider_scoped_catalog_slug("deepseek", "deepseek-v4-pro") == "deepseek-v4-pro"
    )
    assert (
        provider_scoped_catalog_slug("claude", "claude-sonnet-4-6")
        == "claude-sonnet-4-6"
    )
    assert codex_catalog_context_window("claude", "regular-model") == 128000
    assert codex_catalog_context_window("claude", "claude-500k") == 500000
    assert codex_catalog_context_window("kimi", "kimi-k3") == 1048576
    assert codex_catalog_context_window("copilot", "kimi-k3") == 128000
    assert stale_codex_variant_profile_stems() == STALE_CODEX_VARIANT_PROFILE_STEMS
    assert "deepseek-gpt54" in stale_codex_variant_profile_stems()
    assert "minimax-spark" in stale_codex_variant_profile_stems()
