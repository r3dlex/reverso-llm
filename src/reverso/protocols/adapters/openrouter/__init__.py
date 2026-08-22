"""OpenRouter provider support (ADR 0020, OR-G1..OR-G3)."""

from __future__ import annotations

from .adapter import OpenRouterAdapter, OpenRouterError, ResponseStore
from .budget import (
    BudgetLedger,
    BudgetReservation,
    InsufficientBudgetError,
    MissingLimitsError,
)
from .catalog import (
    OpenRouterCatalogEntry,
    OpenRouterCatalogError,
    OpenRouterCatalogSource,
)
from .continuation import (
    ContinuationRejection,
    ReplayChain,
    ReplayItem,
    build_continuation_request,
    materialize_continuation,
)
from .messages import (
    OpenRouterMessagesAdapter,
    OpenRouterMessagesError,
    build_messages_payload,
    normalize_messages_response,
)
from .credentials import OpenRouterCredentialError, resolve_api_key
from .grants import GrantRegistry, InvalidGrantError, LauncherLiveness
from .policy import (
    FreshnessBoundError,
    OpenRouterPolicy,
    PolicyEvaluator,
    PolicyRejectionError,
    UnknownPolicyError,
)
from .runtime import (
    OpenRouterRuntime,
    OpenRouterSurfaceGrant,
    build_openrouter_runtime,
    reset_openrouter_runtime,
)
from .transport import HttpOpenRouterTransport, OpenRouterTransportError

__all__ = (
    "BudgetLedger",
    "BudgetReservation",
    "ContinuationRejection",
    "FreshnessBoundError",
    "GrantRegistry",
    "HttpOpenRouterTransport",
    "InsufficientBudgetError",
    "InvalidGrantError",
    "LauncherLiveness",
    "MissingLimitsError",
    "OpenRouterAdapter",
    "OpenRouterCatalogEntry",
    "OpenRouterCatalogError",
    "OpenRouterCatalogSource",
    "OpenRouterCredentialError",
    "OpenRouterError",
    "OpenRouterMessagesAdapter",
    "OpenRouterMessagesError",
    "OpenRouterPolicy",
    "OpenRouterRuntime",
    "OpenRouterSurfaceGrant",
    "OpenRouterTransportError",
    "PolicyEvaluator",
    "PolicyRejectionError",
    "ReplayChain",
    "ReplayItem",
    "ResponseStore",
    "UnknownPolicyError",
    "build_continuation_request",
    "build_messages_payload",
    "build_openrouter_runtime",
    "materialize_continuation",
    "normalize_messages_response",
    "reset_openrouter_runtime",
    "resolve_api_key",
)
