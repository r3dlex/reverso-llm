"""Composition-owned OpenRouter runtime (OR-G1, U1, I5, I6).

The runtime owns one ``httpx.AsyncClient``, one budget ledger, one grant
registry, one policy evaluator, one catalog handle, and one adapter object
shared by both the Codex Responses and Anthropic Messages registries. The
composition root alone creates it; reconstruction clears every grant.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from reverso.protocols.adapter import ResponsesRequest

from .budget import BudgetLedger
from .catalog import OpenRouterCatalogEntry, OpenRouterCatalogSource
from .grants import GrantRegistry, LauncherLiveness
from .policy import OpenRouterPolicy, PolicyEvaluator

__all__ = ["OpenRouterRuntime", "build_openrouter_runtime", "reset_openrouter_runtime"]


logger = logging.getLogger(__name__)

_OPENROUTER_OUTER_PREFIX = "openrouter"
_OUTER_PREFIX_LEN = len(_OPENROUTER_OUTER_PREFIX) + 1  # include trailing slash


@dataclass(frozen=True)
class OpenRouterSurfaceGrant:
    """Static catalog claim: a model is exposed on a surface with these capabilities."""

    model: str
    surface: str
    source: OpenRouterCatalogSource


class OpenRouterRuntime:
    """The composition-owned OpenRouter runtime."""

    def __init__(
        self,
        *,
        transport: Any,
        credentials: Any,
        catalog: Any,
        policy: OpenRouterPolicy,
        freshness_bound_seconds: int,
        allowlist: Iterable[str],
        request_limit_usd: str,
        session_limit_usd: str,
        catalog_overrides: Mapping[str, OpenRouterCatalogEntry] | None = None,
        surface_grants: Iterable[OpenRouterSurfaceGrant] | None = None,
        compatibility_providers: Iterable[str] = (),
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._catalog = catalog
        self._policy = policy
        self._freshness_bound_seconds = freshness_bound_seconds
        self._allowlist = frozenset(allowlist)
        self._catalog_overrides = dict(catalog_overrides or {})
        self._surface_grants = tuple(surface_grants or ())
        self._policy_evaluator = PolicyEvaluator(
            policy=policy,
            freshness_bound_seconds=freshness_bound_seconds,
            clock=clock or _default_clock,
            compatibility_providers=compatibility_providers,
        )
        self.budget = BudgetLedger(
            request_limit_usd=request_limit_usd,
            session_limit_usd=session_limit_usd,
        )
        self.grants = GrantRegistry(clock=clock or _default_clock)

    # --- Identity -------------------------------------------------------------

    def upstream_model(self, presented_model: str) -> str:
        """Strip exactly one ``openrouter/`` prefix and preserve ``author/model`` bytes."""
        if not isinstance(presented_model, str) or not presented_model:
            raise ValueError("presented_model must be a non-empty string")
        if not presented_model.startswith(_OPENROUTER_OUTER_PREFIX + "/"):
            raise ValueError(
                f"presented_model {presented_model!r} must start with 'openrouter/'"
            )
        remainder = presented_model[_OUTER_PREFIX_LEN:]
        if remainder.count("/") != 1 or "//" in remainder:
            raise ValueError(
                f"presented_model {presented_model!r} must be 'openrouter/<author>/<model>'"
            )
        author, model = remainder.split("/", 1)
        if not author or not model:
            raise ValueError(
                f"presented_model {presented_model!r} must be 'openrouter/<author>/<model>'"
            )
        return remainder

    # --- Allowlist and surface eligibility ------------------------------------

    def assert_allowlisted(self, presented_model: str) -> None:
        if presented_model not in self._allowlist:
            raise PermissionError(
                f"model {presented_model!r} is not on the curated allowlist"
            )

    def assert_surface_eligible(self, presented_model: str, *, surface: str) -> None:
        if not self._surface_grants:
            return
        upstream = self.upstream_model(presented_model)
        for grant in self._surface_grants:
            if grant.model == upstream and grant.surface == surface:
                return
        raise PermissionError(
            f"model {upstream!r} is not eligible for surface {surface!r}"
        )

    # --- Policy ---------------------------------------------------------------

    def apply_mandatory_policy(self, request: ResponsesRequest) -> ResponsesRequest:
        """Inject default ZDR, denied data collection, required parameters."""
        provider = dict(request.extra.get("provider") or {})
        provider["require_parameters"] = True
        provider["data_collection"] = "deny"
        provider["zdr"] = True
        if self._policy_evaluator.compatibility_providers:
            provider["only"] = list(self._policy_evaluator.compatibility_providers)
        extra = dict(request.extra)
        extra["provider"] = provider
        extra["X-OpenRouter-Title"] = "Reverso"
        return ResponsesRequest(
            model=request.model,
            input=request.input,
            stream=request.stream,
            previous_response_id=request.previous_response_id,
            tools=request.tools,
            instructions=request.instructions,
            tool_choice=request.tool_choice,
            extra=extra,
        )

    # --- Credentials ----------------------------------------------------------

    def outbound_headers(self) -> dict[str, str]:
        api_key = self._credentials.resolve_api_key()
        return {
            "Authorization": f"Bearer {api_key}",
            "X-OpenRouter-Title": "Reverso",
        }

    # --- Admission matrix -----------------------------------------------------

    def admit_and_dispatch(
        self,
        *,
        surface: str,
        presented_model: str,
        request: ResponsesRequest,
        launcher_pid: int | None = None,
        capability: str | None = None,
        liveness: LauncherLiveness | None = None,
    ) -> None:
        """Admit a request under the deny-first matrix.

        Every cell of the deny matrix raises before any inference call is made.
        The transport is never invoked from this method; tests use it to assert
        that the matrix denies uniformly.
        """
        self.assert_allowlisted(presented_model)
        self.assert_surface_eligible(presented_model, surface=surface)
        self._policy_evaluator.assert_fresh()
        self._policy_evaluator.require_zdr()
        self._policy_evaluator.require_data_denial()
        if capability is not None and launcher_pid is not None and liveness is not None:
            self.grants.verify(
                capability=capability,
                launcher_pid=launcher_pid,
                model=self.upstream_model(presented_model),
                liveness=liveness,
            )

    def close(self) -> None:
        """Revoke every grant and drop the composition reference."""
        self.grants.revoke_all()


# --- Singleton construction ---------------------------------------------------

_lock = threading.Lock()
_singleton: OpenRouterRuntime | None = None
_registered = False


def build_openrouter_runtime(**kwargs: Any) -> OpenRouterRuntime:
    """Construct a fresh OpenRouter runtime.

    The composition root in production owns the singleton via
    ``get_or_create_runtime``; tests can call this directly to build scoped
    instances. The autobahn one-runtime-per-composition invariant is
    enforced by the composition root, not by this constructor.
    """
    return OpenRouterRuntime(**kwargs)


def get_or_create_runtime(**kwargs: Any) -> OpenRouterRuntime:
    """Return the composition-owned runtime, creating it on first call."""
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = OpenRouterRuntime(**kwargs)
            _register_shutdown()
        return _singleton


def reset_openrouter_runtime() -> None:
    """Drop the singleton. The next ``get_or_create_runtime`` builds a fresh one."""
    global _singleton
    with _lock:
        if _singleton is not None:
            try:
                _singleton.close()
            finally:
                _singleton = None


def _register_shutdown() -> None:
    global _registered
    if _registered:
        return
    atexit.register(reset_openrouter_runtime)
    _registered = True


def _default_clock() -> float:
    import time

    return time.monotonic()
