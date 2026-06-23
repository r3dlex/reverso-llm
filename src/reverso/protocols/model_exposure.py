"""Codex-visible model selector and catalog exposure policy."""

from __future__ import annotations

from dataclasses import dataclass

from reverso.protocols.copilot_models import is_copilot_responses_model_id

CODEX_DEFAULT_MODEL = "gpt-5.5"
CODEX_BUILTIN_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
    "gpt-4.1",
)
CODEX_FRONTIER_MODELS: tuple[str, ...] = CODEX_BUILTIN_MODELS[:2]
PREFIXED_SELECTOR_PREFIXES = frozenset({"copilot", "auggie", "agy"})


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
)
DEEPSEEK_CODEX_PROFILE_DEFAULT = "deepseek-v4-pro"
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


def reverso_routed_codex_profile_prefixes() -> tuple[str, ...]:
    """Return provider prefixes whose Codex profiles route through Reverso."""
    return REVERSO_ROUTED_CODEX_PROFILE_PREFIXES


def direct_codex_profile_specs() -> tuple[CodexProfileSpec, ...]:
    """Return direct Codex provider profiles that are not Reverso routes."""
    return DIRECT_CODEX_PROFILE_SPECS


def stale_codex_variant_profile_stems() -> frozenset[str]:
    """Return exact generated variant profile stems safe for sync archival."""
    return STALE_CODEX_VARIANT_PROFILE_STEMS


def codex_profile_default_model(prefix: str, models: tuple[str, ...]) -> str:
    """Return the default model for a provider-name Codex profile."""
    if prefix == "deepseek" and DEEPSEEK_CODEX_PROFILE_DEFAULT in models:
        return DEEPSEEK_CODEX_PROFILE_DEFAULT
    return models[0]


def codex_responses_compatible_model_ids(
    prefix: str, model_ids: tuple[str, ...]
) -> tuple[str, ...]:
    """Filter live model ids to the subset Codex can call through Responses."""
    if prefix != "copilot":
        return model_ids
    return tuple(
        model_id for model_id in model_ids if is_copilot_responses_model_id(model_id)
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
    )


def provider_scoped_catalog_slug(_prefix: str, model_id: str) -> str:
    """Return the catalog slug for a provider-scoped Codex picker."""
    return model_id


def codex_catalog_context_window(model_id: str) -> int:
    """Return Codex catalog context window metadata for a model id."""
    if "500k" in model_id.lower():
        return 500000
    return 128000


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
