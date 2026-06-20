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


def test_cross_check_passes_on_minimal_config(tmp_path: Path) -> None:
    """cross_check passes when config contains only valid deepseek rows."""
    config = tmp_path / "litellm_config.yaml"
    config.write_text(
        textwrap.dedent(
            """\
            model_list:
              - model_name: deepseek-v4-pro
                litellm_params:
                  model: custom_openai/deepseek-v4-pro
            """
        )
    )
    cross_check_anthropic_models(config)


def test_cross_check_detects_non_surface_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that resolves to a backend not in SURFACE_BACKENDS raises RuntimeError.

    cross_check rebuilds the index from the config path (MINOR-2 robust rebuild).
    The registry derives backends from model-name prefixes, so we inject a bad
    derivation by monkeypatching _backend_for_model_name to return 'claude' for
    deepseek rows. The freshly-built index will contain {deepseek-v4-pro: 'claude'},
    and the backend check must catch that 'claude' is not an Anthropic surface backend.
    """
    import reverso.protocols.surface_registry as _reg

    config = tmp_path / "litellm_config.yaml"
    config.write_text(
        textwrap.dedent(
            """\
            model_list:
              - model_name: deepseek-v4-pro
                litellm_params:
                  model: custom_openai/deepseek-v4-pro
            """
        )
    )
    original = _reg._backend_for_model_name

    def _bad_backend(model_name: str) -> str | None:
        if "deepseek" in model_name.lower():
            return "claude"
        return original(model_name)

    monkeypatch.setattr(_reg, "_backend_for_model_name", _bad_backend)
    with pytest.raises(RuntimeError, match="drift"):
        cross_check_anthropic_models(config)


def test_build_index_skips_claude_rows() -> None:
    # The index built from the real config must never carry a claude row.
    index = _build_model_index()
    assert all("claude" not in name for name in index)
    assert all(backend in SURFACE_BACKENDS["anthropic"] for backend in index.values())
