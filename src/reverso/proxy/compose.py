"""Composition root: the front dispatcher that owns loopback port 64946 (ADR 0003).

This module resolves the single-port composition gap. The first-party
ResponsesGatewayApp (reverso.protocols.responses_app) and the legacy LiteLLM app
(reverso.proxy.app) cannot both bind 64946, so this dispatcher owns the port and
routes by path prefix:

  - first-party provider prefixes (claude, copilot, auggie, deepseek on /v1/...)
    go to the first-party gateway, served entirely without LiteLLM;
  - everything else is delegated verbatim to the legacy LiteLLM app.

reverso.proxy.main boots this module's ``app``. Repointing main back to
``reverso.proxy.app:app`` is the one-line rollback (ADR 0003 D1): the legacy app
still understands the claude/deepseek profile prefixes, so first-party traffic
falls back onto LiteLLM with no data migration.

The legacy app is imported lazily (inside ``_resolve_legacy``) so that merely
importing this module to construct the gateway (e.g. in tests) does not pull
LiteLLM into the import graph. The first-party gateway itself never imports
reverso.proxy.app; the LiteLLM quarantine guard test asserts that invariant.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.responses_app import build_app, split_provider_path

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def build_adapters() -> dict[str, ProviderAdapter]:
    """Construct the real {prefix: adapter} registry for the first-party gateway.

    Adapters are imported here (not at module top) so the registry can be built
    without importing every provider's transitive dependencies until boot.
    """
    from reverso.protocols.adapters.auggie import AuggieAdapter
    from reverso.protocols.adapters.claude import ClaudeAdapter
    from reverso.protocols.adapters.copilot import CopilotAdapter
    from reverso.protocols.adapters.deepseek import DeepSeekAdapter

    return {
        "claude": ClaudeAdapter(),
        "copilot": CopilotAdapter(),
        "auggie": AuggieAdapter(),
        "deepseek": DeepSeekAdapter(),
    }


class CompositionRoot:
    """ASGI dispatcher routing first-party prefixes to the gateway, else legacy.

    ``gateway`` defaults to the real first-party gateway built from
    ``build_adapters``. ``legacy_app`` is resolved lazily on the first
    non-first-party request unless injected (tests inject a tripwire to assert
    the legacy LiteLLM app is bypassed for first-party prefixes).
    """

    def __init__(
        self,
        *,
        gateway: Callable[[Scope, Receive, Send], Awaitable[None]] | None = None,
        legacy_app: Callable[[Scope, Receive, Send], Awaitable[None]] | None = None,
    ) -> None:
        self._gateway = gateway if gateway is not None else build_app(build_adapters())
        self._legacy_app = legacy_app

    def _resolve_legacy(self) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        if self._legacy_app is None:
            from reverso.proxy.app import app as legacy_app

            self._legacy_app = legacy_app
        return self._legacy_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            routed = split_provider_path(str(scope.get("path", "")))
            if routed is not None:
                await self._gateway(scope, receive, send)
                return
        legacy_app = self._resolve_legacy()
        await legacy_app(scope, receive, send)


app = CompositionRoot()
