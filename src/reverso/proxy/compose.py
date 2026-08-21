"""Composition root: the front dispatcher that owns loopback port 64946 (ADR 0003).

This module resolves the single-port composition gap. The first-party
ResponsesGatewayApp (reverso.protocols.responses_app) and the legacy LiteLLM app
(reverso.proxy.app) cannot both bind 64946, so this dispatcher owns the port and
routes by path prefix:

  - first-party provider prefixes (claude, copilot, auggie, deepseek on /v1/...)
    go to the first-party gateway, served entirely without LiteLLM;
  - everything else is delegated verbatim to the legacy LiteLLM app.

``GET /usage`` and ``GET /usage/headroom`` are handled directly here (before
the Anthropic-surface check and before the legacy delegation) so they are
reachable regardless of which model is active. They read in-process stores only
- they MUST NOT and do NOT spawn codex or Headroom subprocesses.

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

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.adapters import codex_usage_store
from reverso.protocols.adapters.kimi import KimiAdapter, KimiOAuthAuth
from reverso.protocols.adapters.ollama import OllamaRuntime, build_ollama_runtime
from reverso.protocols.anthropic_app import (
    build_anthropic_adapters,
    build_anthropic_app,
    route_is_anthropic_surface,
)
from reverso.protocols.headroom_compression import (
    DEFAULT_HEADROOM_METRICS,
    HeadroomCompressionConfig,
)
from reverso.protocols.kimi_login import KimiLoginCoordinator
from reverso.protocols.responses_app import build_app, split_provider_path

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Kimi cleanup can spend two exit-grace windows reaping the child and two more
# finishing pipe drains. Keep the ASGI bound above that 20-second worst case.
_LIFESPAN_CLEANUP_TIMEOUT_SECONDS = 25.0
CODEX_DIRECT_BACKEND_ENV = "REVERSO_CODEX_DIRECT_BACKEND"
OPENAI_BACKEND_ENV = "REVERSO_OPENAI_BACKEND"
REVERSO_HOST_ENV = "REVERSO_HOST"


def codex_direct_backend_enabled(env: dict[str, str] | None = None) -> bool:
    """Return False only when the direct Codex backend is explicitly disabled."""
    source = os.environ if env is None else env
    if source.get(REVERSO_HOST_ENV, "127.0.0.1").strip() != "127.0.0.1":
        return False
    raw = source.get(CODEX_DIRECT_BACKEND_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def openai_backend_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True only for explicit local-loopback OpenAI pass-through opt-in."""
    source = os.environ if env is None else env
    if source.get(REVERSO_HOST_ENV, "127.0.0.1").strip() != "127.0.0.1":
        return False
    raw = source.get(OPENAI_BACKEND_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on", "openai"}


def build_adapters(
    env: dict[str, str] | None = None,
    *,
    kimi_auth: KimiOAuthAuth | None = None,
) -> dict[str, ProviderAdapter]:
    """Construct the real {prefix: adapter} registry for the first-party gateway.

    Adapters are imported here (not at module top) so the registry can be built
    without importing every provider's transitive dependencies until boot.
    """
    from reverso.protocols.adapters.auggie import AuggieAdapter
    from reverso.protocols.adapters.claude import ClaudeAdapter
    from reverso.protocols.adapters.copilot import CopilotAdapter
    from reverso.protocols.adapters.deepseek import DeepSeekAdapter

    adapters: dict[str, ProviderAdapter] = {
        "claude": ClaudeAdapter(),
        "copilot": CopilotAdapter(),
        "auggie": AuggieAdapter(),
        "deepseek": DeepSeekAdapter(),
        "kimi": KimiAdapter(auth=kimi_auth or KimiOAuthAuth()),
    }
    if codex_direct_backend_enabled(env):
        from reverso.protocols.adapters.codex import CodexOAuthAuth
        from reverso.protocols.adapters.codex_direct import (
            CodexDirectAdapter,
            HttpCodexDirectUpstream,
        )

        adapters["codex-direct"] = CodexDirectAdapter(
            auth=CodexOAuthAuth(), upstream=HttpCodexDirectUpstream()
        )
    if openai_backend_enabled(env):
        from reverso.protocols.adapters.openai import build_openai_pass_through_adapter

        openai_adapter = build_openai_pass_through_adapter()
        adapters["openai"] = openai_adapter
        adapters["openai-pass-through"] = openai_adapter
    return adapters


def _headroom_usage_summary() -> dict[str, Any]:
    """Return prompt-free aggregate Headroom usage metrics."""
    config = HeadroomCompressionConfig.from_env()
    return DEFAULT_HEADROOM_METRICS.snapshot(config)


def _headroom_usage_response() -> dict[str, Any]:
    """Return the standalone /usage/headroom response body."""
    return {
        "schema_version": 1,
        "provider": "headroom",
        "headroom": _headroom_usage_summary(),
    }


def _with_headroom_usage(body: dict[str, Any]) -> dict[str, Any]:
    """Add Headroom aggregate metrics to the existing /usage response."""
    enriched = dict(body)
    enriched["headroom"] = _headroom_usage_summary()
    return enriched


async def _send_json(send: Send, body: dict[str, Any], status: int = 200) -> None:
    """Send a JSON response over the ASGI ``send`` callable."""
    encoded = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(encoded)).encode("ascii")],
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded})


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
        anthropic_app: Callable[[Scope, Receive, Send], Awaitable[None]] | None = None,
        legacy_app: Callable[[Scope, Receive, Send], Awaitable[None]] | None = None,
        ollama_runtime: OllamaRuntime | None = None,
    ) -> None:
        self._kimi_login: KimiLoginCoordinator | None = None
        self._kimi_auth: KimiOAuthAuth | None = None
        self._ollama_runtime: OllamaRuntime | None = None
        self._accepting_http = True
        self._active_http: set[asyncio.Task[Any]] = set()
        self._http_quiesced = asyncio.Event()
        self._http_quiesced.set()
        self._close_lock = asyncio.Lock()
        self._lifespan_close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lifespan_cleanup_timeout_seconds = _LIFESPAN_CLEANUP_TIMEOUT_SECONDS
        if gateway is None:
            self._kimi_login = KimiLoginCoordinator()
            self._kimi_auth = KimiOAuthAuth(login_coordinator=self._kimi_login)
        else:
            self._ollama_runtime = ollama_runtime
        if gateway is None:
            adapters = build_adapters(kimi_auth=self._kimi_auth)
            self._gateway = build_app(adapters)
            self._ollama_runtime = ollama_runtime or build_ollama_runtime()
            adapters["ollama"] = self._ollama_runtime.adapter
            if anthropic_app is None:
                anthropic_adapters = build_anthropic_adapters(kimi_auth=self._kimi_auth)
                anthropic_adapters["ollama"] = self._ollama_runtime.adapter
                self._anthropic_app = build_anthropic_app(anthropic_adapters)
            else:
                self._anthropic_app = anthropic_app
        else:
            self._gateway = gateway
            self._anthropic_app = (
                anthropic_app
                if anthropic_app is not None
                else build_anthropic_app(kimi_auth=self._kimi_auth)
            )
        self._legacy_app = legacy_app

    async def close(self) -> None:
        """Close shared provider lifecycle resources before process shutdown."""
        async with self._close_lock:
            if self._closed:
                return
            self._accepting_http = False
            await self._http_quiesced.wait()
            if self._ollama_runtime is not None:
                await self._ollama_runtime.close()
            if self._kimi_login is not None:
                await self._kimi_login.close()
            self._closed = True

    def _admit_http(self) -> asyncio.Task[Any] | None:
        if not self._accepting_http:
            return None
        task = asyncio.current_task()
        if task is None:
            return None
        self._active_http.add(task)
        self._http_quiesced.clear()
        return task

    def _release_http(self, task: asyncio.Task[Any]) -> None:
        self._active_http.discard(task)
        if not self._active_http:
            self._http_quiesced.set()

    def _resolve_legacy(self) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        if self._legacy_app is None:
            from reverso.proxy.app import app as legacy_app

            self._legacy_app = legacy_app
        return self._legacy_app

    @staticmethod
    async def _legacy_lifespan_terminal(
        messages: asyncio.Queue[dict[str, Any]],
        legacy_task: asyncio.Task[None],
        *,
        phase: str,
    ) -> str:
        expected = {
            f"lifespan.{phase}.complete",
            f"lifespan.{phase}.failed",
        }
        while True:
            try:
                message = messages.get_nowait()
            except asyncio.QueueEmpty:
                if legacy_task.done():
                    try:
                        legacy_task.result()
                    except asyncio.CancelledError:
                        return "raised"
                    except Exception:  # noqa: BLE001 - ASGI lifespan boundary
                        return "raised"
                    return "returned"
                message_task = asyncio.create_task(messages.get())
                done, _ = await asyncio.wait(
                    {message_task, legacy_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if message_task not in done:
                    message_task.cancel()
                    await asyncio.gather(message_task, return_exceptions=True)
                    continue
                message = message_task.result()
            message_type = message.get("type")
            if message_type in expected:
                return str(message_type)

    async def _run_lifespan(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        legacy_receives: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        legacy_sends: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        legacy_task = asyncio.create_task(
            self._resolve_legacy()(
                scope,
                legacy_receives.get,
                legacy_sends.put,
            )
        )
        try:
            while True:
                message = await receive()
                message_type = message.get("type")
                if message_type == "lifespan.startup":
                    await legacy_receives.put(message)
                    terminal = await self._legacy_lifespan_terminal(
                        legacy_sends,
                        legacy_task,
                        phase="startup",
                    )
                    if terminal == "lifespan.startup.complete":
                        await send({"type": terminal})
                        continue
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "legacy application startup failed",
                        }
                    )
                    return
                if message_type != "lifespan.shutdown":
                    continue

                self._accepting_http = False
                await legacy_receives.put(message)
                terminal = await self._legacy_lifespan_terminal(
                    legacy_sends,
                    legacy_task,
                    phase="shutdown",
                )
                legacy_failed = terminal != "lifespan.shutdown.complete"
                cleanup_failed = False
                if self._lifespan_close_task is None:
                    self._lifespan_close_task = asyncio.create_task(self.close())
                close_task = self._lifespan_close_task
                try:
                    await asyncio.wait_for(
                        asyncio.shield(close_task),
                        timeout=self._lifespan_cleanup_timeout_seconds,
                    )
                except TimeoutError:
                    active_http = tuple(self._active_http)
                    for task in active_http:
                        task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*active_http, return_exceptions=True),
                            timeout=self._lifespan_cleanup_timeout_seconds,
                        )
                        await asyncio.wait_for(
                            asyncio.shield(close_task),
                            timeout=self._lifespan_cleanup_timeout_seconds,
                        )
                    except Exception:  # noqa: BLE001 - ASGI shutdown boundary
                        cleanup_failed = True
                except Exception:  # noqa: BLE001 - ASGI shutdown boundary
                    cleanup_failed = True

                if not legacy_failed and not cleanup_failed:
                    await send({"type": "lifespan.shutdown.complete"})
                else:
                    if legacy_failed and cleanup_failed:
                        failure = "legacy shutdown and provider cleanup failed"
                    elif legacy_failed:
                        failure = "legacy application shutdown failed"
                    else:
                        failure = "provider shutdown cleanup failed"
                    await send(
                        {
                            "type": "lifespan.shutdown.failed",
                            "message": failure,
                        }
                    )
                return
        finally:
            if not legacy_task.done():
                legacy_task.cancel()
            await asyncio.gather(legacy_task, return_exceptions=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "lifespan":
            await self._run_lifespan(scope, receive, send)
            return
        admitted: asyncio.Task[Any] | None = None
        if scope.get("type") == "http":
            admitted = self._admit_http()
            if admitted is None:
                await _send_json(send, {"error": "gateway shutting down"}, status=503)
                return
        try:
            await self._dispatch(scope, receive, send)
        finally:
            if admitted is not None:
                self._release_http(admitted)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path", ""))

            # GET /usage and /usage/headroom - serve local usage snapshots.
            # Handled BEFORE the Anthropic-surface check and before legacy
            # delegation so they are always reachable. Reads in-process stores
            # only - never spawns codex, Headroom, or provider subprocesses.
            if path == "/usage/headroom" and scope.get("method", "GET") == "GET":
                await _send_json(send, _headroom_usage_response())
                return

            if path == "/usage" and scope.get("method", "GET") == "GET":
                snapshot = codex_usage_store.get()
                if snapshot is not None:
                    body = snapshot
                else:
                    body = codex_usage_store.empty_response()
                await _send_json(send, _with_headroom_usage(body))
                return

            # The Anthropic Messages surface is checked BEFORE the Responses
            # split so /v1/messages and /<profile>/v1/messages (including
            # /claude/v1/messages, now served first-party by the Anthropic app
            # via the claude CLI under subscription OAuth, ADR 0009 superseding
            # ADR 0006 D2) route to the Anthropic app and never reach the legacy
            # LiteLLM app. Responses (/v1/responses, /v1/models) routing is left
            # byte-unchanged.
            if route_is_anthropic_surface(path):
                await self._anthropic_app(scope, receive, send)
                return
            routed = split_provider_path(path)
            if routed is not None:
                await self._gateway(scope, receive, send)
                return
        legacy_app = self._resolve_legacy()
        await legacy_app(scope, receive, send)


app = CompositionRoot()
