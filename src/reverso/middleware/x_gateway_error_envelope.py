"""ASGI middleware that adds x_gateway to JSON error responses."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from reverso.middleware.x_gateway_callback import _infer_provider
from reverso.proxy.profile_routing import split_profile_path

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

_PROFILE_TO_PROVIDER = {
    "claude": "anthropic",
    "deepseek": "deepseek",
}


def _infer_provider_from_path(path: str) -> str:
    profile_path = split_profile_path(path)
    if profile_path is None:
        return "unknown"
    return _PROFILE_TO_PROVIDER.get(profile_path.profile, "unknown")


def _infer_provider_from_body(body: bytes) -> str:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    model = payload.get("model")
    return _infer_provider(model) if isinstance(model, str) else "unknown"


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type" and b"application/json" in value.lower():
            return True
    return False


def _with_content_length(headers: list[tuple[bytes, bytes]], length: int) -> list[tuple[bytes, bytes]]:
    filtered = [(key, value) for key, value in headers if key.lower() != b"content-length"]
    filtered.append((b"content-length", str(length).encode("ascii")))
    return filtered


def _add_x_gateway(body: bytes, provider: str) -> bytes:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict) or "x_gateway" in payload:
        return body
    payload["x_gateway"] = {
        "session_id": None,
        "observations": [],
        "provider": provider,
        "warnings": [],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _read_body(receive: Receive) -> tuple[bytes | None, bool]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            return None, True
        if message_type != "http.request":
            return b"".join(chunks), False
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks), False


def _receive_replay(body: bytes | None, disconnected: bool, source_receive: Receive) -> Receive:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if disconnected:
            return {"type": "http.disconnect"}
        if sent:
            return await source_receive()
        sent = True
        return {"type": "http.request", "body": body or b"", "more_body": False}

    return receive


class XGatewayErrorEnvelopeMiddleware:
    """Inject the Reverso response envelope into JSON error responses."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        body, disconnected = await _read_body(receive)
        provider = _infer_provider_from_path(str(scope.get("path", "")))
        if provider == "unknown" and body is not None:
            provider = _infer_provider_from_body(body)
        start_message: dict[str, Any] | None = None
        body_chunks: list[bytes] = []

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal start_message
            message_type = message.get("type")
            if message_type == "http.response.start":
                status = int(message.get("status", 200))
                headers = list(message.get("headers", []))
                if status >= 400 and _is_json(headers):
                    start_message = dict(message)
                    start_message["headers"] = headers
                    return
                await send(message)
                return

            if message_type == "http.response.body" and start_message is not None:
                body_chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                error_body = _add_x_gateway(b"".join(body_chunks), provider)
                start_message["headers"] = _with_content_length(start_message["headers"], len(error_body))
                await send(start_message)
                await send({"type": "http.response.body", "body": error_body, "more_body": False})
                return

            await send(message)

        await self.app(scope, _receive_replay(body, disconnected, receive), wrapped_send)
