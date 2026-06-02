"""Unit tests for reverso.proxy.utils."""
import pytest
from reverso.proxy.utils import strip_think_blocks, last_user_message, StreamingThinkStripper


def test_strip_think_blocks_no_blocks():
    assert strip_think_blocks("hello world") == "hello world"


def test_strip_think_blocks_complete_block():
    result = strip_think_blocks("<think>internal reasoning</think>answer")
    assert result == "answer"


def test_strip_think_blocks_multiple_blocks():
    result = strip_think_blocks("<think>a</think>x<think>b</think>y")
    assert result == "xy"


def test_strip_think_blocks_unclosed():
    result = strip_think_blocks("prefix<think>unclosed")
    assert result == "prefix"


def test_strip_think_blocks_non_string():
    assert strip_think_blocks(None) is None  # type: ignore


def test_last_user_message_simple():
    messages = [{"role": "user", "content": "hello"}]
    assert last_user_message(messages) == "hello"


def test_last_user_message_last_wins():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert last_user_message(messages) == "second"


def test_last_user_message_content_parts():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}]
    assert last_user_message(messages) == "hello\nworld"


def test_last_user_message_no_user():
    messages = [{"role": "assistant", "content": "hi"}]
    assert last_user_message(messages) == ""


def test_streaming_stripper_passthrough():
    stripper = StreamingThinkStripper()
    assert stripper.strip_delta("hello") == "hello"


def test_streaming_stripper_complete_block():
    stripper = StreamingThinkStripper()
    assert stripper.strip_delta("<think>x</think>answer") == "answer"


def test_streaming_stripper_split_across_chunks():
    stripper = StreamingThinkStripper()
    # Block split across two chunks
    assert stripper.strip_delta("<think>rea") == ""
    assert stripper.strip_delta("soning</think>answer") == "answer"


def test_resolve_cli_command_uses_env(monkeypatch, tmp_path):
    from reverso.proxy.utils import resolve_cli_command

    fake = tmp_path / "fake-cli"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("REVERSO_FAKE_BIN", str(fake))

    assert resolve_cli_command("missing-cli", "REVERSO_FAKE_BIN") == str(fake)
