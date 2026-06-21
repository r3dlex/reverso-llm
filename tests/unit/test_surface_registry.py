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
    list_anthropic_surface_models,
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
    assert resolve_anthropic_backend("totally-unknown") is None
    assert resolve_anthropic_backend("") is None
    assert resolve_anthropic_backend(None) is None


def test_codex_gpt_models_resolve_to_codex() -> None:
    # The five gpt ids served first-party on the Anthropic surface (Milestone 2)
    # resolve to the codex backend, independent of any litellm_config row.
    for model in (
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "gpt-4.1",
    ):
        assert resolve_anthropic_backend(model) == "codex", model


def test_codex_gpt_models_listed_with_backend_codex() -> None:
    rows = list_anthropic_surface_models()
    codex_ids = {row["id"] for row in rows if row["backend"] == "codex"}
    assert codex_ids == {
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "gpt-4.1",
    }


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
    assert anthropic == frozenset({"copilot", "deepseek", "auggie", "codex"})
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


def test_import_safe_without_gpt_config_rows(tmp_path: Path) -> None:
    """cross_check must NOT raise when the gpt rows are absent from the config.

    Simulates the post-G005 world: a config carrying only a deepseek row and NONE
    of the five gpt rows. The static _CODEX_MODELS ids are seeded into the fresh
    index but are EXEMPT from the config-existence assertion, so the lint passes
    even though gpt-* are not in config_names. This is the C3 import-safety
    guarantee that keeps ``import surface_registry`` from raising after G005.
    """
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
    # No RuntimeError: codex ids exempt from config-existence, deepseek present.
    cross_check_anthropic_models(config)


def test_lint_coverage_codex_routing_not_a_silent_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G004.2(e): codex routing is lint-covered, not a silent no-op.

    Removing ``codex`` from SURFACE_BACKENDS["anthropic"] while leaving it in
    _CODEX_MODELS must make cross_check_anthropic_models raise: the codex ids are
    exempt ONLY from the config-existence assertion, NOT from the
    backend-membership assertion. If they were exempt from both, dropping codex
    from the surface would silently pass and routing drift would go uncaught.
    """
    import reverso.protocols.surface_registry as _reg

    monkeypatch.setitem(
        _reg.SURFACE_BACKENDS,
        "anthropic",
        frozenset({"copilot", "deepseek", "auggie"}),
    )
    with pytest.raises(RuntimeError, match="drift"):
        cross_check_anthropic_models()
