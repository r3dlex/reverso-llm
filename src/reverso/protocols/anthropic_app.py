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
rejection (G005) is enforced before dispatch. The two auxiliary endpoints (G006)
are implemented: POST /v1/messages/count_tokens returns a documented word-count
APPROXIMATION of input_tokens (not a real tokenizer), and the bare GET /v1/models
returns the Anthropic-shaped listing of the surface_registry Anthropic-surface
model set (now including claude rows, ADR 0008).

Routing (ADR 0006 D3, ADR 0008):
  - POST /v1/messages: resolve the requested model to a backend through the
    single authority (surface_registry). A claude model now resolves to the
    claude backend and is SERVED via the local claude CLI (ADR 0008); only an
    unknown model returns HTTP 404 with the Anthropic not_found_error envelope.
  - POST /v1/messages/count_tokens: resolve the backend the same way (unknown
    model -> 404), then return {"input_tokens": N} as a documented word-count
    approximation. No capability gating is applied (it is a pre-flight sizing
    call, not a served feature).
  - GET /v1/models: the BARE path only. Returns the Anthropic-shaped listing of
    the Anthropic-surface model set; no per-profile /<provider>/v1/models is
    claimed here (that listing belongs to the Responses gateway).
  - Per-profile prefixes /copilot, /deepseek, /auggie, /claude pin the named
    backend on the Messages family and bypass model auto-resolution.
  - /claude/v1/messages[/count_tokens] resolves to the claude backend and is
    served first-party via the claude CLI (ADR 0008), never delegated to legacy.
  - A missing anthropic-version header defaults to "2023-06-01" and is echoed on
    the response; it is never a 400.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.anthropic_feature_gate import (
    AnthropicFeatureRejected,
    gate_anthropic_features,
)
from reverso.protocols.anthropic_stream import responses_sse_to_anthropic
from reverso.protocols.anthropic_translate import (
    anthropic_request_to_responses,
    responses_envelope_to_anthropic,
)
from reverso.protocols.replay import build_prompt, estimate_usage, new_message_id
from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    list_anthropic_surface_models,
    resolve_anthropic_backend,
)

logger = logging.getLogger(__name__)

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Default Anthropic API version echoed when the client omits anthropic-version.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Fixed surface epoch for GET /v1/models created_at. The Anthropic-surface models
# are first-party CLI-backed and carry no provider creation timestamp, so the
# listing reports a stable ISO 8601 value (the ADR 0006 acceptance date) rather
# than a per-request now(), keeping the listing deterministic and cache-friendly.
_MODELS_CREATED_AT = "2026-06-20T00:00:00Z"

# The Anthropic-surface backends with optional per-profile path prefixes. claude
# is now part of SURFACE_BACKENDS["anthropic"] and is served first-party via the
# claude CLI (ADR 0008), so /claude/v1/messages reaches the claude backend.
_ANTHROPIC_SURFACE_BACKENDS = SURFACE_BACKENDS["anthropic"]
_PROFILE_PREFIXES = frozenset(_ANTHROPIC_SURFACE_BACKENDS)

# The Anthropic local paths this surface serves. G002 routed only /v1/messages;
# G006 adds /v1/messages/count_tokens (a pre-flight sizing POST) and the bare
# GET /v1/models listing.
_MESSAGES_PATH = "/v1/messages"
_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
_MODELS_PATH = "/v1/models"

# Route kinds: which Anthropic endpoint a request targets.
_KIND_MESSAGES = "messages"
_KIND_COUNT_TOKENS = "count_tokens"
_KIND_MODELS = "models"

# The Messages-family local paths (everything but /v1/models) that support an
# optional per-profile prefix and require a POST.
_MESSAGES_KIND_BY_REST = {
    "messages": _KIND_MESSAGES,
    "messages/count_tokens": _KIND_COUNT_TOKENS,
}


@dataclass(frozen=True)
class AnthropicRoute:
    """A request split into a route kind and an optional profile prefix.

    ``kind`` is one of messages / count_tokens / models. ``profile`` is None for
    the bare auto-routing paths, or the named backend
    (copilot/deepseek/auggie/codex/claude) for a /<profile>/v1/... path.
    /v1/models is a bare-only listing (no profile, GET).
    """

    kind: str
    profile: str | None
    path: str


def split_anthropic_path(path: str) -> AnthropicRoute | None:
    """Return an AnthropicRoute for an Anthropic-surface path, else None.

    Matches the bare /v1/messages and /v1/messages/count_tokens auto-routing
    paths, the per-profile /<profile>/v1/messages[/count_tokens] paths for this
    surface's prefixes (copilot, deepseek, auggie, codex, and the now-served
    claude, ADR 0008), and the bare GET listing /v1/models. /v1/models is bare-only: the
    per-profile /<provider>/v1/models listing belongs to the Responses gateway and
    is intentionally NOT claimed here.
    """
    stripped = path.rstrip("/") or "/"
    if stripped == _MESSAGES_PATH:
        return AnthropicRoute(kind=_KIND_MESSAGES, profile=None, path=_MESSAGES_PATH)
    if stripped == _COUNT_TOKENS_PATH:
        return AnthropicRoute(
            kind=_KIND_COUNT_TOKENS, profile=None, path=_COUNT_TOKENS_PATH
        )
    if stripped == _MODELS_PATH:
        return AnthropicRoute(kind=_KIND_MODELS, profile=None, path=_MODELS_PATH)
    # Try a per-profile Messages-family path: /<profile>/v1/messages[/count_tokens].
    profile_parts = stripped.split("/", 3)
    if len(profile_parts) == 4:
        _, profile, profile_version, profile_rest = profile_parts
        # Normalize profile to lowercase so /CLAUDE/... is claimed by this surface
        # and receives the Anthropic not_found_error 404, mirroring
        # surface_registry model normalization (MINOR-1).
        profile = profile.lower()
        kind = _MESSAGES_KIND_BY_REST.get(profile_rest)
        if (
            profile in _PROFILE_PREFIXES
            and profile_version == "v1"
            and kind is not None
        ):
            target = _MESSAGES_PATH if kind == _KIND_MESSAGES else _COUNT_TOKENS_PATH
            return AnthropicRoute(kind=kind, profile=profile, path=target)
    return None


def route_is_anthropic_surface(path: str) -> bool:
    """Whether ``path`` belongs to the Anthropic Messages surface.

    The composition root calls this BEFORE the Responses split so /v1/messages,
    /v1/messages/count_tokens, the bare /v1/models, and the per-profile
    /<profile>/v1/messages[/count_tokens] paths (including the now-served claude
    prefix, ADR 0008) route here. The bare /v1/models is claimed here, so it no longer falls
    through to the legacy app; the per-profile /<provider>/v1/models stays with the
    Responses gateway (split_anthropic_path does not match it).
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


# Maximum cumulative request body size accepted by _read_body (16 MiB). A body
# that exceeds this cap is rejected with a structured 413 Anthropic error envelope
# rather than buffered unbounded into memory.
_MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB


class _BodyTooLargeError(Exception):
    """Raised by _read_body when the cumulative body exceeds _MAX_BODY_BYTES."""


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise _BodyTooLargeError(f"request body exceeds {_MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
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

    ``adapters`` maps Anthropic-surface backend names
    (copilot/deepseek/auggie/codex/claude) to objects satisfying the frozen
    ProviderAdapter Protocol. claude is now a permitted backend, served via the
    local claude CLI (ADR 0008); only backends absent from SURFACE_BACKENDS are
    rejected.
    """

    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
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

        # GET /v1/models is the only GET route this surface serves; it takes no
        # body and resolves no per-request backend (it is a static listing of the
        # Anthropic-surface model set). The Messages family (/v1/messages and
        # /v1/messages/count_tokens) is POST-only.
        if route.kind == _KIND_MODELS:
            if method != "GET":
                await _send_error(
                    send,
                    404,
                    "not_found_error",
                    "not found",
                    anthropic_version=anthropic_version,
                )
                return
            await self._handle_models(send, anthropic_version=anthropic_version)
            return

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
        try:
            body = await _read_body(receive)
        except _BodyTooLargeError:
            await _send_error(
                send,
                413,
                "invalid_request_error",
                "request body too large",
                anthropic_version=anthropic_version,
            )
            return
        payload = _parse_body(body)
        model = _model_from_payload(payload)

        backend = self._resolve_backend(route, model)
        if backend is None:
            # Unknown model (auto path) or a pinned prefix with no matching
            # adapter: not_found_error 404. This is consistent across
            # /v1/messages and /v1/messages/count_tokens so a pre-flight sizing
            # call fails the same way the real call would. claude ids now resolve
            # to the claude backend (ADR 0008) and are served, not 404'd.
            await _send_error(
                send,
                404,
                "not_found_error",
                "model not found on the Anthropic surface",
                anthropic_version=anthropic_version,
            )
            return

        # count_tokens is a pre-flight SIZING call: it resolves the backend (so an
        # unknown/claude model 404s exactly as /v1/messages would) but does NOT
        # apply per-backend capability gating. Gating rejects feature x backend
        # cells that cannot be SERVED; sizing a prompt does not serve anything, so
        # a client may legitimately size a request before deciding to send it.
        if route.kind == _KIND_COUNT_TOKENS:
            if payload is None:
                await _send_error(
                    send,
                    400,
                    "invalid_request_error",
                    "request body must be a JSON object",
                    anthropic_version=anthropic_version,
                )
                return
            await self._handle_count_tokens(
                payload, send, anthropic_version=anthropic_version
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

        # Per-backend capability gating (ADR 0006 capability ceiling, G005). Runs
        # AFTER backend resolution and BEFORE the adapter is dispatched, on BOTH
        # the streaming and non-streaming paths: a streaming request that requests
        # an unsupported feature is rejected here with a 400 JSON body, before any
        # text/event-stream header is committed (never a 200 event-stream).
        try:
            gate_anthropic_features(payload, backend)
        except AnthropicFeatureRejected as rejected:
            # Use str(rejected) so the 400 message is rendered by the single
            # source in AnthropicFeatureRejected.__init__; the app never
            # re-formats it independently.
            await _send_error(
                send,
                400,
                "invalid_request_error",
                str(rejected),
                anthropic_version=anthropic_version,
            )
            return
        except RecursionError:
            # Last-resort guard: the _MAX_BLOCK_DEPTH cap in _scan_block_list is
            # the primary defence against unbounded recursion in the feature scan,
            # making this branch unreachable under normal conditions. This catch
            # defends against unforeseen deep-call paths elsewhere in gate or
            # translation logic so the structured Anthropic error contract is
            # never broken by a framework 500 regardless of the call source.
            await _send_error(
                send,
                400,
                "invalid_request_error",
                "request content is too deeply nested",
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
        finally:
            # Deterministically close the async generator so the upstream
            # stream/subprocess is released promptly on both success and
            # mid-stream-failure paths (Finding 3).
            await anthropic_events.aclose()
        if not started:
            # The mapper always yields at least message_start, so this is
            # defensive; commit the header so the client sees a valid stream.
            await _start_anthropic_stream(send, anthropic_version=anthropic_version)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_count_tokens(
        self,
        payload: dict[str, Any],
        send: Send,
        *,
        anthropic_version: str,
    ) -> None:
        """Return a documented word-count APPROXIMATION of input_tokens (AC7).

        This is NOT a real tokenizer. The Messages body is translated to a
        ResponsesRequest with the same translator the real call uses, the prompt
        is flattened via replay.build_prompt, and replay.estimate_usage counts
        whitespace-delimited words (ADR 0006: count_tokens is translated (approx),
        labeled an estimate in the docs, never represented as exact provider
        tokenization). The response is the Anthropic shape {"input_tokens": N}.
        """
        request = anthropic_request_to_responses(payload)
        prompt = build_prompt(request)
        # estimate_usage word-counts both prompt and output; only the input side
        # is meaningful for a pre-flight sizing call (there is no output yet).
        input_tokens = estimate_usage(prompt, "")["input_tokens"]
        await _send_json(
            send,
            200,
            {"input_tokens": input_tokens},
            anthropic_version=anthropic_version,
        )

    async def _handle_models(self, send: Send, *, anthropic_version: str) -> None:
        """Return the Anthropic-shaped /v1/models listing (AC8).

        Derived from the single surface_registry authority's Anthropic-surface
        model set, so the listing and the router never disagree; claude rows are
        now present and map to the claude backend (ADR 0008). The
        shape mirrors the Anthropic Models API: a ``data`` array of
        {"type":"model","id","display_name","created_at"} rows plus first_id /
        last_id / has_more. ``created_at`` is a fixed surface epoch (the models are
        first-party CLI-backed and have no provider creation timestamp); it is a
        stable ISO 8601 value rather than a per-request now() so the listing is
        deterministic and cache-friendly.
        """
        rows = list_anthropic_surface_models()
        data = [
            {
                "type": "model",
                "id": row["id"],
                "display_name": row["display_name"],
                "created_at": _MODELS_CREATED_AT,
            }
            for row in rows
        ]
        first_id = data[0]["id"] if data else None
        last_id = data[-1]["id"] if data else None
        await _send_json(
            send,
            200,
            {
                "data": data,
                "first_id": first_id,
                "last_id": last_id,
                "has_more": False,
            },
            anthropic_version=anthropic_version,
        )

    def _resolve_backend(self, route: AnthropicRoute, model: str | None) -> str | None:
        """Resolve the backend for a route, or None to yield a 404.

        Per-profile prefixes pin the named backend and bypass model resolution,
        including /claude which now resolves to the claude backend when a claude
        adapter is present (ADR 0008). The bare /v1/messages path auto-resolves the
        requested model through the single authority, which now resolves claude
        ids to the claude backend and still fails closed for unknown models.
        """
        if route.profile is not None:
            if route.profile in self._adapters:
                return route.profile
            return None
        backend = resolve_anthropic_backend(model)
        if backend is not None and backend in self._adapters:
            return backend
        return None


def build_anthropic_adapters() -> dict[str, ProviderAdapter]:
    """Construct the Anthropic-surface backends, including the claude adapter.

    Mirrors reverso.proxy.compose.build_adapters and constructs copilot,
    deepseek, auggie, codex, and claude. claude is now served on the Anthropic
    surface via the local claude CLI (ADR 0008, superseding ADR 0006 D2). codex
    is Anthropic-surface-ONLY (deliberately absent from compose.build_adapters,
    ADR 0007). Adapters are imported here (not at module top) so the registry can
    be built without importing every provider's transitive dependencies until boot.
    """
    from reverso.protocols.adapters.auggie import AuggieAdapter
    from reverso.protocols.adapters.claude import ClaudeAdapter
    from reverso.protocols.adapters.codex import CodexAdapter
    from reverso.protocols.adapters.copilot import CopilotAdapter
    from reverso.protocols.adapters.deepseek import DeepSeekAdapter

    return {
        "copilot": CopilotAdapter(),
        "deepseek": DeepSeekAdapter(),
        "auggie": AuggieAdapter(),
        "codex": CodexAdapter(),
        "claude": ClaudeAdapter(),
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
