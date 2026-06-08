"""Session daemon - FastAPI app served over a Unix-domain socket.

Owns all wrapped CLI subprocesses and exposes an internal HTTP API for the
LiteLLM custom providers to call.

Endpoints:
    POST /session/turn  - execute one turn in a session (create if needed)
    GET  /health        - liveness probe

The daemon is started by reverso.daemon.main and listens on the UDS path
configured in config.yaml (daemon_socket).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reverso.daemon.parsers.claude_code import ClaudeCodeParser, _obs_type_for_tool
from reverso.daemon.parsers.codex_cli import CodexCLIParser
from reverso.daemon.session_table import Session, SessionTable
from reverso.proxy.utils import resolve_cli_command

logger = logging.getLogger(__name__)

app = FastAPI(title="reverso-daemon", version="0.2.0")

# Module-level session table shared across all requests.
_session_table: SessionTable = SessionTable()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TurnRequest(BaseModel):
    workspace: str
    provider: str  # "anthropic" or "openai"
    user_message: str
    model: str = "claude-sonnet-4-6"


class TurnResponse(BaseModel):
    assistant_text: str
    session_id: str
    observations: list[dict]


# ---------------------------------------------------------------------------
# Subprocess spawn helpers
# ---------------------------------------------------------------------------


def _resolve_workspace(workspace: str) -> str:
    """Expand ~ and resolve to an absolute path string."""
    return str(Path(workspace).expanduser().resolve())


async def _spawn_claude(workspace: str, model: str) -> asyncio.subprocess.Process:
    """Spawn a long-lived Claude Code process ready for interactive turns.

    We use --output-format stream-json --verbose so the parser can extract
    tool-use events.  The process reads prompts from stdin and produces
    JSON-lines on stdout.

    Note: Claude Code's -p flag exits after one turn. For session reuse we
    use the interactive mode with stdin/stdout pipes, relying on --resume
    on subsequent turns to maintain context.  The spawn here is for the
    first turn; subsequent turns re-spawn with --resume SESSION_ID because
    Claude Code does not stay alive between turns in -p mode.

    The subprocess is kept alive as a sentinel (its pid is stored) and the
    actual turn is driven by spawning a new -p process with --resume.
    """
    resolved = _resolve_workspace(workspace)
    # Spawn a minimal placeholder process (sleep) as the session sentinel.
    # Real turn execution spawns per-turn subprocesses with --resume.
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "infinity",
        cwd=resolved,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("Spawned Claude sentinel pid=%d workspace=%s", proc.pid, resolved)
    return proc


async def _spawn_codex(workspace: str, model: str) -> asyncio.subprocess.Process:
    """Spawn a codex sentinel process for session tracking."""
    resolved = _resolve_workspace(workspace)
    proc = await asyncio.create_subprocess_exec(
        "sleep",
        "infinity",
        cwd=resolved,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("Spawned Codex sentinel pid=%d workspace=%s", proc.pid, resolved)
    return proc


# ---------------------------------------------------------------------------
# Per-turn CLI invocation helpers
# ---------------------------------------------------------------------------


async def _aiter_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Async-iterate over lines from an asyncio StreamReader."""
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        yield line_bytes.decode("utf-8", errors="replace")


async def _run_claude_turn(
    session: Session,
    user_message: str,
    model: str,
    workspace: str,
    timeout: float = 300,
) -> tuple[str, list[dict], str | None]:
    """Execute one Claude Code turn and return (text, observations, cli_session_id).

    Uses --resume if the session already has a cli_session_id from a prior turn.
    """
    resolved = _resolve_workspace(workspace)
    cmd = [
        resolve_cli_command("claude", "REVERSO_CLAUDE_BIN"),
        "-p",
        user_message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--dangerously-skip-permissions",
    ]
    if session.cli_session_id:
        cmd.extend(["--resume", session.cli_session_id])

    logger.debug("Claude turn cmd: %s cwd=%s", cmd, resolved)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=resolved,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None

    try:
        parser = ClaudeCodeParser()
        parse_task = asyncio.create_task(parser.parse_stream(_aiter_lines(proc.stdout)))
        try:
            assistant_text, observations = await asyncio.wait_for(
                parse_task, timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Claude turn timed out after {timeout}s")

        await proc.wait()
        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"claude exited {proc.returncode}; stderr: {stderr_text}"
            )

        cli_session_id: str | None = getattr(parser, "session_id", None)

    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    return assistant_text, observations, cli_session_id


async def _run_codex_turn(
    session: Session,
    user_message: str,
    model: str,
    workspace: str,
    timeout: float = 300,
) -> tuple[str, list[dict], str | None]:
    """Execute one Codex CLI turn and return (text, observations, thread_id)."""
    resolved = _resolve_workspace(workspace)

    if session.cli_session_id:
        # Resume an existing thread.
        cmd = [
            resolve_cli_command("codex", "REVERSO_CODEX_BIN"),
            "exec",
            "resume",
            session.cli_session_id,
            user_message,
            "--json",
            "-s",
            "workspace-write",
            "--model",
            model,
            "-c",
            "skip_git_repo_check=true",
        ]
    else:
        cmd = [
            resolve_cli_command("codex", "REVERSO_CODEX_BIN"),
            "exec",
            user_message,
            "--json",
            "-s",
            "workspace-write",
            "--model",
            model,
            "-c",
            "skip_git_repo_check=true",
        ]

    logger.debug("Codex turn cmd: %s cwd=%s", cmd, resolved)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=resolved,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None

    try:
        parser = CodexCLIParser()
        parse_task = asyncio.create_task(parser.parse_stream(_aiter_lines(proc.stdout)))
        try:
            assistant_text, observations = await asyncio.wait_for(
                parse_task, timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Codex turn timed out after {timeout}s")

        await proc.wait()
        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"codex exited {proc.returncode}; stderr: {stderr_text}")

        thread_id = getattr(parser, "thread_id", None)

    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    return assistant_text, observations, thread_id


async def _stream_claude_turn(
    session: Session,
    user_message: str,
    model: str,
    workspace: str,
    timeout: float = 300,
) -> AsyncIterator[dict[str, Any]]:
    """Execute one Claude Code turn and yield text delta events before completion."""
    resolved = _resolve_workspace(workspace)
    cmd = [
        resolve_cli_command("claude", "REVERSO_CLAUDE_BIN"),
        "-p",
        user_message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--dangerously-skip-permissions",
    ]
    if session.cli_session_id:
        cmd.extend(["--resume", session.cli_session_id])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=resolved,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None

    pending: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    assistant_parts: list[str] = []
    session_id: str | None = None
    seen_text_by_index: dict[int, str] = {}

    async def emit_text(value: str) -> AsyncIterator[dict[str, Any]]:
        if value:
            assistant_parts.append(value)
            yield {"type": "delta", "delta": value}

    try:
        async with asyncio.timeout(timeout):
            async for raw_line in _aiter_lines(proc.stdout):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type == "assistant":
                    if not session_id:
                        session_id = event.get("session_id")
                    msg = event.get("message", {})
                    content_list = msg.get("content", [])
                    if not isinstance(content_list, list):
                        continue
                    for index, content_item in enumerate(content_list):
                        if not isinstance(content_item, dict):
                            continue
                        item_type = content_item.get("type")
                        if item_type == "text":
                            text = str(content_item.get("text", ""))
                            previous = seen_text_by_index.get(index, "")
                            if text.startswith(previous):
                                delta = text[len(previous) :]
                            elif text != previous:
                                delta = text
                            else:
                                delta = ""
                            seen_text_by_index[index] = text
                            async for item in emit_text(delta):
                                yield item
                        elif item_type == "tool_use":
                            tool_id = content_item.get("id", "")
                            pending[tool_id] = {
                                "tool_name": content_item.get("name", ""),
                                "args": content_item.get("input", {}),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                elif event_type == "user":
                    msg = event.get("message", {})
                    content_list = msg.get("content", [])
                    if not isinstance(content_list, list):
                        continue
                    for content_item in content_list:
                        if (
                            not isinstance(content_item, dict)
                            or content_item.get("type") != "tool_result"
                        ):
                            continue
                        tool_use_id = content_item.get("tool_use_id", "")
                        raw_content = content_item.get("content", "")
                        if isinstance(raw_content, list):
                            result_text = "".join(
                                block.get("text", "")
                                if isinstance(block, dict)
                                else str(block)
                                for block in raw_content
                            )
                        else:
                            result_text = str(raw_content) if raw_content else ""
                        pending_item = pending.pop(
                            tool_use_id,
                            {
                                "tool_name": "",
                                "args": {},
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        tool_name = str(pending_item["tool_name"])
                        observations.append(
                            {
                                "type": _obs_type_for_tool(tool_name),
                                "tool_name": tool_name,
                                "args": pending_item["args"],
                                "is_error": bool(content_item.get("is_error", False)),
                                "result_summary": result_text[:200],
                                "timestamp": pending_item["timestamp"],
                            }
                        )
                elif event_type == "result":
                    if not session_id:
                        session_id = event.get("session_id")
                    final_text = str(event.get("result", ""))
                    current_text = "".join(assistant_parts)
                    if final_text and not current_text:
                        async for item in emit_text(final_text):
                            yield item
                    elif final_text.startswith(current_text) and len(final_text) > len(
                        current_text
                    ):
                        async for item in emit_text(final_text[len(current_text) :]):
                            yield item

        await proc.wait()
        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"claude exited {proc.returncode}; stderr: {stderr_text}"
            )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Claude turn timed out after {timeout}s") from exc
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    yield {
        "type": "completed",
        "assistant_text": "".join(assistant_parts),
        "observations": observations,
        "session_id": session_id,
    }


async def _stream_turn_events(req: TurnRequest) -> AsyncIterator[str]:
    """Yield one turn as newline-delimited JSON for proxy-side streaming."""
    workspace = _resolve_workspace(req.workspace)
    provider = req.provider.lower()
    model = req.model

    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")

    if provider == "anthropic":

        async def spawn_fn(ws: str, prov: str) -> asyncio.subprocess.Process:
            return await _spawn_claude(ws, model)
    else:

        async def spawn_fn(ws: str, prov: str) -> asyncio.subprocess.Process:
            return await _spawn_codex(ws, model)

    session = await _session_table.get_or_create(workspace, provider, spawn_fn)

    async with session.lock:
        session.last_request_at = datetime.now(timezone.utc)
        try:
            if provider == "anthropic":
                async for event in _stream_claude_turn(
                    session, req.user_message, model, workspace
                ):
                    if event.get("type") == "completed":
                        session.turn_count += 1
                        if event.get("session_id"):
                            session.cli_session_id = str(event["session_id"])
                        if not event.get("session_id"):
                            machine, ws, prov = session.key
                            event["session_id"] = f"{prov}:{machine}:{ws}"
                    yield json.dumps(event, separators=(",", ":")) + "\n"
            else:
                assistant_text, observations, cli_id = await _run_codex_turn(
                    session, req.user_message, model, workspace
                )
                if assistant_text:
                    yield (
                        json.dumps(
                            {"type": "delta", "delta": assistant_text},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                session.turn_count += 1
                if cli_id:
                    session.cli_session_id = cli_id
                machine, ws, prov = session.key
                yield (
                    json.dumps(
                        {
                            "type": "completed",
                            "assistant_text": assistant_text,
                            "observations": observations,
                            "session_id": cli_id or f"{prov}:{machine}:{ws}",
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        except RuntimeError as exc:
            await _session_table.remove(session.key)
            logger.error("Streaming turn failed for session %s: %s", session.key, exc)
            yield (
                json.dumps({"type": "error", "error": str(exc)}, separators=(",", ":"))
                + "\n"
            )


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "sessions": str(len(_session_table))}


@app.post("/session/turn/stream")
async def session_turn_stream(req: TurnRequest) -> StreamingResponse:
    """Execute one turn and stream newline-delimited JSON events."""
    if req.provider.lower() not in ("anthropic", "openai"):
        raise HTTPException(
            status_code=400, detail=f"Unknown provider: {req.provider!r}"
        )
    return StreamingResponse(
        _stream_turn_events(req), media_type="application/x-ndjson"
    )


@app.post("/session/turn", response_model=TurnResponse)
async def session_turn(req: TurnRequest) -> TurnResponse:
    """Execute one turn in a session, creating the session if it does not exist.

    The per-session asyncio lock serialises concurrent requests for the same
    (workspace, provider) pair so that turns always execute in order.
    """
    workspace = _resolve_workspace(req.workspace)
    provider = req.provider.lower()
    model = req.model

    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")

    if provider == "anthropic":

        async def spawn_fn(ws: str, prov: str) -> asyncio.subprocess.Process:
            return await _spawn_claude(ws, model)
    else:

        async def spawn_fn(ws: str, prov: str) -> asyncio.subprocess.Process:
            return await _spawn_codex(ws, model)

    session = await _session_table.get_or_create(workspace, provider, spawn_fn)

    async with session.lock:
        # Update last_request_at under the session lock to avoid races.
        session.last_request_at = datetime.now(timezone.utc)

        try:
            if provider == "anthropic":
                assistant_text, observations, cli_id = await _run_claude_turn(
                    session=session,
                    user_message=req.user_message,
                    model=model,
                    workspace=workspace,
                )
            else:
                assistant_text, observations, cli_id = await _run_codex_turn(
                    session=session,
                    user_message=req.user_message,
                    model=model,
                    workspace=workspace,
                )
        except RuntimeError as exc:
            # On error, remove the broken session so the next request gets a
            # fresh one.
            await _session_table.remove(session.key)
            logger.error("Turn failed for session %s: %s", session.key, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Update session metadata.
        session.turn_count += 1
        if cli_id:
            session.cli_session_id = cli_id

        # Build the canonical session_id to return to the caller.
        # Format: provider:cli_session_id or provider:machine:workspace if no cli id yet.
        if cli_id:
            session_id_str = cli_id
        else:
            machine, ws, prov = session.key
            session_id_str = f"{prov}:{machine}:{ws}"

    logger.info(
        "Turn complete session=%s turn=%d obs=%d text_len=%d",
        session_id_str,
        session.turn_count,
        len(observations),
        len(assistant_text),
    )

    return TurnResponse(
        assistant_text=assistant_text,
        session_id=session_id_str,
        observations=observations,
    )
