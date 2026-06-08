"""Add Codex model-refresh compatibility fields to LiteLLM model lists."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _is_models_path(path: str) -> bool:
    return path.rstrip("/").endswith("/models")


def _is_codex_refresh(scope: Scope) -> bool:
    query = scope.get("query_string", b"")
    if isinstance(query, str):
        query = query.encode("utf-8")
    return b"client_version=" in query


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(
        key.lower() == b"content-type" and b"json" in value.lower()
        for key, value in headers
    )


def _without_content_length(
    headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    return [(key, value) for key, value in headers if key.lower() != b"content-length"]


def _with_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    filtered = _without_content_length(headers)
    filtered.append((b"content-length", str(length).encode("ascii")))
    return filtered


def _normalize_models_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict) or "models" in payload:
        return body
    data = payload.get("data")
    if not isinstance(data, list):
        return body
    models: list[dict[str, Any]] = []
    normalized = dict(payload)
    normalized["models"] = models
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


class CodexModelsCompatMiddleware:
    """Expose a minimal Codex-refresh field without replacing OpenAI model data.

    Codex refreshes model metadata from /models and expects a top-level
    ``models`` field using Codex's private schema. Reverso keeps LiteLLM's
    OpenAI-compatible ``data`` list as the canonical model response and adds an
    empty ``models`` list only so refresh decoding succeeds. Profile config still
    supplies the GPT-level model names that Reverso routes internally.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or not _is_models_path(str(scope.get("path", "")))
            or not _is_codex_refresh(scope)
        ):
            await self.app(scope, receive, send)
            return

        is_json_response = False
        start_message: dict[str, Any] | None = None
        body_parts: list[bytes] = []

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal is_json_response, start_message
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                is_json_response = _is_json(headers)
                start_message = dict(message)
                if is_json_response:
                    start_message["headers"] = _without_content_length(headers)
                return
            if message.get("type") != "http.response.body" or not is_json_response:
                if start_message is not None:
                    await send(start_message)
                    start_message = None
                await send(message)
                return
            body_parts.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = _normalize_models_body(b"".join(body_parts))
            if start_message is not None:
                start_message["headers"] = _with_content_length(
                    list(start_message.get("headers", [])), len(body)
                )
                await send(start_message)
                start_message = None
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, wrapped_send)
        if start_message is not None:
            await send(start_message)
