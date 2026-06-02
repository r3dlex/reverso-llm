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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from reverso.daemon.parsers.claude_code import ClaudeCodeParser
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
        "sleep", "infinity",
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
        "sleep", "infinity",
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
        "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
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
        parse_task = asyncio.create_task(
            parser.parse_stream(_aiter_lines(proc.stdout))
        )
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
            "exec", "resume", session.cli_session_id, user_message,
            "--json",
            "-s", "workspace-write",
            "--model", model,
            "-c", "skip_git_repo_check=true",
        ]
    else:
        cmd = [
            resolve_cli_command("codex", "REVERSO_CODEX_BIN"),
            "exec", user_message,
            "--json",
            "-s", "workspace-write",
            "--model", model,
            "-c", "skip_git_repo_check=true",
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
        parse_task = asyncio.create_task(
            parser.parse_stream(_aiter_lines(proc.stdout))
        )
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
            raise RuntimeError(
                f"codex exited {proc.returncode}; stderr: {stderr_text}"
            )

        thread_id = getattr(parser, "thread_id", None)

    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    return assistant_text, observations, thread_id


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "sessions": str(len(_session_table))}


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
