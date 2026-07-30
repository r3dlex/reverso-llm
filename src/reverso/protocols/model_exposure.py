"""Codex-visible model selector and catalog exposure policy."""

from __future__ import annotations

import os
from dataclasses import dataclass

from reverso.protocols.copilot_models import copilot_model_route

CODEX_DEFAULT_MODEL = "gpt-5.5"
CODEX_BUILTIN_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
    "gpt-4.1",
)
CODEX_FRONTIER_MODELS: tuple[str, ...] = CODEX_BUILTIN_MODELS[:2]
PREFIXED_SELECTOR_PREFIXES = frozenset(
    {"copilot", "auggie", "agy", "codex-direct", "openai-pass-through"}
)


@dataclass(frozen=True)
class StaticCatalogSeed:
    """Provider-owned catalog aliases that exist without live model fetch."""

    prefix: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class CodexProfileSpec:
    """One Codex provider profile that the sync tool may write."""

    prefix: str
    model: str
    model_provider: str
    uses_model_catalog: bool
    model_context_window: int | None = None
    model_auto_compact_token_limit: int | None = None


STATIC_CATALOG_SEEDS: tuple[StaticCatalogSeed, ...] = (
    StaticCatalogSeed("codex", CODEX_BUILTIN_MODELS),
    StaticCatalogSeed("minimax", ("MiniMax-M3",)),
    StaticCatalogSeed("oauth", ("gemini-2.5-pro", "gemini-2.5-flash")),
)
REVERSO_ROUTED_CODEX_PROFILE_PREFIXES: tuple[str, ...] = (
    "claude",
    "copilot",
    "auggie",
    "deepseek",
    "kimi",
)
CODEX_DIRECT_BACKEND_ENV = "REVERSO_CODEX_DIRECT_BACKEND"
OPENAI_BACKEND_ENV = "REVERSO_OPENAI_BACKEND"
REVERSO_HOST_ENV = "REVERSO_HOST"
_CODEX_DIRECT_PROFILE_PREFIX = "codex-direct"
_OPENAI_PROFILE_PREFIX = "openai-pass-through"
DEEPSEEK_CODEX_PROFILE_DEFAULT = "deepseek-v4-pro"
KIMI_CODEX_MODEL = "kimi-k3"
KIMI_CODEX_CONTEXT_WINDOW = 1048576
KIMI_CODEX_AUTO_COMPACT_TOKEN_LIMIT = KIMI_CODEX_CONTEXT_WINDOW * 9 // 10
DIRECT_CODEX_PROFILE_SPECS: tuple[CodexProfileSpec, ...] = (
    CodexProfileSpec(
        prefix="openai",
        model=CODEX_DEFAULT_MODEL,
        model_provider="openai",
        uses_model_catalog=False,
    ),
    CodexProfileSpec(
        prefix="minimax",
        model="MiniMax-M3",
        model_provider="minimax",
        uses_model_catalog=False,
        model_context_window=512000,
    ),
)
STALE_CODEX_VARIANT_PROFILE_STEMS: frozenset[str] = frozenset(
    {
        "deepseek-gpt54",
        "deepseek-mini",
        "deepseek-spark",
        "minimax-gpt54",
        "minimax-mini",
        "minimax-spark",
    }
)


def codex_direct_profile_enabled(env: dict[str, str] | None = None) -> bool:
    """Return False only when the Codex Direct profile is explicitly disabled."""
    source = os.environ if env is None else env
    if source.get(REVERSO_HOST_ENV, "127.0.0.1").strip() != "127.0.0.1":
        return False
    raw = source.get(CODEX_DIRECT_BACKEND_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def openai_profile_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True only for explicit local-loopback OpenAI profile opt-in."""
    source = os.environ if env is None else env
    if source.get(REVERSO_HOST_ENV, "127.0.0.1").strip() != "127.0.0.1":
        return False
    raw = source.get(OPENAI_BACKEND_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "openai"}


def reverso_routed_codex_profile_prefixes(
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return provider prefixes whose Codex profiles route through Reverso."""
    prefixes = REVERSO_ROUTED_CODEX_PROFILE_PREFIXES
    if codex_direct_profile_enabled(env):
        prefixes = prefixes + (_CODEX_DIRECT_PROFILE_PREFIX,)
    if openai_profile_enabled(env):
        prefixes = prefixes + (_OPENAI_PROFILE_PREFIX,)
    return prefixes


def direct_codex_profile_specs() -> tuple[CodexProfileSpec, ...]:
    """Return direct Codex provider profiles that are not Reverso routes."""
    return DIRECT_CODEX_PROFILE_SPECS


def stale_codex_variant_profile_stems() -> frozenset[str]:
    """Return exact generated variant profile stems safe for sync archival."""
    return STALE_CODEX_VARIANT_PROFILE_STEMS


def codex_profile_default_model(prefix: str, models: tuple[str, ...]) -> str:
    """Return the default model for a provider-name Codex profile."""
    if prefix == "deepseek" and DEEPSEEK_CODEX_PROFILE_DEFAULT in models:
        model_id = DEEPSEEK_CODEX_PROFILE_DEFAULT
    else:
        model_id = models[0]
    return selector_model_id(prefix, model_id)


def codex_responses_compatible_model_ids(
    prefix: str, model_ids: tuple[str, ...]
) -> tuple[str, ...]:
    """Return model ids the Codex picker should expose for ``prefix``.

    For ``copilot``, GitHub Copilot serves ``gpt-*`` on /responses and
    ``claude-*``/``gemini-*`` on /chat/completions (ADR 0011, commit
    4507019). ``copilot_model_route`` is the single authority for which ids
    Copilot serves, so we accept any model with a known route. For other
    prefixes, all upstream models are exposed unchanged.
    """
    if prefix == "kimi":
        return tuple(model_id for model_id in model_ids if model_id == KIMI_CODEX_MODEL)
    if prefix != "copilot":
        return model_ids
    return tuple(
        model_id for model_id in model_ids if copilot_model_route(model_id) is not None
    )


def reverso_codex_profile_spec(
    prefix: str, models: tuple[str, ...]
) -> CodexProfileSpec:
    """Return a Reverso-routed provider profile spec for live models."""
    return CodexProfileSpec(
        prefix=prefix,
        model=codex_profile_default_model(prefix, models),
        model_provider=f"reverso_{prefix}",
        uses_model_catalog=True,
        model_context_window=(KIMI_CODEX_CONTEXT_WINDOW if prefix == "kimi" else None),
        model_auto_compact_token_limit=(
            KIMI_CODEX_AUTO_COMPACT_TOKEN_LIMIT if prefix == "kimi" else None
        ),
    )


def provider_scoped_catalog_slug(prefix: str, model_id: str) -> str:
    """Return the catalog slug for a provider-scoped Codex picker."""
    return selector_model_id(prefix, model_id)


def codex_catalog_context_window(prefix: str, model_id: str) -> int:
    """Return provider-scoped Codex catalog context metadata for a model id."""
    if prefix == "kimi" and model_id == KIMI_CODEX_MODEL:
        return KIMI_CODEX_CONTEXT_WINDOW
    if "500k" in model_id.lower():
        return 500000
    return 128000


# Per-served-model context-window sizes for the /usage telemetry surface
# (Slice 1b). Unmapped ids return None so the HUD renders ``n/a`` rather than a
# confidently-wrong percentage computed against a guessed window. This is the
# /usage-specific variant; ``codex_catalog_context_window`` above stays
# int-typed because ``codex_sync`` needs a concrete value for catalog files.
_CODEX_USAGE_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.5": 128000,
    "gpt-5.4": 128000,
    "gpt-5.4-mini": 128000,
    "gpt-5.3-codex-spark": 128000,
    "gpt-4.1": 128000,
}


def codex_usage_context_window(model_id: str) -> int | None:
    """Context-window size for a served Codex model id, for /usage telemetry.

    Returns the known window for a served gpt-* id (500000 for a 500k variant),
    and ``None`` for an unmapped id so the consumer renders ``n/a`` instead of
    dividing used tokens by a guessed window.
    """
    if "500k" in model_id.lower():
        return 500000
    return _CODEX_USAGE_CONTEXT_WINDOWS.get(model_id)


def catalog_display_name(prefix: str, model_id: str) -> str:
    """Return a human display name that makes routing ownership explicit."""
    if prefix == "codex":
        return f"GPT (Codex) {model_id}"
    if prefix == "minimax":
        return f"MiniMax {model_id}"
    if prefix == "oauth":
        return f"OAuth {model_id}"
    if prefix == "claude":
        return f"Claude (Claude Code) {model_id}"
    if prefix == "deepseek":
        return f"DeepSeek {model_id}"
    if prefix == "codex-direct":
        return f"Codex Direct (OAuth) {model_id}"
    if prefix == "openai-pass-through":
        return f"OpenAI pass-through {model_id}"
    return f"Reverso {prefix} {model_id}"


def selector_model_id(prefix: str, model_id: str) -> str:
    """Return the Codex-visible selector id for a provider/model pair."""
    if prefix in PREFIXED_SELECTOR_PREFIXES:
        return f"{prefix}/{model_id}"
    return model_id


def claude_code_selector_model_id(prefix: str, model_id: str) -> str:
    """Return the Claude Code selector id for a provider/model pair."""
    if prefix in PREFIXED_SELECTOR_PREFIXES:
        return f"{prefix}-{model_id}"
    return model_id


def codex_builtin_model_backends() -> dict[str, str]:
    """Return built-in Codex GPT ids mapped to their Anthropic backend."""
    return {model_id: "codex" for model_id in CODEX_BUILTIN_MODELS}
