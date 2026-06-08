"""Normalize Codex Responses requests for provider profile compatibility."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from reverso.proxy.profile_routing import split_profile_path

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _is_responses_post(scope: Scope) -> bool:
    if str(scope.get("method", "GET")).upper() != "POST":
        return False
    path = str(scope.get("path", ""))
    profile_path = split_profile_path(path)
    effective_path = profile_path.rewritten_path if profile_path is not None else path
    return effective_path.rstrip("/").endswith("/responses")


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


def _headers_with_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    filtered = [
        (key, value) for key, value in headers if key.lower() != b"content-length"
    ]
    filtered.append((b"content-length", str(length).encode("ascii")))
    return filtered


def _receive_replay(
    body: bytes | None, disconnected: bool, source_receive: Receive
) -> Receive:
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


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _is_assistant_message_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "assistant"
    )


def _sanitize_input_tool_sequence(items: list[Any]) -> list[Any]:
    sanitized: list[Any] = []
    pending_call_ids: set[str] = set()
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            call_id = item.get("call_id")
            if isinstance(call_id, str):
                pending_call_ids.add(call_id)
            sanitized.append(item)
            continue
        if pending_call_ids and _is_assistant_message_item(item):
            continue
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            if isinstance(call_id, str):
                pending_call_ids.discard(call_id)
        sanitized.append(item)
    return sanitized


def _is_valid_tool_choice(tool_choice: Any, function_tool_names: set[str]) -> bool:
    if isinstance(tool_choice, str):
        return tool_choice in {"auto", "none", "required"}
    if not isinstance(tool_choice, dict):
        return False
    if tool_choice.get("type") != "function":
        return False
    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return False
    name = function.get("name")
    return isinstance(name, str) and name in function_tool_names


def normalize_codex_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop Codex-only Responses fields that OpenAI-compatible providers reject."""
    data = dict(payload)
    input_items = data.get("input")
    if isinstance(input_items, list):
        developer_chunks: list[str] = []
        rewritten_input: list[Any] = []
        for item in input_items:
            if isinstance(item, dict) and item.get("role") == "developer":
                text = _content_to_text(item.get("content"))
                if text:
                    developer_chunks.append(text)
                continue
            rewritten_input.append(item)
        if developer_chunks:
            prefix = (
                data.get("instructions")
                if isinstance(data.get("instructions"), str)
                else ""
            )
            merged = "\n\n".join(
                part
                for part in [
                    prefix,
                    "Developer instructions:\n" + "\n\n".join(developer_chunks),
                ]
                if part
            )
            data["instructions"] = merged
        data["input"] = _sanitize_input_tool_sequence(rewritten_input)

    tools = data.get("tools")
    if isinstance(tools, list):
        function_tools = [
            tool
            for tool in tools
            if isinstance(tool, dict) and tool.get("type") == "function"
        ]
        if function_tools:
            data["tools"] = function_tools
            function_tool_names = {
                tool["name"]
                for tool in function_tools
                if isinstance(tool.get("name"), str)
            }
            if "tool_choice" in data and not _is_valid_tool_choice(
                data["tool_choice"], function_tool_names
            ):
                data.pop("tool_choice", None)
        else:
            data.pop("tools", None)
            data.pop("tool_choice", None)

    for key in (
        "parallel_tool_calls",
        "reasoning",
        "context_management",
        "include",
        "prompt_cache_key",
        "safety_identifier",
        "service_tier",
        "store",
        "truncation",
    ):
        data.pop(key, None)
    return data


def _normalize_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict):
        return body
    normalized = normalize_codex_responses_payload(payload)
    if normalized == payload:
        return body
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


class CodexResponsesNormalizerMiddleware:
    """Normalize Codex Responses payloads before LiteLLM provider routing."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _is_responses_post(scope):
            await self.app(scope, receive, send)
            return

        body, disconnected = await _read_body(receive)
        if body is not None:
            body = _normalize_body(body)
            scope = dict(scope)
            scope["headers"] = _headers_with_content_length(
                list(scope.get("headers", [])), len(body)
            )
        await self.app(scope, _receive_replay(body, disconnected, receive), send)
