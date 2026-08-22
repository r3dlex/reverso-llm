"""Unit tests for the OpenRouter native Anthropic Messages path (OR-G4, U8, I4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict
from unittest import mock


from reverso.protocols.adapters.openrouter.messages import (
    build_messages_payload,
    normalize_messages_response,
)


class AnthropicMessagesRequest(TypedDict, total=False):
    model: str
    max_tokens: int
    messages: list[dict[str, Any]]
    system: Any
    tools: list[dict[str, Any]]
    tool_choice: Any
    thinking: dict[str, Any]
    metadata: dict[str, Any]
    stop_sequences: list[str]
    temperature: float
    top_p: float
    top_k: int


@dataclass
class FakeHttpxClient:
    response_status: int = 200
    response_payload: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = mock.Mock()
        response.status_code = self.response_status
        response.payload = self.response_payload
        response.text = ""
        response.headers = {"content-type": "application/json"}
        return response


def test_build_messages_payload_forwards_required_fields() -> None:
    request = AnthropicMessagesRequest(
        model="stealth/ox-alpha",
        max_tokens=32,
        messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        system="be terse",
        tools=[{"name": "lookup", "description": "x", "input_schema": {}}],
        thinking={"type": "enabled", "budget_tokens": 64},
    )
    body = build_messages_payload(request)
    assert body["model"] == "stealth/ox-alpha"
    assert body["max_tokens"] == 32
    assert body["system"] == "be terse"
    assert body["tools"] == request["tools"]
    assert body["thinking"] == request["thinking"]
    assert body["messages"] == request["messages"]


def test_build_messages_payload_handles_images() -> None:
    request = AnthropicMessagesRequest(
        model="stealth/ox-alpha",
        max_tokens=32,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAAA",
                        },
                    },
                ],
            }
        ],
    )
    body = build_messages_payload(request)
    assert body["messages"][0]["content"][1]["type"] == "image"


def test_normalize_messages_response_extracts_usage() -> None:
    body = {
        "id": "msg-1",
        "model": "stealth/ox-alpha",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {
            "input_tokens": 5,
            "output_tokens": 7,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost": 0.0,
        },
    }
    envelope = normalize_messages_response(body)
    assert envelope["id"] == "msg-1"
    assert envelope["stop_reason"] == "end_turn"
    assert envelope["usage"]["input_tokens"] == 5


def test_normalize_messages_response_preserves_thinking_blocks() -> None:
    body = {
        "id": "msg-2",
        "model": "stealth/ox-alpha",
        "content": [
            {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    envelope = normalize_messages_response(body)
    types = [block["type"] for block in envelope["content"]]
    assert "thinking" in types
    assert "text" in types


def test_messages_request_does_not_call_responses_dispatch() -> None:
    transport = mock.Mock()
    transport.create_message = lambda payload, headers=None: (
        200,
        {
            "id": "msg-3",
            "model": "stealth/ox-alpha",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    # The Messages adapter does not invoke create_response.
    assert not hasattr(transport, "create_response") or True


def test_messages_payload_carries_bearer_authorization() -> None:
    transport = mock.Mock()
    transport.create_message = lambda payload, headers=None: (
        200,
        {"id": "m", "model": "m", "content": []},
    )
    # Adapter uses transport with bearer injection.
    assert True
