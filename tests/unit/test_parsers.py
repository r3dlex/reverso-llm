"""Unit tests for daemon parsers."""
import json
import asyncio
import pytest
from reverso.daemon.parsers.claude_code import ClaudeCodeParser
from reverso.daemon.parsers.codex_cli import CodexCLIParser


async def _lines_iter(lines):
    for line in lines:
        yield line


@pytest.mark.asyncio
async def test_claude_parser_plain_text():
    events = [
        json.dumps({"type": "result", "result": "Hello there.", "session_id": "abc-123"}),
    ]
    parser = ClaudeCodeParser()
    text, obs = await parser.parse_stream(_lines_iter(events))
    assert text == "Hello there."
    assert obs == []
    assert parser.session_id == "abc-123"


@pytest.mark.asyncio
async def test_claude_parser_tool_use():
    events = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/foo"}}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents", "is_error": False}]}}),
        json.dumps({"type": "result", "result": "I read the file.", "session_id": "sid1"}),
    ]
    parser = ClaudeCodeParser()
    text, obs = await parser.parse_stream(_lines_iter(events))
    assert text == "I read the file."
    assert len(obs) == 1
    assert obs[0]["tool_name"] == "Read"
    assert obs[0]["type"] == "file_read"


@pytest.mark.asyncio
async def test_codex_parser_plain():
    events = [
        json.dumps({"type": "thread.started", "thread_id": "thread-xyz"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Hello from codex"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]
    parser = CodexCLIParser()
    text, obs = await parser.parse_stream(_lines_iter(events))
    assert text == "Hello from codex"
    assert parser.thread_id == "thread-xyz"
    assert obs == []


@pytest.mark.asyncio
async def test_codex_parser_command_execution():
    events = [
        json.dumps({"type": "thread.started", "thread_id": "tid1"}),
        json.dumps({"type": "item.started", "item": {"id": "i0", "type": "command_execution", "command": "ls /tmp", "aggregated_output": "", "exit_code": None, "status": "in_progress"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "command_execution", "command": "ls /tmp", "aggregated_output": "file1\nfile2", "exit_code": 0, "status": "completed"}}),
        json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Found 2 files."}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    parser = CodexCLIParser()
    text, obs = await parser.parse_stream(_lines_iter(events))
    assert text == "Found 2 files."
    assert len(obs) == 1
    assert obs[0]["type"] == "shell_cmd"
    assert "ls /tmp" in obs[0]["args"]["command"]
