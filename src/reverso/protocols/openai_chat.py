"""Shared OpenAI chat-completions translation helpers (ADR 0011).

The DeepSeek adapter was the first Responses<->/chat/completions translator in
this codebase. GitHub Copilot serves Anthropic (claude-*) and Google (gemini-*)
models on the SAME /chat/completions surface (gpt-* stays on /responses), so the
two adapters now share the pure translation seam that has no provider-specific
behaviour:

  * request translation: Responses input/instructions/tool-loop items ->
    chat messages (_build_messages_from_request, _walk_input_items),
  * tool translation: Responses function tools/tool_choice -> chat shapes
    (_chat_tools, _chat_tool_choice),
  * response translation: chat completion -> Responses output items
    (_first_message, _tool_call_item) and usage rename (_responses_usage),
  * streaming: chat SSE delta parsing (_parse_stream_event), the
    usage round-trip (_usage_to_chat), and the pre-emission priming
    contract (_prime_upstream_stream).

Provider-specific behaviour (DeepSeek reasoning_content carry-forward, profile
model resolution, response_format/max_output_tokens extra translation) stays in
the owning adapter. The canonical Responses SSE event emission stays in
``reverso.protocols.replay`` (replay_incremental); this module only produces the
chunk dicts that helper consumes.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from reverso.protocols.replay import (
    flatten_input,
    message_item,
    new_message_id,
)


def first_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the first chat choice's ``message`` dict (empty when absent)."""
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                return message
    return {}


def has_tool_items(value: Any) -> bool:
    """True when the Responses input list contains a function_call(_output) item."""
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") in {"function_call", "function_call_output"}
        for item in value
    )


def walk_input_items(items: Any) -> list[dict[str, Any]]:
    """Translate a codex tool-loop input list into chat-completion messages.

    Codex resends the prior turn's transcript on the second leg of a tool
    loop: the original user message, the assistant's function_call echo (with
    call_id/name/arguments), then a function_call_output item (with call_id
    and the executed tool's output). The chat API expects this as an assistant
    message carrying ``tool_calls`` followed by one ``role="tool"`` message per
    call result. Without this translation the upstream never sees the tool
    result and keeps re-issuing the same function_call indefinitely.
    Consecutive function_call items collapse into ONE assistant message so a
    parallel-tool turn surfaces as a single tool_calls array. Reasoning items
    and any unknown types are skipped.
    """
    out: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if pending_tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": list(pending_tool_calls),
                }
            )
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            pending_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else "",
                    },
                }
            )
            continue
        flush_pending()
        if item_type == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if not isinstance(call_id, str):
                continue
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_output_content(output),
                }
            )
            continue
        if item_type == "message" or item.get("role") is not None:
            role = item.get("role")
            if not isinstance(role, str):
                continue
            text = input_content_text(item)
            out.append({"role": role, "content": text})
            continue
        # reasoning or unknown item types: skip
    flush_pending()
    return out


def tool_output_content(output: Any) -> str:
    """Coerce a function_call_output ``output`` field into a chat ``content`` string.

    Codex sends a plain string for shell tool outputs, but other tools return
    structured payloads (dict/list); the chat API expects a string ``content``
    on the role=tool message, so structured outputs are JSON-serialised here
    instead of silently dropped. None and non-serialisable values fall back
    to the empty string (the conservative default).
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def input_content_text(item: dict[str, Any]) -> str:
    """Extract text content from a Responses input item (string or list-of-parts)."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    text = item.get("text")
    return text if isinstance(text, str) else ""


def build_messages_from_request(
    *,
    instructions: str | None,
    input_value: Any,
    prior_messages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Translate Responses instructions/input plus a prior turn into chat messages.

    ``instructions`` becomes a leading system message. ``prior_messages`` (when
    given) is the provider-built carry-forward for previous_response_id chaining
    and is inserted before the current turn. When ``input_value`` is an item
    list carrying any function_call/function_call_output item, a structural walk
    is used so the upstream chat API sees the prior assistant tool_calls message
    and the role=tool result; otherwise text-only input goes through the
    flatten_input collapse so message-only fixtures stay unchanged.
    """
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if prior_messages:
        messages.extend(prior_messages)
    if has_tool_items(input_value):
        messages.extend(walk_input_items(input_value))
        return messages
    user_text = flatten_input(input_value)
    if user_text:
        messages.append({"role": "user", "content": user_text})
    return messages


def responses_usage(usage: Any) -> dict[str, Any] | None:
    """Translate chat-completions usage into Responses usage field names.

    Codex parses the terminal response.completed event strictly and fails on
    chat-style ``prompt_tokens``/``completion_tokens`` (missing field
    `input_tokens`), so the chat names must not leak into the envelope.
    """
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Responses-format function tools to chat-completions format.

    Codex sends flat ``{"type":"function","name":...,"parameters":...}`` tool
    declarations; the chat API requires the nested ``function`` object and
    returns 400 (missing field `function`) otherwise. Tools already in chat
    format pass through unchanged; non-function tool types are dropped because
    only function tools are supported.
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "function" in tool:
            converted.append(tool)
            continue
        if tool.get("type") != "function":
            continue
        function = {
            key: tool[key]
            for key in ("name", "description", "parameters")
            if tool.get(key) is not None
        }
        converted.append({"type": "function", "function": function})
    return converted


def chat_tool_choice(tool_choice: Any) -> Any:
    """Convert a Responses-format tool_choice to chat-completions format."""
    if (
        isinstance(tool_choice, dict)
        and tool_choice.get("type") == "function"
        and "function" not in tool_choice
    ):
        return {"type": "function", "function": {"name": tool_choice.get("name")}}
    return tool_choice


def tool_call_item(call: dict[str, Any]) -> dict[str, Any]:
    """Surface a chat tool_call as a Responses function_call output item.

    Tool calls are SURFACED only; the adapter never executes them (no subprocess
    or extra network beyond the single upstream chat call).
    """
    function = call.get("function", {}) if isinstance(call, dict) else {}
    return {
        "id": new_message_id(),
        "type": "function_call",
        "status": "completed",
        "call_id": call.get("id") if isinstance(call, dict) else None,
        "name": function.get("name"),
        "arguments": function.get("arguments"),
    }


def map_completion_output(
    raw: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the Responses output items and return the source chat message.

    Returns ``(output, message)`` where ``output`` is the Responses output list
    (a single message item plus one function_call item per upstream tool_call)
    and ``message`` is the upstream chat ``message`` dict so the caller can read
    provider-specific fields (e.g. reasoning_content). The message item id is
    minted fresh; the caller may rewrite it (streaming reuses the announced id).
    """
    message = first_message(raw)
    text = message.get("content") or ""
    output: list[dict[str, Any]] = [message_item(new_message_id(), str(text))]
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            output.append(tool_call_item(call))
    return output, message


def parse_stream_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one upstream stream-json event into the replay chunk shape.

    Returns ``None`` for events that should be skipped (no choices payload AND
    no usage block, or a non-null choices entry that yields no content of any
    kind). Note: ``finish_reason`` does NOT mark ``done`` here; OpenAI-compatible
    upstreams with ``stream_options.include_usage`` emit the terminal usage
    block AFTER the finish_reason chunk and BEFORE the ``[DONE]`` sentinel, so
    the iterator must keep consuming until ``[DONE]``.
    """
    usage = event.get("usage")
    choices = event.get("choices")
    text = ""
    reasoning_text = ""
    tool_calls: list[dict[str, Any]] = []
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                text = content
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                reasoning_text = reasoning
            raw_tool_calls = delta.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for call in raw_tool_calls:
                    if isinstance(call, dict):
                        tool_calls.append(call)
    translated_usage = responses_usage(usage) if isinstance(usage, dict) else None
    if not text and not reasoning_text and not tool_calls and translated_usage is None:
        return None
    return {
        "text": text,
        "reasoning_text": reasoning_text,
        "tool_calls": tool_calls,
        "usage": translated_usage,
        "done": False,
    }


def usage_to_chat(usage: dict[str, Any]) -> dict[str, Any]:
    """Reverse ``responses_usage`` so a synthesised chat body looks upstream-shaped.

    A finalize step that round-trips through a chat-shaped raw dict expects
    chat-style ``prompt_tokens``/``completion_tokens`` keys; the streaming chunk
    parser already produced a Responses-shaped usage object, so it is reversed
    here.
    """
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


async def prime_upstream_stream(
    chunks: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Advance ``chunks`` once so upstream connect+status errors raise eagerly.

    A streaming runner is an async generator whose ``async with
    client.stream(...)`` block only enters when iteration starts. Priming the
    first step here ensures any pre-emission failure (4xx at response.headers,
    transport error before any byte) raises BEFORE the caller yields the first
    canonical envelope event, so the gateway can synthesise a structured 502.
    The first usable chunk (if any) is re-injected ahead of the remaining
    iterator so replay_incremental sees the whole sequence.
    """
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:

        async def empty() -> AsyncIterator[dict[str, Any]]:
            return
            yield  # pragma: no cover - keeps this an async generator

        return empty()

    async def replay() -> AsyncIterator[dict[str, Any]]:
        yield first
        async for chunk in chunks:
            yield chunk

    return replay()
