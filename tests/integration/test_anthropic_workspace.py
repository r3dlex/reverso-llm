"""Integration tests for the per-request workspace on the Anthropic surface (ADR 0012).

The Anthropic Messages surface resolves a per-request workspace from the
``x-reverso-workspace`` request header and sets ``CURRENT_PROFILE_WORKSPACE`` for the
dispatch, so the CLI-backed adapters (codex/claude) spawn in the caller's launch
directory instead of inheriting the daemon CWD. These tests drive
AnthropicMessagesApp with a fake adapter whose create_response records the contextvar
value observed at dispatch time, and assert it is set from an existing-absolute-dir
header, None when the header is absent, and None when the header points at a
non-existent path (validated away). They also cover the header-less default: the
surface derives the workspace from Claude Code's "Primary working directory" system
prompt line, with the explicit header taking precedence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.anthropic_app import build_anthropic_app
from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

KNOWN_MODEL = "deepseek-v4-pro"


class _WorkspaceRecordingAdapter:
    """A minimal ProviderAdapter that records CURRENT_PROFILE_WORKSPACE on dispatch."""

    def __init__(self) -> None:
        self.recorded: list[str | None] = []

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self.recorded.append(CURRENT_PROFILE_WORKSPACE.get())
        return ResponseEnvelope(
            id="resp_workspace_0001",
            model=request.model or KNOWN_MODEL,
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": []}
                    ],
                }
            ],
            status="completed",
        )

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        self.recorded.append(CURRENT_PROFILE_WORKSPACE.get())
        yield SSEEvent(event="response.completed", data={"type": "response.completed"})

    async def list_models(self) -> ModelList:
        return ModelList(data=[{"id": KNOWN_MODEL, "object": "model"}])

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model=KNOWN_MODEL)

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


def _client(adapter: _WorkspaceRecordingAdapter) -> httpx.AsyncClient:
    app = build_anthropic_app({"deepseek": adapter})
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:64946")


def _messages_body() -> dict[str, Any]:
    return {
        "model": KNOWN_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }


def _messages_body_with_cwd_system(cwd: str) -> dict[str, Any]:
    # Mirror Claude Code's system-prompt env block, which carries the launch dir as a
    # "- Primary working directory: <path>" line among other environment details.
    body = _messages_body()
    body["system"] = [
        {
            "type": "text",
            "text": (
                "You are a helpful assistant.\n\n# Environment\n"
                f" - Primary working directory: {cwd}\n - Platform: darwin\n"
            ),
        }
    ]
    return body


@pytest.mark.asyncio
async def test_workspace_header_sets_contextvar(tmp_path: Path) -> None:
    # An existing absolute dir in x-reverso-workspace is set on CURRENT_PROFILE_WORKSPACE
    # so the CLI-backed adapter spawns there (ADR 0012, spec a).
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/v1/messages",
            json=_messages_body(),
            headers={"x-reverso-workspace": str(tmp_path)},
        )
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [str(tmp_path)]
    # The contextvar is reset on the way out, never leaking past the request.
    assert CURRENT_PROFILE_WORKSPACE.get() is None


@pytest.mark.asyncio
async def test_workspace_header_absent_is_none() -> None:
    # No header -> None (spec b): the adapter inherits no per-request workspace.
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post("/v1/messages", json=_messages_body())
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [None]
    assert CURRENT_PROFILE_WORKSPACE.get() is None


@pytest.mark.asyncio
async def test_workspace_from_system_prompt_when_no_header(tmp_path: Path) -> None:
    # No header, but the system prompt carries "Primary working directory: <dir>":
    # the surface defaults the workspace to that launch dir (by-default behavior).
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/v1/messages", json=_messages_body_with_cwd_system(str(tmp_path))
        )
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [str(tmp_path)]
    assert CURRENT_PROFILE_WORKSPACE.get() is None


@pytest.mark.asyncio
async def test_workspace_header_overrides_system_prompt(tmp_path: Path) -> None:
    # When BOTH are present, the explicit header wins over the system-prompt cwd.
    header_dir = tmp_path / "header"
    system_dir = tmp_path / "system"
    header_dir.mkdir()
    system_dir.mkdir()
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/v1/messages",
            json=_messages_body_with_cwd_system(str(system_dir)),
            headers={"x-reverso-workspace": str(header_dir)},
        )
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [str(header_dir)]
    assert CURRENT_PROFILE_WORKSPACE.get() is None


@pytest.mark.asyncio
async def test_workspace_from_system_prompt_nonexistent_is_none(tmp_path: Path) -> None:
    # A system-prompt cwd that does not exist is validated away to None.
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/v1/messages",
            json=_messages_body_with_cwd_system(str(tmp_path / "missing")),
        )
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [None]
    assert CURRENT_PROFILE_WORKSPACE.get() is None


@pytest.mark.asyncio
async def test_workspace_header_nonexistent_path_is_none(tmp_path: Path) -> None:
    # A header pointing at a non-existent path is validated away to None (spec c), so
    # a bogus cwd is never passed to the CLI spine (which would raise on a missing dir).
    missing = tmp_path / "does-not-exist"
    adapter = _WorkspaceRecordingAdapter()
    async with _client(adapter) as client:
        resp = await client.post(
            "/v1/messages",
            json=_messages_body(),
            headers={"x-reverso-workspace": str(missing)},
        )
    assert resp.status_code == 200, resp.text
    assert adapter.recorded == [None]
    assert CURRENT_PROFILE_WORKSPACE.get() is None
