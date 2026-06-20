"""Unit tests for the single first-party model-to-backend authority (ADR 0006 D2).

surface_registry is the ONLY first-party resolver and the single reader of
config/litellm_config.yaml as data. These tests pin: correct backend resolution
for known models, None for unknown models, fail-closed None for any claude model
(including mixed-case and aliases), mixed-case backend routing, the SURFACE_BACKENDS
claude exclusion, and the build-time cross-check (pass plus drift detection).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reverso.protocols import surface_registry
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    _build_model_index,
    cross_check_anthropic_models,
    resolve_anthropic_backend,
)


def test_known_deepseek_models_resolve_to_deepseek() -> None:
    for model in (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-reasoner",
        "deepseek-chat",
    ):
        assert resolve_anthropic_backend(model) == "deepseek"


def test_mixed_case_deepseek_routes() -> None:
    assert resolve_anthropic_backend("DeepSeek-V4-Pro") == "deepseek"
    assert resolve_anthropic_backend("  deepseek-v4-flash  ") == "deepseek"
    assert resolve_anthropic_backend("custom/deepseek-chat") == "deepseek"


def test_unknown_model_returns_none() -> None:
    assert resolve_anthropic_backend("gpt-5.5") is None
    assert resolve_anthropic_backend("totally-unknown") is None
    assert resolve_anthropic_backend("") is None
    assert resolve_anthropic_backend(None) is None


def test_claude_models_fail_closed_to_none() -> None:
    # Every claude-family name, including aliases and mixed case, must resolve to
    # None: the Anthropic surface excludes claude (D2 circularity), fail-closed.
    for model in (
        "claude",
        "claude-opus",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-6",
        "Claude-Opus",
        "CLAUDE-SONNET",
        "claude-opus-4.8",  # MODEL_ALIASES form
        "custom/claude-sonnet-4-6",
    ):
        assert resolve_anthropic_backend(model) is None, model


def test_surface_backends_excludes_claude() -> None:
    anthropic = SURFACE_BACKENDS["anthropic"]
    assert anthropic == frozenset({"copilot", "deepseek", "auggie"})
    assert "claude" not in anthropic


def test_cross_check_passes_against_real_config() -> None:
    # No exception means every indexed Anthropic-routed model exists in the
    # litellm_config data and maps to an Anthropic-surface backend.
    cross_check_anthropic_models()


def test_cross_check_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model indexed but absent from the config must raise (drift detection)."""
    # Point the registry at a config that has NO deepseek rows, then inject a
    # stale index entry; the cross-check must catch the missing model.
    config = tmp_path / "litellm_config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            model_list:
              - model_name: deepseek-v4-pro
                litellm_params:
                  model: custom_openai/deepseek-v4-pro
            """
        )
    )
    monkeypatch.setenv("REVERSO_CONFIG", str(config))
    # Index claims a model that is not in this config -> drift.
    monkeypatch.setattr(
        surface_registry, "_MODEL_INDEX", {"deepseek-ghost": "deepseek"}
    )
    with pytest.raises(RuntimeError, match="drift"):
        cross_check_anthropic_models()


def test_cross_check_detects_non_surface_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model mapping to a backend not on the Anthropic surface must raise."""
    monkeypatch.setattr(
        surface_registry, "_MODEL_INDEX", {"deepseek-v4-pro": "claude"}
    )
    with pytest.raises(RuntimeError, match="drift"):
        cross_check_anthropic_models()


def test_build_index_skips_claude_rows() -> None:
    # The index built from the real config must never carry a claude row.
    index = _build_model_index()
    assert all("claude" not in name for name in index)
    assert all(backend in SURFACE_BACKENDS["anthropic"] for backend in index.values())
