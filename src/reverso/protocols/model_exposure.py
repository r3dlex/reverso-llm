"""Codex-visible model selector and catalog exposure policy."""

from __future__ import annotations

from dataclasses import dataclass

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


STATIC_CATALOG_SEEDS: tuple[StaticCatalogSeed, ...] = (
    StaticCatalogSeed("codex", CODEX_BUILTIN_MODELS),
    StaticCatalogSeed("minimax", ("MiniMax-M3",)),
    StaticCatalogSeed("oauth", ("gemini-2.5-pro", "gemini-2.5-flash")),
)


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
