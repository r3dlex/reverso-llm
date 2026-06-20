"""First-party Anthropic Messages ASGI app skeleton (ADR 0006 D1/D3).

A second inbound dialect served from the same loopback port (127.0.0.1:64946) as
the OpenAI Responses surface. This module mirrors responses_app.py: it is pure
ASGI (no FastAPI) and does NOT import the legacy LiteLLM app or any ``litellm``
module; the LiteLLM quarantine guard test asserts that invariant.

G004 scope: routing/dispatch (G002), the NON-STREAMING Messages translation
(G003), and Anthropic-native SSE streaming (stream=true) are implemented. A
resolved /v1/messages POST is translated to a ResponsesRequest
(anthropic_translate), dispatched to the resolved adapter, and mapped back to
Anthropic shapes. Non-streaming returns the message body (HTTP 200); streaming
pipes adapter.stream_response through the pure anthropic_stream mapper and writes
each Anthropic event as ``event: <type>\ndata: <json>\n\n``. The mapper uses a
peek-first protocol: the 200 text/event-stream header is held until the first
upstream event is in hand, so a connect/auth/setup failure returns a secret-free
502 JSON error envelope. A mid-stream failure (after the 200 is committed) becomes
an in-band terminal Anthropic ``error`` event. Capability gating / feature
rejection (G005) and count_tokens / /v1/models (G006) remain clearly-marked stubs
here.

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
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.anthropic_stream import responses_sse_to_anthropic
from reverso.protocols.anthropic_translate import (
    anthropic_request_to_responses,
    responses_envelope_to_anthropic,
)
from reverso.protocols.replay import new_message_id
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    resolve_anthropic_backend,
)

logger = logging.getLogger(__name__)

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


def _parse_body(body: bytes) -> dict[str, Any] | None:
    """Decode a Messages JSON body into a dict, or None when malformed.

    A missing or malformed body, or a non-object JSON value, yields None, which
    drives the not_found_error 404 on the auto-routing path (no model to resolve).
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _model_from_payload(payload: dict[str, Any] | None) -> str | None:
    """Extract the requested ``model`` from a parsed Messages body, or None."""
    if payload is None:
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


async def _start_anthropic_stream(send: Send, *, anthropic_version: str) -> None:
    """Commit the 200 text/event-stream header for an Anthropic SSE response.

    Echoes the resolved anthropic-version (default when omitted), mirroring the
    JSON path; the header is sent only once, on the first Anthropic event.
    """
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
                (b"anthropic-version", anthropic_version.encode("ascii")),
            ],
        }
    )


def _anthropic_sse_bytes(event: dict[str, Any]) -> bytes:
    """Encode an Anthropic event dict as wire bytes (event + data + blank line).

    The SSE event name is the event's own ``type`` field, matching the Anthropic
    streaming grammar (``event: <type>\\ndata: <json>\\n\\n``).
    """
    event_type = str(event.get("type", "message"))
    data = json.dumps(event, separators=(",", ":")).encode("utf-8")
    return b"event: " + event_type.encode("utf-8") + b"\ndata: " + data + b"\n\n"


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


def _safe_backend_error_message(exc: Exception) -> str:
    """Return a secret-free backend-failure string for the 502 envelope.

    Only the exception class name is surfaced: a backend adapter's exception may
    carry an upstream URL, status echo, or other internal detail, so the message
    text never includes the exception's own payload (mirrors
    responses_app._safe_error_message).
    """
    return f"upstream backend error ({type(exc).__name__})"


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

        # Read and decode the body: the auto-routing path resolves the requested
        # model, and the full payload is translated into a ResponsesRequest.
        body = await _read_body(receive)
        payload = _parse_body(body)
        model = _model_from_payload(payload)

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

        # payload is non-None here: an auto-routed request without a parseable
        # body cannot resolve a model (backend would be None), and a pinned
        # prefix request still needs a body to translate. Guard defensively.
        if payload is None:
            await _send_error(
                send,
                400,
                "invalid_request_error",
                "request body must be a JSON object",
                anthropic_version=anthropic_version,
            )
            return

        if payload.get("stream") is True:
            await self._handle_streaming(
                backend, payload, send, anthropic_version=anthropic_version
            )
            return

        await self._handle_nonstreaming(
            backend, payload, send, anthropic_version=anthropic_version
        )

    async def _handle_nonstreaming(
        self,
        backend: str,
        payload: dict[str, Any],
        send: Send,
        *,
        anthropic_version: str,
    ) -> None:
        """Translate, dispatch, and map back a non-streaming Messages request.

        The payload is mapped to a ResponsesRequest, dispatched to the resolved
        adapter's create_response, and the ResponseEnvelope is mapped back to an
        Anthropic message body (HTTP 200). A backend failure becomes a secret-free
        502 Anthropic error envelope; the request is never silently dropped.
        """
        adapter = self._adapters[backend]
        request = anthropic_request_to_responses(payload)
        try:
            envelope = await adapter.create_response(request)
        except Exception as exc:  # noqa: BLE001 - any backend failure -> 502
            logger.warning(
                "anthropic backend %s create failed: %s", backend, type(exc).__name__
            )
            await _send_error(
                send,
                502,
                "api_error",
                _safe_backend_error_message(exc),
                anthropic_version=anthropic_version,
            )
            return
        await _send_json(
            send,
            200,
            responses_envelope_to_anthropic(envelope),
            anthropic_version=anthropic_version,
        )

    async def _handle_streaming(
        self,
        backend: str,
        payload: dict[str, Any],
        send: Send,
        *,
        anthropic_version: str,
    ) -> None:
        """Stream an Anthropic SSE response over the pure anthropic_stream mapper.

        The payload is translated to a ResponsesRequest, the resolved adapter's
        stream_response is piped through responses_sse_to_anthropic, and each
        Anthropic event is written as ``event: <type>\\ndata: <json>\\n\\n``.

        The mapper uses a peek-first protocol: it awaits the first upstream event
        before yielding message_start, so a connect/auth/setup failure on the
        first __anext__ propagates out of the mapper without any event having been
        yielded. The 200 text/event-stream header is held until the mapper yields
        its first event, so:
          - Pre-stream failure (exception before the first yield): the header is
            still uncommitted; this method returns a secret-free 502 JSON envelope.
          - Empty upstream (zero Responses events): the mapper yields message_start
            etc. without error; a 200 stream with the minimal terminal sequence.
          - Mid-stream failure (exception after the first yield): the 200 header is
            already committed; an in-band terminal Anthropic error event is the
            only safe channel (emitted by the mapper itself).
        """
        adapter = self._adapters[backend]
        request = anthropic_request_to_responses(payload)
        message_id = new_message_id()
        anthropic_events = responses_sse_to_anthropic(
            adapter.stream_response(request),
            model=request.model,
            message_id=message_id,
        )
        started = False
        try:
            async for event in anthropic_events:
                if not started:
                    await _start_anthropic_stream(
                        send, anthropic_version=anthropic_version
                    )
                    started = True
                await send(
                    {
                        "type": "http.response.body",
                        "body": _anthropic_sse_bytes(event),
                        "more_body": True,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - any failure must not crash the app
            logger.warning(
                "anthropic stream %s failed: %s", backend, type(exc).__name__
            )
            if not started:
                # Pre-first-event failure: the response line is still uncommitted,
                # so return a structured non-streaming 502 envelope.
                await _send_error(
                    send,
                    502,
                    "api_error",
                    _safe_backend_error_message(exc),
                    anthropic_version=anthropic_version,
                )
                return
            # Post-commit failure: emit an in-band terminal Anthropic error event.
            await send(
                {
                    "type": "http.response.body",
                    "body": _anthropic_sse_bytes(
                        build_anthropic_error(
                            "api_error", _safe_backend_error_message(exc)
                        )
                    ),
                    "more_body": True,
                }
            )
        if not started:
            # The mapper always yields at least message_start, so this is
            # defensive; commit the header so the client sees a valid stream.
            await _start_anthropic_stream(send, anthropic_version=anthropic_version)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

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
