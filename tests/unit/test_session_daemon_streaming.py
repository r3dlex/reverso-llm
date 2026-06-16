"""Tests for daemon NDJSON turn streaming."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest

from reverso.daemon import session_daemon
from reverso.daemon.session_table import Session


async def _lines() -> AsyncIterator[str]:
    yield '{"type":"assistant","session_id":"sid","message":{"content":[{"type":"text","text":"Hello"}]}}\n'
    yield '{"type":"assistant","session_id":"sid","message":{"content":[{"type":"text","text":"Hello there"}]}}\n'
    yield '{"type":"result","session_id":"sid","result":"Hello there"}\n'


class _Proc:
    stdout = object()
    stderr = None
    returncode = 0

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_codex_turn_uses_explicit_skip_git_repo_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return _Proc()

    class FakeCodexParser:
        thread_id = "thread-1"

        async def parse_stream(self, _lines):
            return "OK", []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(session_daemon, "CodexCLIParser", FakeCodexParser)

    now = datetime.now(timezone.utc)
    session = Session(
        key=("local", "/tmp", "openai"),
        process=_Proc(),
        spawned_at=now,
        last_request_at=now,
    )
    text, observations, thread_id = await session_daemon._run_codex_turn(
        session=session,
        user_message="hello",
        model="gpt-5.3-codex-spark",
        workspace="/tmp",
        timeout=5,
    )

    assert text == "OK"
    assert observations == []
    assert thread_id == "thread-1"
    assert "--skip-git-repo-check" in captured["args"]


@pytest.mark.asyncio
async def test_stream_claude_turn_emits_incremental_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(session_daemon, "_aiter_lines", lambda stream: _lines())

    now = datetime.now(timezone.utc)
    session = Session(
        key=("local", "/tmp", "anthropic"),
        process=_Proc(),
        spawned_at=now,
        last_request_at=now,
    )
    events = [
        event
        async for event in session_daemon._stream_claude_turn(
            session=session,
            user_message="hello",
            model="claude-sonnet-4-6",
            workspace="/tmp",
            timeout=5,
        )
    ]

    assert events == [
        {"type": "delta", "delta": "Hello"},
        {"type": "delta", "delta": " there"},
        {
            "type": "completed",
            "assistant_text": "Hello there",
            "observations": [],
            "session_id": "sid",
        },
    ]


async def _tool_lines() -> AsyncIterator[str]:
    yield (
        json.dumps(
            {
                "type": "assistant",
                "session_id": "sid",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    yield (
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_1",
                            "content": "ok",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    yield json.dumps({"type": "result", "session_id": "sid", "result": "done"}) + "\n"


@pytest.mark.asyncio
async def test_stream_claude_turn_preserves_specific_observation_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(session_daemon, "_aiter_lines", lambda stream: _tool_lines())

    now = datetime.now(timezone.utc)
    session = Session(
        key=("local", "/tmp", "anthropic"),
        process=_Proc(),
        spawned_at=now,
        last_request_at=now,
    )
    events = [
        event
        async for event in session_daemon._stream_claude_turn(
            session=session,
            user_message="hello",
            model="claude-sonnet-4-6",
            workspace="/tmp",
            timeout=5,
        )
    ]

    completed = events[-1]
    assert completed["type"] == "completed"
    assert completed["observations"][0]["type"] == "file_read"
    assert completed["observations"][0]["tool_name"] == "Read"


@pytest.mark.asyncio
async def test_session_turn_stream_rejects_invalid_provider_before_streaming() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await session_daemon.session_turn_stream(
            session_daemon.TurnRequest(
                workspace="/tmp",
                provider="bad-provider",
                user_message="hello",
                model="model",
            )
        )

    assert exc_info.value.status_code == 400
