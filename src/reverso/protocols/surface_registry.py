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
surface as one row. ADR 0009 adds ``claude``: claude-family ids are now SERVED on
the Anthropic surface via the local claude CLI (subscription OAuth), superseding
ADR 0006 D2's exclusion.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from reverso.protocols.model_exposure import codex_builtin_model_backends

# Surface-scoped backend exposure as DATA (ADR 0006 D2, superseded by ADR 0009).
# The Anthropic surface exposes copilot, deepseek, auggie, codex, and claude.
# claude is now SERVED on this surface (ADR 0009): the circularity concern (a
# claude backend that re-enters Reverso) is mitigated because Reverso runs as a
# server whose process env carries no ANTHROPIC_BASE_URL AND the claude adapter
# scrubs routing/auth env from the spawned CLI's child env, so the claude CLI
# always reaches api.anthropic.com under the subscription OAuth, never Reverso.
SURFACE_BACKENDS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"copilot", "deepseek", "auggie", "codex", "claude"}),
}

# Model-name substring that identifies the claude family. A normalized model id
# naming claude maps to the "claude" backend (ADR 0009); detection is casing- and
# alias-insensitive so every claude-family id routes to the claude CLI.
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

    claude-family rows map to the "claude" backend, now served on the Anthropic
    surface (ADR 0009). copilot has no rows in the legacy config (it is a
    first-party-only backend), so copilot is not derivable from the config and is
    exposed by the SURFACE_BACKENDS table rather than the config rows.
    """
    normalized = _normalize_model(model_name)
    if _is_claude_model(normalized):
        return "claude"
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
    claude rows now map to the "claude" backend (ADR 0009) and copilot is exposed
    via SURFACE_BACKENDS as a first-party backend that has no legacy config rows.
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

# Backends that own a concrete model taxonomy (they appear as a value in the
# index): deepseek and claude (config rows) and codex (static seed). A qualified
# id naming one of these MUST name a model the backend actually serves; only
# rowless backends (copilot/auggie) trust an arbitrary bare model behind their prefix.
_BACKENDS_WITH_ROWS: frozenset[str] = frozenset(_MODEL_INDEX.values())

# Claude Code's gateway model discovery (CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY)
# IGNORES any /v1/models id that does not begin with "claude" or "anthropic". claude
# ids already pass; to make every OTHER backend selectable in the /model picker, the
# discovery listing mints an "anthropic-<backend>-<bare>" alias per non-claude model.
# resolve_anthropic_backend + canonical_model_id route the alias back to <backend> with
# the bare model. A single dash reads cleanly in the picker, but real first-party model
# ids use single hyphens AND can contain them (gpt-5.5, deepseek-v4-pro), so the alias
# cannot be split naively on "-": after stripping the "anthropic-" prefix the remainder
# must begin with "<backend>-" where <backend> is a KNOWN Anthropic-surface backend; the
# rest is the bare model. The known backend names share no common prefix, so a first
# match over the known set is unambiguous.
_DISCOVERY_ALIAS_PREFIX = "anthropic-"

# Rowless backends own no _MODEL_INDEX taxonomy, so the discovery listing carries a
# curated, known-good set for the picker. Free-text copilot/<id> (and auggie/<id>)
# still reaches anything else the upstream serves; this only seeds the picker.
_DISCOVERY_ROWLESS_MODELS: dict[str, tuple[str, ...]] = {
    "copilot": ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"),
    "auggie": ("opus4.7", "haiku4.5"),
}


def _split_discovery_alias(normalized: str) -> tuple[str, str] | None:
    """Split a discovery alias ``anthropic-<backend>-<bare>`` into (backend, bare).

    Returns None when the id is not a well-formed alias naming an Anthropic-surface
    backend with a non-empty bare model. ``normalized`` is the output of
    ``_normalize_model`` (lowercased, custom/ stripped), so backend/bare are lowercase.

    The separator is a single dash, which model ids also use (and embed), so the alias
    cannot be split naively on ``-``. After stripping the ``anthropic-`` prefix the
    remainder must begin with ``<backend>-`` for a KNOWN Anthropic-surface backend; the
    rest is the bare model. The known backend names share no common prefix, so a first
    match over the known set is unambiguous. Fails closed for ``anthropic-`` alone, a
    known backend with no bare model, an unknown backend token, or an empty bare model.
    """
    if not normalized.startswith(_DISCOVERY_ALIAS_PREFIX):
        return None
    rest = normalized[len(_DISCOVERY_ALIAS_PREFIX) :]
    for backend in SURFACE_BACKENDS["anthropic"]:
        token = f"{backend}-"
        if rest.startswith(token):
            bare = rest[len(token) :]
            if not bare:
                return None
            return backend, bare
    return None


def _split_provider_qualified(normalized: str) -> tuple[str | None, str]:
    """Split a normalized id into (provider, bare_model) on the first '/'.

    Returns ``(None, normalized)`` when the id carries no '/' separator, i.e. it is
    a bare model name routed by name family rather than an explicit provider
    prefix. The ``custom/`` prefix is already removed by ``_normalize_model``, so
    any remaining '/' is a provider qualifier (model ids on this surface never
    contain a slash of their own).
    """
    head, sep, tail = normalized.partition("/")
    if not sep:
        return None, normalized
    return head, tail


def _resolve_qualified(provider: str, bare: str) -> str | None:
    """Resolve a fully-qualified ``provider/model`` id, provider prefix authoritative.

    The provider prefix lets a caller put the provider up front to disambiguate
    when two backends would otherwise share a model name. Fail-closed: the provider
    must be an Anthropic-surface backend and the bare model must be non-empty.

    A ROWLESS backend (copilot/auggie) owns no model taxonomy, so the index cannot
    know which models it serves; an explicit rowless provider is therefore
    authoritative for ANY bare id, INCLUDING one indexed to a rows-owning backend.
    This is the provider-up-front disambiguation in action: ``copilot/gpt-5.5``
    selects GitHub Copilot's gpt-5.5, distinct from codex's bare ``gpt-5.5`` (the
    two are different upstream subscriptions that happen to share a model name).

    A rows-owning backend (codex/deepseek/claude) must name a model indexed to
    ITSELF: a bare id indexed to a different backend (e.g. ``deepseek/gpt-5.5``) is a
    conflict that resolves to None, and a bare id unknown to the index fails closed
    exactly as the bare-id path would.
    """
    if not bare or provider not in SURFACE_BACKENDS["anthropic"]:
        return None
    if provider not in _BACKENDS_WITH_ROWS:
        return provider
    indexed = _MODEL_INDEX.get(bare)
    if indexed is not None:
        return provider if indexed == provider else None
    return None


def resolve_anthropic_backend(model: str | None) -> str | None:
    """Resolve a requested model to an Anthropic-surface backend, or None.

    Returns None for an unknown model. A claude-family model resolves to the
    "claude" backend via _MODEL_INDEX (ADR 0009), no longer fail-closed. Model
    names are normalized (stripped, lowercased, custom/ prefix dropped) so
    mixed-case ids route correctly. A resolved backend is always a member of
    SURFACE_BACKENDS["anthropic"].

    A fully-qualified ``provider/model`` id routes by its explicit provider prefix
    (provider up front), so callers can disambiguate conflicting model names; the
    prefix must be an Anthropic-surface backend and must not contradict a bare model
    that is independently indexed to a different backend. Bare ids keep name-family
    resolution unchanged.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    normalized = _normalize_model(model)
    alias = _split_discovery_alias(normalized)
    if alias is not None:
        # A /model-picker discovery alias (anthropic-<backend>-<bare>) routes by its
        # explicit backend; the bare model is validated downstream by that adapter.
        return alias[0]
    provider, bare = _split_provider_qualified(normalized)
    if provider is not None:
        return _resolve_qualified(provider, bare)
    backend = _MODEL_INDEX.get(normalized)
    if backend is None:
        return None
    if backend not in SURFACE_BACKENDS["anthropic"]:
        return None
    return backend


def canonical_model_id(model: str | None) -> str | None:
    """Strip a valid ``provider/`` qualifier, returning the bare upstream model id.

    The provider prefix is a routing hint for resolve_anthropic_backend, NOT part of
    the model id the downstream adapter expects. When ``model`` is a fully-qualified
    ``provider/model`` whose provider is an Anthropic-surface backend, the bare model
    (original casing, qualifier removed) is returned. Otherwise the input is returned
    unchanged. claude is now an Anthropic-surface backend (ADR 0009), so a
    ``claude/`` prefix is stripped like any other served provider; only a non-surface
    prefix (or a bare id) is left intact.

    The provider decision uses the SAME normalization as resolve_anthropic_backend
    (``_normalize_model`` + ``_split_provider_qualified``), so the two never diverge:
    every qualified id the resolver routes is stripped to its bare model here (e.g.
    mixed-case ``Codex/GPT-5.5`` and ``CUSTOM/codex/gpt-5.5`` both canonicalize),
    guaranteeing the provider prefix never leaks to the adapter.
    """
    if not isinstance(model, str):
        return None
    normalized = _normalize_model(model)
    alias = _split_discovery_alias(normalized)
    if alias is not None:
        # Discovery alias anthropic-<backend>-<bare> -> bare upstream id. The alias is
        # machine-minted (lowercase ascii), so the normalized bare is the wire id; this
        # mirrors resolve_anthropic_backend so router and canonicalizer never diverge.
        return alias[1]
    provider, _bare = _split_provider_qualified(normalized)
    if provider is None or provider not in SURFACE_BACKENDS["anthropic"]:
        return model
    # Strip the qualifier from the ORIGINAL string so the bare model keeps its
    # casing (consistent with how a bare id reaches the adapter unmodified), using
    # the same case-insensitive custom/ semantics as _normalize_model.
    stripped = model.strip()
    if stripped.lower().startswith("custom/"):
        stripped = stripped[len("custom/") :]
    _head, _sep, tail = stripped.partition("/")
    return tail


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
    routes through, so the listing and the router never disagree. claude rows are
    now present and map to the "claude" backend (ADR 0009). Each row
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


def _discovery_alias_row(backend: str, model_id: str) -> dict[str, str]:
    return {
        "id": f"anthropic-{backend}-{model_id}",
        "display_name": f"{backend.capitalize()}: {model_id}",
        "backend": backend,
    }


def list_anthropic_discovery_aliases() -> list[dict[str, str]]:
    """List ``anthropic-<backend>-<model>`` aliases for the /model picker.

    Claude Code's gateway model discovery ignores any /v1/models id not beginning with
    "claude" or "anthropic". claude-family ids already pass, so this mints an alias for
    every OTHER Anthropic-surface backend (codex/deepseek from the index, copilot/auggie
    from the curated rowless set) so they become selectable in the picker. Each alias
    routes back through resolve_anthropic_backend + canonical_model_id to its backend and
    bare model. Sorted by id for a deterministic listing. The bare surface listing
    (list_anthropic_surface_models) is unchanged; this is purely additive for discovery.
    """
    rows: list[dict[str, str]] = []
    for model_id, backend in _MODEL_INDEX.items():
        if backend == "claude" or backend not in SURFACE_BACKENDS["anthropic"]:
            continue
        rows.append(_discovery_alias_row(backend, model_id))
    for backend, models in _DISCOVERY_ROWLESS_MODELS.items():
        if backend not in SURFACE_BACKENDS["anthropic"]:
            continue
        for model_id in models:
            rows.append(_discovery_alias_row(backend, model_id))
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
