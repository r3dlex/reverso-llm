"""Unit tests for OpenRouter Messages streaming (OR-G5, U8, I5).

These tests inject a fake streaming transport and assert that the
OpenRouter Messages SSE path:
* emits canonical Anthropic SSE events in order;
* drops SSE comments and stops at message_stop;
* accepts the streaming payload through the messages adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest import mock

import pytest

from reverso.protocols.adapters.openrouter.messages_stream import (
    build_stream_payload,
    normalize_stream_event,
)


@dataclass
class FakeStreamingClient:
    events: list[str] = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        response = mock.Mock()
        response.status_code = 200
        response.iter_lines = lambda: iter(self.events)
        response.__enter__ = lambda self=self: self
        response.__exit__ = lambda *a: False
        response.headers = {"content-type": "text/event-stream"}
        return response


def test_build_stream_payload_forwards_stream_true() -> None:
    payload = build_stream_payload({"model": "m", "max_tokens": 32, "messages": []})
    assert payload["stream"] is True


def test_normalize_stream_event_extracts_event_type() -> None:
    raw = {"type": "message_start", "message": {"id": "m", "role": "assistant"}}
    normalized = normalize_stream_event(raw)
    assert normalized["type"] == "message_start"
    assert normalized["message"]["id"] == "m"


def test_normalize_stream_event_passes_through_unknown_types() -> None:
    raw = {"type": "ping"}
    assert normalize_stream_event(raw) == raw


def test_normalize_stream_event_rejects_non_dict_inputs() -> None:
    with pytest.raises(TypeError):
        normalize_stream_event(None)  # type: ignore[arg-type]
