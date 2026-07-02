"""First-party Responses ASGI app for the first-party provider paths.

Serves every first-party provider from one loopback port (127.0.0.1:64946) via
path prefixes /claude, /copilot, /auggie and /deepseek (ADR 0002 D1/D2, extended
by ADR 0003). Adapters implementing the frozen ProviderAdapter Protocol are
injected by prefix through build_app, so each provider lane plugs in without
touching this module. This app MUST NOT import reverso.proxy.app; LiteLLM is
quarantined for these paths and a runtime guard test asserts the legacy app
never handles a first-party request.

The auggie/copilot/deepseek prefixes are owned here; the legacy
reverso.proxy.profile_routing.PROVIDER_PREFIXES is intentionally not mutated, so
the composition root (reverso.proxy.compose) can route first-party traffic here
while still delegating everything else to the legacy LiteLLM app.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.feature_policy import (
    UnsupportedFeature,
    build_unsupported_payload,
    check_features,
    extract_features,
)
from reverso.protocols.middleware import (
    encode_sse_event,
    models_with_codex_refresh,
    normalize_request_payload,
    strip_think_json,
)
from reverso.protocols.headroom_compression import compress_responses_request
from reverso.protocols.replay import record_input_items
from reverso.proxy.profile_routing import (
    CURRENT_PROFILE_WORKSPACE,
    _workspace_from_body,
    _workspace_from_codex_turn_metadata,
)

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

BIND_HOST = "127.0.0.1"
BIND_PORT = 64946

# First-party prefixes served here. NOT the legacy PROVIDER_PREFIXES; the
# composition root routes these to this app and delegates the rest to legacy.
APP_PROVIDER_PREFIXES = frozenset({"claude", "copilot", "auggie", "deepseek"})

_DONE_EVENT = b"data: [DONE]\n\n"


class RoutedPath:
    """A request split into its provider prefix and provider-local path."""

    __slots__ = ("provider", "path")

    def __init__(self, provider: str, path: str) -> None:
        self.provider = provider
        self.path = path


def split_provider_path(path: str) -> RoutedPath | None:
    """Return (provider, /v1/...) for /<provider>/v1/... paths, else None.

    Reuses the prior-art split pattern from profile_routing.split_profile_path
    but matches only this app's own APP_PROVIDER_PREFIXES.
    """
    parts = path.split("/", 3)
    if len(parts) < 4:
        return None
    _, provider, version, rest = parts
    if provider not in APP_PROVIDER_PREFIXES or version != "v1":
        return None
    return RoutedPath(provider=provider, path=f"/v1/{rest}")


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


async def _send_json(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_error(send: Send, status: int, message: str) -> None:
    await _send_json(
        send, status, {"error": {"message": message, "type": "invalid_request_error"}}
    )


async def _send_server_error(send: Send, message: str) -> None:
    await _send_json(send, 502, {"error": {"message": message, "type": "server_error"}})


def _safe_error_message(exc: Exception) -> str:
    """Return a secret-free error string.

    Generic exception payloads may carry upstream URLs or internal details, so
    only the class name is surfaced by default. Provider exceptions may opt in
    to exposing a curated ``public_message`` with model-level diagnostics.
    """
    public_message = getattr(exc, "public_message", None)
    if isinstance(public_message, str) and public_message:
        return f"upstream provider error ({type(exc).__name__}): {public_message}"
    return f"upstream provider error ({type(exc).__name__})"


class _SendTracker:
    """Wraps an ASGI send to record whether a response has started.

    Lets the app fall back to a structured error only while the response line
    is still uncommitted; once headers are sent (e.g. mid-stream), the streaming
    path owns its own terminal failure event instead.
    """

    __slots__ = ("_send", "started")

    def __init__(self, send: Send) -> None:
        self._send = send
        self.started = False

    async def __call__(self, message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            self.started = True
        await self._send(message)


def _envelope_to_payload(envelope: ResponseEnvelope) -> dict[str, Any]:
    if envelope.raw:
        return strip_think_json(envelope.raw)
    payload: dict[str, Any] = {
        "id": envelope.id,
        "object": "response",
        "status": envelope.status,
        "model": envelope.model,
        "output": envelope.output,
    }
    if envelope.usage is not None:
        payload["usage"] = envelope.usage
    if envelope.previous_response_id is not None:
        payload["previous_response_id"] = envelope.previous_response_id
    return strip_think_json(payload)


def _models_to_payload(models: ModelList) -> dict[str, Any]:
    payload = {
        "object": models.object,
        "data": models.data,
        "models": models.models,
    }
    return models_with_codex_refresh(payload)


def _input_items_to_payload(items: InputItemList) -> dict[str, Any]:
    return {
        "object": items.object,
        "response_id": items.response_id,
        "data": items.data,
    }


def _sse_event_bytes(event: SSEEvent) -> bytes:
    if event.raw is not None:
        return event.raw
    return encode_sse_event(event.event, event.data)


def _response_id_from_sse_event(event: SSEEvent) -> str | None:
    """Extract a response id from a decoded Responses SSE event."""
    response = event.data.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    value = event.data.get("id")
    if isinstance(value, str) and value.startswith("resp_"):
        return value
    return None


async def _send_unsupported_feature(send: Send, provider: str, feature: str) -> None:
    await _send_json(send, 400, build_unsupported_payload(provider, feature))


async def _handle_create_response(
    adapter: ProviderAdapter,
    provider: str,
    payload: dict[str, Any],
    send: Send,
    *,
    workspace: str | None = None,
    remember_input_items: Callable[[str, ResponsesRequest], None] | None = None,
) -> None:
    # The gate inspects the raw payload (Codex-only fields preserved in extra)
    # so it can reject e.g. parallel_tool_calls on claude even though the Codex
    # normalizer would silently strip it before the adapter sees it.
    raw_request = ResponsesRequest.from_payload(payload)
    try:
        check_features(provider, extract_features(raw_request))
    except UnsupportedFeature as exc:
        await _send_unsupported_feature(send, exc.provider, exc.feature)
        return

    normalized = normalize_request_payload(payload)
    request = ResponsesRequest.from_payload(normalized)
    compression_outcome = await compress_responses_request(request)
    dispatch_request = compression_outcome.request
    token = CURRENT_PROFILE_WORKSPACE.set(workspace)
    try:
        if dispatch_request.stream:
            await _stream(
                adapter,
                provider,
                dispatch_request,
                send,
                original_request=raw_request,
                remember_input_items=remember_input_items,
            )
            return
        envelope = await adapter.create_response(dispatch_request)
    except UnsupportedFeature as exc:
        await _send_unsupported_feature(send, exc.provider, exc.feature)
        return
    finally:
        CURRENT_PROFILE_WORKSPACE.reset(token)
    if remember_input_items is not None:
        remember_input_items(envelope.id, raw_request)
    await _send_json(send, 200, _envelope_to_payload(envelope))


async def _start_stream(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
            ],
        }
    )


async def _emit_mid_stream_failure(send: Send, exc: Exception) -> None:
    """Terminate an already-200 stream with response.failed + [DONE]."""
    failure = encode_sse_event(
        "response.failed",
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "message": _safe_error_message(exc),
                    "type": "server_error",
                },
            },
        },
    )
    await send({"type": "http.response.body", "body": failure, "more_body": True})
    await send({"type": "http.response.body", "body": _DONE_EVENT, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _stream(
    adapter: ProviderAdapter,
    provider: str,
    request: ResponsesRequest,
    send: Send,
    *,
    original_request: ResponsesRequest | None = None,
    remember_input_items: Callable[[str, ResponsesRequest], None] | None = None,
) -> None:
    # The 200 header is held until the first event so a failure that happens
    # before any output (auth, connect, upstream non-2xx) can still return a
    # structured error instead of a truncated 200 stream. UnsupportedFeature
    # raised before the first event becomes the structured 400 body; raised
    # after a delta has been emitted it surfaces through the same mid-stream
    # response.failed + [DONE] contract as any other provider error.
    started = False
    saw_done = False
    recorded_input_items = False
    try:
        async for event in adapter.stream_response(request):
            if (
                not recorded_input_items
                and original_request is not None
                and remember_input_items is not None
            ):
                response_id = _response_id_from_sse_event(event)
                if response_id is not None:
                    remember_input_items(response_id, original_request)
                    recorded_input_items = True
            if not started:
                await _start_stream(send)
                started = True
            chunk = _sse_event_bytes(event)
            if _DONE_EVENT.strip() in chunk:
                saw_done = True
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
    except UnsupportedFeature as exc:
        if not started:
            await _send_unsupported_feature(send, exc.provider, exc.feature)
            return
        # Once the 200 header is committed the structured 400 body can no
        # longer reach the client, so surface a response.failed + [DONE] like
        # any other terminal error.
        await _emit_mid_stream_failure(send, exc)
        return
    except Exception as exc:  # noqa: BLE001 - a provider failure must not crash the app
        if not started:
            await _send_server_error(send, _safe_error_message(exc))
            return
        await _emit_mid_stream_failure(send, exc)
        return
    if not started:
        await _start_stream(send)
    if not saw_done:
        await send(
            {"type": "http.response.body", "body": _DONE_EVENT, "more_body": True}
        )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _response_id_from_path(local_path: str) -> tuple[str | None, bool]:
    """Parse /v1/responses/{id} and /v1/responses/{id}/input_items.

    Returns (response_id, is_input_items). response_id is None when the path is
    not a per-response route.
    """
    trimmed = local_path.strip("/")
    parts = trimmed.split("/")
    if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "responses":
        response_id = parts[2]
        is_input_items = len(parts) >= 4 and parts[3] == "input_items"
        return response_id, is_input_items
    return None, False


class ResponsesGatewayApp:
    """ASGI app routing first-party Responses traffic to per-prefix adapters."""

    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
        unknown = set(adapters) - APP_PROVIDER_PREFIXES
        if unknown:
            raise ValueError(
                f"unsupported provider prefix(es): {sorted(unknown)}; "
                f"allowed: {sorted(APP_PROVIDER_PREFIXES)}"
            )
        self._adapters = dict(adapters)
        self._input_items_lock = threading.Lock()
        self._original_input_items: dict[str, list[dict[str, Any]]] = {}

    def _remember_input_items(
        self, response_id: str, request: ResponsesRequest
    ) -> None:
        with self._input_items_lock:
            self._original_input_items[response_id] = record_input_items(request)

    def _get_original_input_items(self, response_id: str) -> InputItemList | None:
        with self._input_items_lock:
            items = self._original_input_items.get(response_id)
            if items is None:
                return None
            copied = [dict(item) for item in items]
        return InputItemList(response_id=response_id, data=copied)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return
        routed = split_provider_path(str(scope.get("path", "")))
        if routed is None:
            await _send_error(send, 404, "not found")
            return
        adapter = self._adapters.get(routed.provider)
        if adapter is None:
            await _send_error(send, 503, f"no adapter for provider {routed.provider!r}")
            return

        tracked = _SendTracker(send)
        try:
            await self._dispatch(adapter, scope, receive, tracked, routed)
        except Exception as exc:  # noqa: BLE001 - provider failures become a 502
            # A streamed response that already committed its 200 owns its own
            # terminal failure event; only synthesize an error response while
            # the status line is still uncommitted.
            if not tracked.started:
                await _send_server_error(tracked, _safe_error_message(exc))

    async def _dispatch(
        self,
        adapter: ProviderAdapter,
        scope: Scope,
        receive: Receive,
        send: Send,
        routed: RoutedPath,
    ) -> None:
        method = str(scope.get("method", "GET")).upper()
        local = routed.path
        local_no_slash = local.rstrip("/")

        if method == "POST" and local_no_slash.endswith("/v1/responses"):
            body = await _read_body(receive)
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                await _send_error(send, 400, "invalid JSON body")
                return
            if not isinstance(payload, dict):
                await _send_error(send, 400, "request body must be an object")
                return
            header_workspace = _workspace_from_codex_turn_metadata(
                scope.get("headers", [])
            )
            workspace = _workspace_from_body(body) or header_workspace
            await _handle_create_response(
                adapter,
                routed.provider,
                payload,
                send,
                workspace=workspace,
                remember_input_items=self._remember_input_items,
            )
            return

        if method == "GET" and local_no_slash.endswith("/v1/models"):
            models = await adapter.list_models()
            await _send_json(send, 200, _models_to_payload(models))
            return

        if method == "GET":
            response_id, is_input_items = _response_id_from_path(local)
            if response_id is not None and is_input_items:
                original = self._get_original_input_items(response_id)
                if original is not None:
                    await _send_json(send, 200, _input_items_to_payload(original))
                    return
                items = await adapter.list_input_items(response_id)
                await _send_json(send, 200, _input_items_to_payload(items))
                return
            if response_id is not None:
                envelope = await adapter.get_response(response_id)
                await _send_json(send, 200, _envelope_to_payload(envelope))
                return

        await _send_error(send, 404, "not found")


def build_app(adapters: dict[str, ProviderAdapter]) -> ResponsesGatewayApp:
    """Build the first-party Responses app from a {prefix: adapter} registry.

    ``adapters`` maps provider prefixes ("claude", "copilot", "auggie",
    "deepseek") to objects satisfying the ProviderAdapter Protocol. The
    composition root injects the adapters here; the app holds no provider
    internals.
    """
    return ResponsesGatewayApp(adapters)
