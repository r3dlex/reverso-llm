"""Tests for shared Codex model exposure policy."""

from __future__ import annotations

from reverso.protocols.model_exposure import (
    CODEX_BUILTIN_MODELS,
    CODEX_DEFAULT_MODEL,
    CODEX_FRONTIER_MODELS,
    STATIC_CATALOG_SEEDS,
    codex_builtin_model_backends,
    selector_model_id,
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
    assert selector_model_id("codex", "gpt-5.5") == "gpt-5.5"
    assert selector_model_id("deepseek", "deepseek-v4-pro") == "deepseek-v4-pro"
    assert selector_model_id("minimax", "MiniMax-M3") == "MiniMax-M3"
    assert selector_model_id("claude", "claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_static_catalog_seeds_are_unprefixed_for_builtin_codex() -> None:
    seeds_by_prefix = {seed.prefix: seed.model_ids for seed in STATIC_CATALOG_SEEDS}
    assert seeds_by_prefix["codex"] == CODEX_BUILTIN_MODELS
    assert "MiniMax-M3" in seeds_by_prefix["minimax"]
    assert "gemini-2.5-pro" in seeds_by_prefix["oauth"]


def test_codex_builtin_model_backends_share_surface_mapping() -> None:
    assert codex_builtin_model_backends() == {
        model_id: "codex" for model_id in CODEX_BUILTIN_MODELS
    }
