"""Strip provider reasoning tags from Responses API output."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from reverso.proxy.utils import StreamingThinkStripper, strip_think_blocks

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]


_DONE_LINE = b"data: [DONE]"
_DROP_SSE_EVENT_TYPES = frozenset({
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
})


def _is_responses_path(path: str) -> bool:
    return path.rstrip("/").endswith("/responses")


def _is_sse(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type" and b"text/event-stream" in value.lower():
            return True
    return False


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type" and b"json" in value.lower():
            return True
    return False


def _without_content_length(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    return [(key, value) for key, value in headers if key.lower() != b"content-length"]


def _strip_text_fields(value: Any, stripper: StreamingThinkStripper | None = None) -> Any:
    if isinstance(value, list):
        list_stripper = stripper or StreamingThinkStripper()
        return [
            _strip_text_fields(item, list_stripper)
            for item in value
            if not (isinstance(item, dict) and item.get("type") == "reasoning")
        ]
    if not isinstance(value, dict):
        return value
    stripped: dict[str, Any] = {}
    for key, child in value.items():
        if key == "content" and isinstance(child, list):
            stripped[key] = _collapse_leading_output_text_reasoning(_strip_text_fields(child, stripper))
        elif key in {"text", "delta"} and isinstance(child, str):
            stripped[key] = stripper.strip_delta(child) if stripper else strip_think_blocks(child)
        else:
            stripped[key] = _strip_text_fields(child, stripper)
    return stripped


def _collapse_leading_output_text_reasoning(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    text_indexes = [
        index
        for index, item in enumerate(value)
        if isinstance(item, dict) and item.get("type") == "output_text" and isinstance(item.get("text"), str)
    ]
    non_empty = [index for index in text_indexes if value[index]["text"].strip()]
    if len(non_empty) <= 1:
        return value
    keep = non_empty[-1]
    collapsed = []
    for index, item in enumerate(value):
        if index in non_empty and index != keep:
            item = dict(item)
            item["text"] = ""
        collapsed.append(item)
    return collapsed


def _stream_key(payload: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        payload.get("item_id"),
        payload.get("output_index"),
        payload.get("content_index"),
    )


def _data_line(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _is_reasoning_output_item(payload: dict[str, Any]) -> bool:
    if payload.get("type") not in {"response.output_item.added", "response.output_item.done"}:
        return False
    item = payload.get("item")
    return isinstance(item, dict) and item.get("type") == "reasoning"


def _synthetic_text_preamble(payload: dict[str, Any], sent_items: set[Any], sent_content_parts: set[tuple[Any, Any, Any]]) -> list[dict[str, Any]]:
    key = _stream_key(payload)
    item_id = payload.get("item_id")
    output_index = payload.get("output_index", 0)
    content_index = payload.get("content_index", 0)
    events: list[dict[str, Any]] = []
    if item_id not in sent_items:
        item = {
            "id": item_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        event = {"type": "response.output_item.added", "output_index": output_index, "item": item}
        if isinstance(payload.get("model"), str):
            event["model"] = payload["model"]
        events.append(event)
        sent_items.add(item_id)
    if key not in sent_content_parts:
        event = {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }
        if isinstance(payload.get("model"), str):
            event["model"] = payload["model"]
        events.append(event)
        sent_content_parts.add(key)
    return events


def _strip_sse_payload(
    payload: dict[str, Any],
    stream_strippers: dict[tuple[Any, Any, Any], StreamingThinkStripper],
    completed_texts: dict[tuple[Any, Any, Any], str],
    sent_items: set[Any],
    sent_content_parts: set[tuple[Any, Any, Any]],
) -> list[dict[str, Any]]:
    event_type = payload.get("type")
    if event_type in _DROP_SSE_EVENT_TYPES or _is_reasoning_output_item(payload):
        return []
    if event_type == "response.output_item.added":
        item = payload.get("item")
        if isinstance(item, dict):
            sent_items.add(item.get("id"))
    if event_type == "response.content_part.added":
        part = payload.get("part")
        if isinstance(part, dict) and part.get("type") == "output_text":
            sent_content_parts.add(_stream_key(payload))
    if event_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
        key = _stream_key(payload)
        stripper = stream_strippers.setdefault(key, StreamingThinkStripper())
        payload = dict(payload)
        payload["delta"] = stripper.strip_delta(payload["delta"])
        if payload["delta"] == "":
            return []
        return _synthetic_text_preamble(payload, sent_items, sent_content_parts) + [payload]
    if event_type == "response.output_text.done" and isinstance(payload.get("text"), str):
        key = _stream_key(payload)
        payload = _strip_text_fields(payload)
        completed_texts[key] = str(payload.get("text", ""))
        return [payload]
    if event_type == "response.content_part.done":
        key = _stream_key(payload)
        part = payload.get("part")
        if isinstance(part, dict) and part.get("type") != "output_text" and key in completed_texts:
            payload = dict(payload)
            payload["part"] = {
                "type": "output_text",
                "text": completed_texts[key],
                "annotations": [],
            }
            return [payload]
    return [_strip_text_fields(payload)]


def _rewrite_sse_line(
    line: bytes,
    stream_strippers: dict[tuple[Any, Any, Any], StreamingThinkStripper],
    completed_texts: dict[tuple[Any, Any, Any], str],
    sent_items: set[Any],
    sent_content_parts: set[tuple[Any, Any, Any]],
) -> list[bytes]:
    if not line.startswith(b"data: ") or line.strip() == _DONE_LINE:
        return [line]
    try:
        payload = json.loads(line[6:])
    except json.JSONDecodeError:
        return [line]
    if not isinstance(payload, dict):
        return [line]
    payloads = _strip_sse_payload(payload, stream_strippers, completed_texts, sent_items, sent_content_parts)
    return [_data_line(payload) for payload in payloads]


def _rewrite_sse_block(
    body: bytes,
    stream_strippers: dict[tuple[Any, Any, Any], StreamingThinkStripper],
    completed_texts: dict[tuple[Any, Any, Any], str],
    sent_items: set[Any],
    sent_content_parts: set[tuple[Any, Any, Any]],
) -> bytes:
    keepends = body.splitlines(keepends=True)
    rewritten_lines: list[bytes] = []
    for line in keepends:
        content = line.rstrip(b"\r\n")
        rewritten = _rewrite_sse_line(content, stream_strippers, completed_texts, sent_items, sent_content_parts)
        for index, rewritten_line in enumerate(rewritten):
            suffix = line[len(content):] if index == len(rewritten) - 1 else b"\n\n"
            rewritten_lines.append(rewritten_line + suffix)
    return b"".join(rewritten_lines)


def _rewrite_json_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    return json.dumps(_strip_text_fields(payload), separators=(",", ":")).encode("utf-8")


class ResponsesThinkStripperMiddleware:
    """Remove <think> blocks from Responses API JSON and SSE text output."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _is_responses_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        is_responses_sse = False
        is_responses_json = False
        pending = b""
        stream_strippers: dict[tuple[Any, Any, Any], StreamingThinkStripper] = {}
        completed_texts: dict[tuple[Any, Any, Any], str] = {}
        sent_items: set[Any] = set()
        sent_content_parts: set[tuple[Any, Any, Any]] = set()

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal is_responses_json, is_responses_sse, pending
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                is_responses_sse = _is_sse(headers)
                is_responses_json = _is_json(headers)
                if is_responses_sse or is_responses_json:
                    message = dict(message)
                    message["headers"] = _without_content_length(headers)
                await send(message)
                return

            if message.get("type") != "http.response.body" or not (is_responses_sse or is_responses_json):
                await send(message)
                return

            body = pending + message.get("body", b"")
            pending = b""

            if is_responses_sse:
                if message.get("more_body", False):
                    split_at = body.rfind(b"\n")
                    if split_at == -1:
                        pending = body
                        return
                    pending = body[split_at + 1:]
                    rewritten = _rewrite_sse_block(body[:split_at + 1], stream_strippers, completed_texts, sent_items, sent_content_parts)
                    await send({**message, "body": rewritten})
                    return
                rewritten = _rewrite_sse_block(body, stream_strippers, completed_texts, sent_items, sent_content_parts)
                await send({**message, "body": rewritten})
                return

            if message.get("more_body", False):
                pending = body
                return
            await send({**message, "body": _rewrite_json_body(body)})

        await self.app(scope, receive, wrapped_send)
