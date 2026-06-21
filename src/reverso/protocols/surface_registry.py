"""Single first-party model-to-backend authority (ADR 0006 D2).

This module is the ONLY first-party resolver that maps a requested model id to a
backend. The first-party stack otherwise routes by path prefix and holds no model
map of its own; the single model map in the system is the quarantined LiteLLM
config (config/litellm_config.yaml), which this module reads via ``yaml.safe_load``
as DATA only. It MUST NOT import the legacy LiteLLM app (reverso.proxy.app) or any
``litellm`` module, preserving the ADR 0002 D2 quarantine. A subprocess
import-graph guard (tests/unit/test_litellm_quarantine.py) asserts that invariant.

Surface-scoped exposure is data, held in ``SURFACE_BACKENDS`` keyed by surface, so
a backend can be present on the Responses surface yet absent from the Anthropic
surface without any code branch. Milestone 2 adds ``codex-cli`` to the Anthropic
surface as one row.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from reverso.protocols.model_exposure import codex_builtin_model_backends

# Surface-scoped backend exposure as DATA (ADR 0006 D2). The Anthropic surface
# exposes copilot, deepseek, and auggie; claude is EXCLUDED because Claude Code
# talking to a claude backend through Reverso is circular (the claude backend is
# the claude CLI itself). Milestone 2 adds "codex-cli" here as a single row.
SURFACE_BACKENDS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"copilot", "deepseek", "auggie", "codex"}),
}

# Model-name substrings/prefixes that identify the claude family. The Anthropic
# surface must never route to claude (fail-closed), so any model whose normalized
# name names claude resolves to None regardless of casing or aliasing.
_CLAUDE_MARKER = "claude"

# Which backend each litellm_config model_name maps to, by the model id naming
# convention used in config/litellm_config.yaml. The config carries no explicit
# backend tag, so the backend is derived from the model-name family here, the one
# place that knows the first-party backend taxonomy. copilot has no config rows
# (it is a first-party-only backend exposed via SURFACE_BACKENDS), so no copilot
# prefix is derived here.
_DEEPSEEK_PREFIX = "deepseek"
_AUGGIE_PREFIX = "auggie"

# The five gpt model ids served first-party by the codex backend on the Anthropic
# surface (Milestone 2, ADR 0007). These are NOT derivable from a config name
# prefix the way deepseek/auggie are: codex has its own first-party model taxonomy
# and (after G005) its rows are removed from litellm_config entirely. The mapping
# is therefore held as STATIC data here, seeded into the model index inside
# _build_model_index so BOTH the module-level _MODEL_INDEX and the fresh index
# cross_check_anthropic_models rebuilds carry these ids identically (C3). Routing
# stays config-independent yet lint-covered: the backend-membership assertion in
# cross_check still applies, while the config-existence assertion exempts these
# ids so import does not raise once the gpt config rows are gone.
_CODEX_MODELS: dict[str, str] = codex_builtin_model_backends()


def _resolve_config_path() -> Path:
    """Locate config/litellm_config.yaml, honoring the REVERSO_CONFIG override.

    Mirrors reverso.proxy.main._resolve_config_path so the registry reads the
    same config the proxy boots from. This file lives at
    src/reverso/protocols/surface_registry.py, so the repo root is parents[3].
    """
    override = os.environ.get("REVERSO_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "config" / "litellm_config.yaml"


def _normalize_model(model: str) -> str:
    """Normalize a requested model id: strip, lowercase, drop a custom/ prefix."""
    return model.strip().lower().removeprefix("custom/")


def _is_claude_model(normalized: str) -> bool:
    """Fail-closed claude detection on an already-normalized model id."""
    return _CLAUDE_MARKER in normalized


def _backend_for_model_name(model_name: str) -> str | None:
    """Map a litellm_config model_name to its first-party backend, or None.

    claude-family rows are intentionally unmapped: the Anthropic surface excludes
    claude, and copilot has no rows in the legacy config (it is a first-party-only
    backend), so copilot is not derivable from the config and is exposed by the
    SURFACE_BACKENDS table rather than the config rows.
    """
    normalized = _normalize_model(model_name)
    if _is_claude_model(normalized):
        return None
    if normalized.startswith(_DEEPSEEK_PREFIX):
        return "deepseek"
    if normalized.startswith(_AUGGIE_PREFIX):
        return "auggie"
    return None


def _load_model_list(path: Path | None = None) -> list[dict[str, Any]]:
    """Read litellm_config.yaml as DATA and return its model_list (never imports)."""
    config_path = path if path is not None else _resolve_config_path()
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        return []
    model_list = data.get("model_list")
    if not isinstance(model_list, list):
        return []
    return [row for row in model_list if isinstance(row, dict)]


def _build_model_index(path: Path | None = None) -> dict[str, str]:
    """Build {normalized_model_name: backend} from the litellm_config data.

    Only rows that resolve to an Anthropic-surface-eligible backend are indexed;
    claude rows are skipped (fail-closed) and copilot is added below as a
    first-party backend that has no legacy config rows.
    """
    index: dict[str, str] = {}
    for row in _load_model_list(path):
        model_name = row.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            continue
        backend = _backend_for_model_name(model_name)
        if backend is None:
            continue
        index[_normalize_model(model_name)] = backend
    # Seed the static codex ids (Milestone 2). These are config-independent: codex
    # owns its own model taxonomy and (after G005) has no litellm_config rows, so
    # they are seeded here rather than derived from config. Seeding inside the
    # builder (not only the module-level _MODEL_INDEX) means cross_check's
    # independently-rebuilt fresh_index carries them too, keeping resolution and
    # the build-time lint consistent (C3).
    for model_id, backend in _CODEX_MODELS.items():
        index[_normalize_model(model_id)] = backend
    return index


# Built once at import: the model->backend map derived from litellm_config DATA.
_MODEL_INDEX: dict[str, str] = _build_model_index()


def resolve_anthropic_backend(model: str | None) -> str | None:
    """Resolve a requested model to an Anthropic-surface backend, or None.

    Returns None for an unknown model AND for any claude-family model (fail-closed,
    even mixed-case or aliased). Model names are normalized (stripped, lowercased,
    custom/ prefix dropped) so mixed-case ids route correctly. A resolved backend
    is always a member of SURFACE_BACKENDS["anthropic"].
    """
    if not isinstance(model, str) or not model.strip():
        return None
    normalized = _normalize_model(model)
    if _is_claude_model(normalized):
        return None
    backend = _MODEL_INDEX.get(normalized)
    if backend is None:
        return None
    if backend not in SURFACE_BACKENDS["anthropic"]:
        return None
    return backend


def _display_name_for_model(model_id: str) -> str:
    """Derive a human-friendly display_name from a normalized model id.

    The litellm_config rows carry no display name, so it is derived here (the one
    place that owns the first-party model taxonomy): split on hyphens, RETAIN every
    token (the leading backend-family token is kept, not dropped), and title-case
    each word (a vN version token is upper-cased). This is a simple DERIVED label
    (documented as a convenience, not a provider-authoritative product name), e.g.
    "deepseek-v4-pro" -> "Deepseek V4 Pro", "auggie-default" -> "Auggie Default".
    """
    words = [part for part in model_id.split("-") if part]
    if not words:
        return model_id
    return " ".join(
        word.upper()
        if word.startswith("v") and word[1:].isdigit()
        else word.capitalize()
        for word in words
    )


def list_anthropic_surface_models() -> list[dict[str, str]]:
    """List the Anthropic-surface models for GET /v1/models (ADR 0006 D2/AC8).

    Returns one row per model exposed on the Anthropic surface, derived from the
    SAME litellm_config-backed ``_MODEL_INDEX`` that ``resolve_anthropic_backend``
    routes through, so the listing and the router never disagree. claude is never
    present (the index already excludes the claude family, fail-closed). Each row
    is ``{"id": <model>, "display_name": <derived label>, "backend": <backend>}``;
    the result is sorted by id for a deterministic, stable listing.
    """
    rows = [
        {
            "id": model_id,
            "display_name": _display_name_for_model(model_id),
            "backend": backend,
        }
        for model_id, backend in _MODEL_INDEX.items()
        if backend in SURFACE_BACKENDS["anthropic"]
    ]
    rows.sort(key=lambda row: row["id"])
    return rows


def cross_check_anthropic_models(path: Path | None = None) -> None:
    """Build-time lint: every Anthropic-routed model exists in the config.

    Rebuilds the model->backend index from the SAME config path it reads, so the
    check is self-consistent even when REVERSO_CONFIG changes between import-time
    and a later re-invocation (MINOR-2 robust rebuild). Asserts that every model
    in the freshly-built index exists in that same config's model_list, and that
    each resolved backend is exposed on the Anthropic surface. Raises RuntimeError
    on drift so an inconsistency is caught early rather than at request time.

    ``path`` is forwarded to ``_build_model_index`` and ``_load_model_list`` so
    tests can inject a temporary config without touching the environment.
    """
    fresh_index = _build_model_index(path)
    config_names = {
        _normalize_model(row["model_name"])
        for row in _load_model_list(path)
        if isinstance(row.get("model_name"), str)
    }
    anthropic_backends = SURFACE_BACKENDS["anthropic"]
    # The static codex ids are exempt from the CONFIG-EXISTENCE assertion only:
    # they are seeded data, not config rows, and G005 removes the gpt rows from
    # litellm_config entirely, so requiring them in the config would make import
    # raise. They are NOT exempt from the backend-membership assertion below, so
    # codex routing stays lint-covered (a codex id mapping to a backend absent
    # from the Anthropic surface still raises).
    codex_exempt = {_normalize_model(model_id) for model_id in _CODEX_MODELS}
    for model_name, backend in fresh_index.items():
        if model_name not in config_names and model_name not in codex_exempt:
            raise RuntimeError(
                "surface_registry drift: indexed Anthropic model "
                f"{model_name!r} is not present in litellm_config.yaml"
            )
        if backend not in anthropic_backends:
            raise RuntimeError(
                "surface_registry drift: model "
                f"{model_name!r} maps to backend {backend!r} which is not on the "
                "Anthropic surface"
            )


# Run the build-time lint at import so config drift fails closed and early.
cross_check_anthropic_models()
