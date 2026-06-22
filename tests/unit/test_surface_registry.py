"""Unit tests for the single first-party model-to-backend authority (ADR 0006 D2).

surface_registry is the ONLY first-party resolver and the single reader of
config/litellm_config.yaml as data. These tests pin: correct backend resolution
for known models, None for unknown models, claude-family resolution to the claude
backend (including mixed-case and aliases, ADR 0009), mixed-case backend routing,
the SURFACE_BACKENDS claude inclusion, and the build-time cross-check (pass plus
drift detection).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    _build_model_index,
    canonical_model_id,
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


def test_resolve_anthropic_backend_claude_opus() -> None:
    # ADR 0009 spec (a): the canonical claude id resolves to the claude backend.
    assert resolve_anthropic_backend("claude-opus-4-8") == "claude"


def test_claude_models_resolve_to_claude() -> None:
    # Every claude-family name, including aliases and mixed case, resolves to the
    # claude backend, now served on the Anthropic surface (ADR 0009).
    for model in (
        "claude",
        "claude-opus",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-6",
        "Claude-Opus",
        "CLAUDE-SONNET",
        "custom/claude-sonnet-4-6",
    ):
        assert resolve_anthropic_backend(model) == "claude", model


def test_surface_backends_includes_claude() -> None:
    anthropic = SURFACE_BACKENDS["anthropic"]
    assert anthropic == frozenset({"copilot", "deepseek", "auggie", "codex", "claude"})
    assert "claude" in anthropic


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
    derivation by monkeypatching _backend_for_model_name to return a backend not
    on the Anthropic surface ('mystery') for deepseek rows. The freshly-built
    index will contain {deepseek-v4-pro: 'mystery'}, and the backend check must
    catch that 'mystery' is not an Anthropic surface backend.
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
            return "mystery"
        return original(model_name)

    monkeypatch.setattr(_reg, "_backend_for_model_name", _bad_backend)
    with pytest.raises(RuntimeError, match="drift"):
        cross_check_anthropic_models(config)


def test_build_index_maps_claude_rows_to_claude_backend() -> None:
    # The index built from the real config carries claude rows mapping to claude.
    index = _build_model_index()
    claude_rows = {name: backend for name, backend in index.items() if "claude" in name}
    assert claude_rows, "expected claude rows in the index (ADR 0009)"
    assert all(backend == "claude" for backend in claude_rows.values())
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


def test_qualified_provider_routes_by_prefix() -> None:
    # A provider/model id resolves by its explicit prefix when the bare model is
    # served by that same backend (provider up front, no conflict).
    assert resolve_anthropic_backend("codex/gpt-5.5") == "codex"
    assert resolve_anthropic_backend("codex/gpt-4.1") == "codex"
    assert resolve_anthropic_backend("deepseek/deepseek-v4-pro") == "deepseek"
    assert resolve_anthropic_backend("deepseek/deepseek-chat") == "deepseek"


def test_qualified_prefix_trusted_only_for_rowless_backends() -> None:
    # copilot and auggie carry no config rows (rowless), so an arbitrary bare model
    # behind their prefix is trusted (provider-up-front intent).
    assert resolve_anthropic_backend("copilot/anything-goes") == "copilot"
    assert resolve_anthropic_backend("auggie/auggie-default") == "auggie"
    # deepseek and codex DO own a taxonomy, so an unknown bare model fails closed
    # exactly as the bare-id path would (no fail-open behind a known-backend prefix).
    assert resolve_anthropic_backend("codex/totally-made-up") is None
    assert resolve_anthropic_backend("deepseek/totally-made-up") is None
    # whitespace inside the bare model is not a served codex id -> fail closed.
    assert resolve_anthropic_backend("codex/ gpt-5.5") is None


def test_qualified_mismatch_fails_closed() -> None:
    # The prefix names one backend but the bare model is indexed to another: a
    # conflict that must fail closed rather than silently honor either side.
    assert resolve_anthropic_backend("deepseek/gpt-5.5") is None
    assert resolve_anthropic_backend("codex/deepseek-v4-pro") is None


def test_qualified_non_surface_prefix_fails_closed() -> None:
    assert resolve_anthropic_backend("openai/gpt-5.5") is None
    assert resolve_anthropic_backend("unknown/whatever") is None
    # claude is now a served surface backend (ADR 0009), but a claude prefix on a
    # NON-claude model is still a mismatch conflict and fails closed.
    assert resolve_anthropic_backend("claude/gpt-5.5") is None


def test_qualified_claude_prefix_now_served() -> None:
    # ADR 0009: claude joined the Anthropic surface, so a claude/<claude-model>
    # qualified id resolves to the claude backend (was fail-closed pre-merge).
    assert resolve_anthropic_backend("claude/claude-opus-4-8") == "claude"


def test_qualified_malformed_fails_closed() -> None:
    assert resolve_anthropic_backend("codex/") is None
    assert resolve_anthropic_backend("/gpt-5.5") is None
    assert resolve_anthropic_backend("/") is None


def test_qualified_mixed_case_and_custom_prefix() -> None:
    assert resolve_anthropic_backend("Codex/GPT-5.5") == "codex"
    assert resolve_anthropic_backend("  deepseek/Deepseek-V4-Pro  ") == "deepseek"
    assert resolve_anthropic_backend("custom/codex/gpt-5.5") == "codex"


def test_canonical_model_id_strips_valid_qualifier() -> None:
    # The bare upstream id (original casing) is returned, qualifier removed.
    assert canonical_model_id("codex/gpt-5.5") == "gpt-5.5"
    assert canonical_model_id("deepseek/deepseek-v4-pro") == "deepseek-v4-pro"
    assert canonical_model_id("copilot/Some-Model") == "Some-Model"
    assert canonical_model_id("custom/codex/gpt-5.5") == "gpt-5.5"
    # Mixed case and an upper-case custom/ prefix must canonicalize the same way
    # the resolver routes them (no divergence -> no prefix leak to the adapter).
    assert canonical_model_id("Codex/GPT-5.5") == "GPT-5.5"
    assert canonical_model_id("CUSTOM/codex/gpt-5.5") == "gpt-5.5"
    # claude is now a served surface backend (ADR 0009), so its qualifier strips too.
    assert canonical_model_id("claude/claude-opus-4-8") == "claude-opus-4-8"


@pytest.mark.parametrize(
    "model",
    [
        "codex/gpt-5.5",
        "deepseek/deepseek-v4-pro",
        "Codex/GPT-5.5",
        "CUSTOM/codex/gpt-5.5",
        "copilot/any-model",
        "auggie/auggie-default",
    ],
)
def test_resolve_and_canonical_agree_no_prefix_leak(model: str) -> None:
    """Coupling invariant (ADR 0008): whenever the resolver routes a qualified id,
    canonical_model_id strips the qualifier so the adapter never sees the prefix.
    """
    if resolve_anthropic_backend(model) is not None:
        canonical = canonical_model_id(model)
        assert canonical is not None
        assert "/" not in canonical, (model, canonical)


def test_canonical_model_id_leaves_bare_and_invalid_unchanged() -> None:
    assert canonical_model_id("gpt-5.5") == "gpt-5.5"
    assert canonical_model_id("deepseek-v4-pro") == "deepseek-v4-pro"
    # A non-surface qualifier is left intact (it 404s at resolution).
    assert canonical_model_id("openai/gpt-5.5") == "openai/gpt-5.5"
    assert canonical_model_id(None) is None


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
