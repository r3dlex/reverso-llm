"""First-party Anthropic Messages ASGI app skeleton (ADR 0006 D1/D3).

A second inbound dialect served from the same loopback port (127.0.0.1:64946) as
the OpenAI Responses surface. This module mirrors responses_app.py: it is pure
ASGI (no FastAPI) and does NOT import the legacy LiteLLM app or any ``litellm``
module; the LiteLLM quarantine guard test asserts that invariant.

G002 scope: routing and dispatch are FULLY implemented; the actual Messages
translation is a clearly-marked stub (see _create_stub) deferred to G003.
Streaming (G004), capability gating (G005), and count_tokens/models (G006) are
NOT implemented here.

Routing (ADR 0006 D3):
  - POST /v1/messages: resolve the requested model to a backend through the
    single authority (surface_registry). An unknown model OR any claude model
    returns HTTP 404 with the Anthropic not_found_error envelope (claude is
    excluded for the D2 circularity reason). A resolved backend reaches the
    create stub.
  - Per-profile prefixes /copilot/v1/messages, /deepseek/v1/messages, and
    /auggie/v1/messages pin the named backend and bypass model auto-resolution.
  - /claude/v1/messages is claimed by this surface so a claude-pointed client
    gets an Anthropic-shaped 404, but resolution still yields None and the
    request is served the not_found_error 404 here, never delegated to legacy.
  - A missing anthropic-version header defaults to "2023-06-01" and is echoed on
    the response; it is never a 400.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    resolve_anthropic_backend,
)

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Default Anthropic API version echoed when the client omits anthropic-version.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# The Anthropic-surface backends with optional per-profile path prefixes. claude
# is intentionally a prefix this surface CLAIMS (route_is_anthropic_surface) but
# never a backend it serves: a /claude/v1/messages request is routed here and
# returns the Anthropic not_found_error 404 rather than reaching the legacy app.
_ANTHROPIC_SURFACE_BACKENDS = SURFACE_BACKENDS["anthropic"]
_PROFILE_PREFIXES = frozenset(_ANTHROPIC_SURFACE_BACKENDS | {"claude"})

# The Messages local path this surface serves (G002 routes only /v1/messages;
# count_tokens and models are G006).
_MESSAGES_PATH = "/v1/messages"


@dataclass(frozen=True)
class AnthropicRoute:
    """A request split into an optional profile prefix and the Messages path.

    ``profile`` is None for the bare /v1/messages auto-routing path, or the named
    backend (copilot/deepseek/auggie/claude) for a /<profile>/v1/messages path.
    """

    profile: str | None
    path: str


def split_anthropic_path(path: str) -> AnthropicRoute | None:
    """Return an AnthropicRoute for an Anthropic-surface path, else None.

    Matches the bare /v1/messages auto-routing path and the per-profile
    /<profile>/v1/messages paths for this surface's prefixes (copilot, deepseek,
    auggie, and the claimed-but-never-served claude).
    """
    stripped = path.rstrip("/") or "/"
    if stripped == _MESSAGES_PATH:
        return AnthropicRoute(profile=None, path=_MESSAGES_PATH)
    parts = stripped.split("/", 3)
    if len(parts) < 4:
        return None
    _, profile, version, rest = parts
    # Normalize profile to lowercase so /CLAUDE/v1/messages and /Claude/v1/messages
    # are claimed by this surface and receive the Anthropic not_found_error 404,
    # mirroring surface_registry model normalization (MINOR-1).
    profile = profile.lower()
    if profile not in _PROFILE_PREFIXES or version != "v1":
        return None
    if f"/{rest}" != _MESSAGES_PATH[len("/v1") :]:
        # Only /v1/messages (not count_tokens/models) is served in G002.
        return None
    return AnthropicRoute(profile=profile, path=_MESSAGES_PATH)


def route_is_anthropic_surface(path: str) -> bool:
    """Whether ``path`` belongs to the Anthropic Messages surface.

    The composition root calls this BEFORE the Responses split so /v1/messages
    and /<profile>/v1/messages (including /claude/v1/messages) route here.
    """
    return split_anthropic_path(path) is not None


def build_anthropic_error(error_type: str, message: str) -> dict[str, Any]:
    """Build the Anthropic error envelope (ADR 0006 D3).

    Shape: {"type": "error", "error": {"type": <error_type>, "message": <msg>}}.
    """
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _anthropic_version_from_headers(headers: list[tuple[bytes, bytes]]) -> str:
    """Return the client anthropic-version, or the default when absent.

    A missing header defaults to DEFAULT_ANTHROPIC_VERSION and is never a 400.
    """
    for key, value in headers:
        if key.lower() == b"anthropic-version":
            decoded = value.decode("utf-8", "replace").strip()
            if decoded:
                return decoded
    return DEFAULT_ANTHROPIC_VERSION


def _model_from_body(body: bytes) -> str | None:
    """Extract the requested ``model`` from a Messages body, or None.

    G002 parses only the model id (for auto-routing); G003 maps the full request.
    A malformed body or a missing model yields None, which drives a 404.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model if isinstance(model, str) else None


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, Any],
    *,
    anthropic_version: str,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                # Echo the resolved anthropic-version (default when omitted).
                (b"anthropic-version", anthropic_version.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_error(
    send: Send,
    status: int,
    error_type: str,
    message: str,
    *,
    anthropic_version: str,
) -> None:
    await _send_json(
        send,
        status,
        build_anthropic_error(error_type, message),
        anthropic_version=anthropic_version,
    )


def _create_stub(backend: str, anthropic_version: str) -> dict[str, Any]:
    """Clearly-marked G002 placeholder for a resolved Messages create request.

    TODO(G003): replace with the real Anthropic Messages translation that maps the
    request to a ResponsesRequest, calls the resolved adapter's create_response,
    and maps the ResponseEnvelope back to a Messages response. Until then a
    resolved backend returns a structured Anthropic not_implemented error so no
    request is silently accepted with no effect.
    """
    return build_anthropic_error(
        "not_implemented",
        # The backend is named so the stub is observably routed, not generic.
        f"Anthropic Messages create is not implemented yet for backend "
        f"{backend!r} (G003); request routed on anthropic-version "
        f"{anthropic_version}.",
    )


class AnthropicMessagesApp:
    """ASGI app routing first-party Anthropic Messages traffic (G002 skeleton).

    ``adapters`` maps Anthropic-surface backend names (copilot/deepseek/auggie) to
    objects satisfying the frozen ProviderAdapter Protocol. The constructor
    REJECTS a claude adapter: claude is excluded from the Anthropic surface (ADR
    0006 D2), so it must never be injectable as a backend here.
    """

    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
        if "claude" in adapters:
            raise ValueError(
                "claude is excluded from the Anthropic surface (ADR 0006 D2); "
                "the Anthropic app must not be given a claude adapter"
            )
        unknown = set(adapters) - _ANTHROPIC_SURFACE_BACKENDS
        if unknown:
            raise ValueError(
                f"unsupported Anthropic backend(s): {sorted(unknown)}; "
                f"allowed: {sorted(_ANTHROPIC_SURFACE_BACKENDS)}"
            )
        self._adapters = dict(adapters)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return
        headers = scope.get("headers", [])
        anthropic_version = _anthropic_version_from_headers(headers)
        route = split_anthropic_path(str(scope.get("path", "")))
        if route is None:
            await _send_error(
                send,
                404,
                "not_found_error",
                "not found",
                anthropic_version=anthropic_version,
            )
            return
        method = str(scope.get("method", "GET")).upper()
        if method != "POST":
            await _send_error(
                send,
                404,
                "not_found_error",
                "not found",
                anthropic_version=anthropic_version,
            )
            return

        # Read the body so the auto-routing path can resolve the requested model.
        # G002 parses only the model id for routing; G003 translates the rest.
        body = await _read_body(receive)
        model = _model_from_body(body)

        backend = self._resolve_backend(route, model)
        if backend is None:
            # Unknown model OR a claude model (auto path), or the claimed
            # /claude/v1/messages prefix: Anthropic not_found_error 404 envelope.
            await _send_error(
                send,
                404,
                "not_found_error",
                "model not found on the Anthropic surface",
                anthropic_version=anthropic_version,
            )
            return

        await _send_json(
            send,
            200,
            _create_stub(backend, anthropic_version),
            anthropic_version=anthropic_version,
        )

    def _resolve_backend(self, route: AnthropicRoute, model: str | None) -> str | None:
        """Resolve the backend for a route, or None to yield a 404.

        Per-profile prefixes pin the named backend and bypass model resolution,
        EXCEPT /claude which is claimed by this surface but never served (returns
        None -> 404). The bare /v1/messages path auto-resolves the requested
        model through the single authority, which fails closed for claude and
        unknown models.
        """
        if route.profile is not None:
            if route.profile == "claude":
                return None
            if route.profile in self._adapters:
                return route.profile
            return None
        backend = resolve_anthropic_backend(model)
        if backend is not None and backend in self._adapters:
            return backend
        return None


def build_anthropic_adapters() -> dict[str, ProviderAdapter]:
    """Construct ONLY the Anthropic-surface backends (never a ClaudeAdapter).

    Mirrors reverso.proxy.compose.build_adapters but constructs only copilot,
    deepseek, and auggie; claude is excluded from the Anthropic surface (ADR 0006
    D2). Adapters are imported here (not at module top) so the registry can be
    built without importing every provider's transitive dependencies until boot.
    """
    from reverso.protocols.adapters.auggie import AuggieAdapter
    from reverso.protocols.adapters.copilot import CopilotAdapter
    from reverso.protocols.adapters.deepseek import DeepSeekAdapter

    return {
        "copilot": CopilotAdapter(),
        "deepseek": DeepSeekAdapter(),
        "auggie": AuggieAdapter(),
    }


def build_anthropic_app(
    adapters: dict[str, ProviderAdapter] | None = None,
) -> AnthropicMessagesApp:
    """Build the Anthropic Messages app from a {backend: adapter} registry.

    Defaults to the real Anthropic-surface backends from build_anthropic_adapters.
    """
    if adapters is None:
        adapters = build_anthropic_adapters()
    return AnthropicMessagesApp(adapters)
