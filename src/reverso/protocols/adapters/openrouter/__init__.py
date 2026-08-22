"""OpenRouter provider support (ADR 0020, OR-G1, OR-G2).

This package carries the credential, catalog, policy, grant, budget, runtime,
transport, and adapter facts the OpenRouter verticals are built on. The runtime
is the sole composition-owned owner; both the Codex Responses vertical (OR-G2)
and the Claude Messages vertical (OR-G4) share its adapter object.
"""

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
    "OpenRouterPolicy",
    "OpenRouterRuntime",
    "OpenRouterSurfaceGrant",
    "OpenRouterTransportError",
    "PolicyEvaluator",
    "PolicyRejectionError",
    "ResponseStore",
    "UnknownPolicyError",
    "build_openrouter_runtime",
    "reset_openrouter_runtime",
    "resolve_api_key",
)
