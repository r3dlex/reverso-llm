"""Guard Responses API SSE streams against missing completion events."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]


_RESPONSE_COMPLETED_MARKER = b'"type":"response.completed"'
_RESPONSE_COMPLETED_MARKER_SPACED = b'"type": "response.completed"'
_DONE_LINE = b"data: [DONE]"
_DONE_EVENT = b"data: [DONE]\n\n"


def _is_responses_path(path: str) -> bool:
    return path.rstrip("/").endswith("/responses")


def _is_sse(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type" and b"text/event-stream" in value.lower():
            return True
    return False


def _without_content_length(
    headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    return [(key, value) for key, value in headers if key.lower() != b"content-length"]


def _completion_event(response_id: str) -> bytes:
    payload = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "completed",
        },
    }
    return (
        b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n\n"
    )


def _extract_response_id(chunk: bytes, current: str) -> str:
    if current != "resp_reverso_completed":
        return current
    for line in chunk.splitlines():
        if not line.startswith(b"data: ") or line.strip() == _DONE_LINE:
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("id"), str):
            return response["id"]
        if isinstance(payload.get("response_id"), str):
            return payload["response_id"]
    return current


def _has_completed_marker(chunk: bytes) -> bool:
    return (
        _RESPONSE_COMPLETED_MARKER in chunk
        or _RESPONSE_COMPLETED_MARKER_SPACED in chunk
    )


def _append_completion_before_done(body: bytes, completion: bytes) -> bytes:
    done_index = body.find(_DONE_LINE)
    if done_index == -1:
        return body + completion
    return body[:done_index] + completion + body[done_index:]


def _has_done_marker(chunk: bytes) -> bool:
    return _DONE_LINE in chunk


class ResponsesSSECompletionMiddleware:
    """Append response.completed to Responses SSE streams when upstream omits it."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _is_responses_path(
            str(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        is_responses_sse = False
        pending_body = b""
        saw_completed = False
        saw_done = False
        response_id = "resp_reverso_completed"
        marker_tail = b""

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal \
                is_responses_sse, \
                marker_tail, \
                pending_body, \
                response_id, \
                saw_completed, \
                saw_done
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                is_responses_sse = _is_sse(headers)
                if is_responses_sse:
                    message = dict(message)
                    message["headers"] = _without_content_length(headers)
                await send(message)
                return

            if message.get("type") != "http.response.body" or not is_responses_sse:
                await send(message)
                return

            body = pending_body + message.get("body", b"")
            pending_body = b""
            response_id = _extract_response_id(body, response_id)
            marker_window = marker_tail + body
            if _has_completed_marker(marker_window):
                saw_completed = True
            if _has_done_marker(marker_window):
                saw_done = True
            marker_tail = marker_window[-64:]

            if message.get("more_body", False):
                tail_size = (
                    max(len(_DONE_LINE), len(_RESPONSE_COMPLETED_MARKER_SPACED)) - 1
                )
                if len(body) <= tail_size:
                    pending_body = body
                    return
                pending_body = body[-tail_size:]
                await send({**message, "body": body[:-tail_size]})
                return

            if not saw_completed or not saw_done:
                final_body = body
                if not saw_completed:
                    final_body = _append_completion_before_done(
                        final_body, _completion_event(response_id)
                    )
                    saw_completed = True
                if not saw_done:
                    final_body += _DONE_EVENT
                    saw_done = True
                await send(
                    {
                        "type": "http.response.body",
                        "body": final_body,
                        "more_body": False,
                    }
                )
                return

            await send({**message, "body": body})

        await self.app(scope, receive, wrapped_send)
