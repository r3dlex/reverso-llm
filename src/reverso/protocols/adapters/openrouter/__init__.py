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
from .client_sync import (
    ClaudeLauncher,
    CodexProfile,
    build_claude_alias,
    build_claude_launcher,
    build_codex_profile,
)
from .messages import (
    OpenRouterMessagesAdapter,
    OpenRouterMessagesError,
    build_messages_payload,
    normalize_messages_response,
)
from .messages_stream import (
    OpenRouterMessagesStreamError,
    OpenRouterMessagesStreamingClient,
    build_stream_payload,
)
from .observability import OpenRouterMetrics, StatusSnapshot
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
    "ClaudeLauncher",
    "CodexProfile",
    "build_claude_alias",
    "OpenRouterMessagesAdapter",
    "OpenRouterMessagesError",
    "OpenRouterMessagesStreamError",
    "OpenRouterMessagesStreamingClient",
    "OpenRouterMetrics",
    "OpenRouterPolicy",
    "StatusSnapshot",
    "OpenRouterRuntime",
    "OpenRouterSurfaceGrant",
    "OpenRouterTransportError",
    "PolicyEvaluator",
    "PolicyRejectionError",
    "ReplayChain",
    "ReplayItem",
    "ResponseStore",
    "UnknownPolicyError",
    "build_claude_launcher",
    "build_codex_profile",
    "build_continuation_request",
    "build_messages_payload",
    "build_openrouter_runtime",
    "build_stream_payload",
    "materialize_continuation",
    "normalize_messages_response",
    "reset_openrouter_runtime",
    "resolve_api_key",
)
