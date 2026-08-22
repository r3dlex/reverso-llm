"""Authenticated catalog surface for the OpenRouter provider (OR-G1, U2, I1).

The catalog carries live, authenticated metadata for OpenRouter models. Cached
or last-known-good metadata is discovery-only and never authorizes inference;
admission requires a fresh authenticated snapshot with an immutable revision
and a bounded age proven by OR-G0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "OpenRouterCatalogEntry",
    "OpenRouterCatalogError",
    "OpenRouterCatalogSource",
]


class OpenRouterCatalogSource(str, Enum):
    """Provenance of a catalog entry."""

    LIVE_AUTHED = "live_authed"
    DISCOVERY_CACHE = "discovery_cache"
    UNKNOWN = "unknown"


class OpenRouterCatalogError(RuntimeError):
    """The OpenRouter catalog could not be loaded."""


@dataclass(frozen=True)
class OpenRouterCatalogEntry:
    """A single OpenRouter model entry from the authenticated catalog."""

    id: str  # author/model
    context_length: int
    top_provider_max_completion_tokens: int
    pricing_prompt: str  # decimal string
    pricing_completion: str  # decimal string
    supported_parameters: tuple[str, ...]
    source: OpenRouterCatalogSource = OpenRouterCatalogSource.LIVE_AUTHED
    revision: str = ""
    observed_at: float = 0.0
    raw: dict[str, object] = field(default_factory=dict, compare=False)

    def supports(self, parameter: str) -> bool:
        return parameter in self.supported_parameters


def entry_from_model_payload(payload: dict[str, object]) -> OpenRouterCatalogEntry:
    """Parse one entry from the OpenRouter ``/api/v1/models`` data list."""
    model_id = str(payload.get("id", "")).strip()
    if not model_id or "/" not in model_id:
        raise OpenRouterCatalogError(f"malformed model id: {payload!r}")
    pricing = payload.get("pricing") or {}
    top_provider = payload.get("top_provider") or {}
    parameters = tuple(payload.get("supported_parameters") or ())
    return OpenRouterCatalogEntry(
        id=model_id,
        context_length=int(payload.get("context_length") or 0),
        top_provider_max_completion_tokens=int(
            top_provider.get("max_completion_tokens") or 0
        ),
        pricing_prompt=str(pricing.get("prompt") or "0"),
        pricing_completion=str(pricing.get("completion") or "0"),
        supported_parameters=parameters,
        source=OpenRouterCatalogSource.LIVE_AUTHED,
        revision=str(payload.get("canonical_slug") or model_id),
        raw=payload,
    )
