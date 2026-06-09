"""First-party Responses ASGI app for the Claude and Copilot provider paths.

Serves both providers from one loopback port (127.0.0.1:64946) via path
prefixes /claude and /copilot (ADR 0002 D1/D2). Adapters implementing the
frozen ProviderAdapter Protocol are injected by prefix through build_app, so
Lanes B and C plug in without touching this module. This app MUST NOT import
reverso.proxy.app; LiteLLM is quarantined for these paths and a runtime guard
test asserts the legacy app never handles a /claude or /copilot request.

The copilot prefix is net-new and owned here; the legacy
reverso.proxy.profile_routing.PROVIDER_PREFIXES is intentionally not mutated.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.middleware import (
    encode_sse_event,
    models_with_codex_refresh,
    normalize_request_payload,
    strip_think_json,
)

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

BIND_HOST = "127.0.0.1"
BIND_PORT = 64946

# Net-new in the first-party app. NOT the legacy PROVIDER_PREFIXES.
APP_PROVIDER_PREFIXES = frozenset({"claude", "copilot"})

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


async def _handle_create_response(
    adapter: ProviderAdapter,
    payload: dict[str, Any],
    send: Send,
) -> None:
    normalized = normalize_request_payload(payload)
    request = ResponsesRequest.from_payload(normalized)
    if request.stream:
        await _stream(adapter, request, send)
        return
    envelope = await adapter.create_response(request)
    await _send_json(send, 200, _envelope_to_payload(envelope))


async def _stream(
    adapter: ProviderAdapter,
    request: ResponsesRequest,
    send: Send,
) -> None:
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
    saw_done = False
    async for event in adapter.stream_response(request):
        chunk = _sse_event_bytes(event)
        if _DONE_EVENT.strip() in chunk:
            saw_done = True
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
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
    """ASGI app routing /claude and /copilot Responses traffic to adapters."""

    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
        unknown = set(adapters) - APP_PROVIDER_PREFIXES
        if unknown:
            raise ValueError(
                f"unsupported provider prefix(es): {sorted(unknown)}; "
                f"allowed: {sorted(APP_PROVIDER_PREFIXES)}"
            )
        self._adapters = dict(adapters)

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
            await _handle_create_response(adapter, payload, send)
            return

        if method == "GET" and local_no_slash.endswith("/v1/models"):
            models = await adapter.list_models()
            await _send_json(send, 200, _models_to_payload(models))
            return

        if method == "GET":
            response_id, is_input_items = _response_id_from_path(local)
            if response_id is not None and is_input_items:
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

    ``adapters`` maps provider prefixes ("claude", "copilot") to objects
    satisfying the ProviderAdapter Protocol. Lanes B and C inject their adapters
    here; the app holds no provider internals.
    """
    return ResponsesGatewayApp(adapters)
