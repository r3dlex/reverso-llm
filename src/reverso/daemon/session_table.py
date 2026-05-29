"""Session table - in-memory registry of active wrapped CLI subprocesses.

Each session is keyed by (machine_id, workspace_abs_path, provider_name) and
holds a reference to the long-lived CLI subprocess along with lifecycle metadata.
"""
from __future__ import annotations

import asyncio
import platform
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable


# Session key: (machine_id, workspace_abs_path, provider_name)
SessionKey = tuple[str, str, str]


def _machine_id() -> str:
    """Return a stable identifier for the current machine."""
    return platform.node()


@dataclass
class Session:
    """Represents one long-lived wrapped CLI subprocess."""

    key: SessionKey
    process: asyncio.subprocess.Process
    spawned_at: datetime
    last_request_at: datetime
    turn_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Opaque session/thread identifier returned by the CLI (uuid for Claude,
    # ulid for Codex). Populated after the first turn completes.
    cli_session_id: str | None = None


# Type alias for the async factory that spawns a new subprocess.
SpawnFn = Callable[[str, str], Awaitable[asyncio.subprocess.Process]]


class SessionTable:
    """Thread-safe (asyncio-safe) in-memory session registry.

    Uses a single asyncio.Lock to protect mutations to the underlying dict.
    Each individual Session also carries its own lock so that concurrent
    requests for the same session are serialised at the session level without
    blocking other sessions.
    """

    def __init__(self) -> None:
        self._sessions: dict[SessionKey, Session] = {}
        self._table_lock: asyncio.Lock = asyncio.Lock()

    def _make_key(self, workspace: str, provider: str) -> SessionKey:
        return (_machine_id(), workspace, provider)

    async def get_or_create(
        self,
        workspace: str,
        provider: str,
        spawn_fn: SpawnFn,
    ) -> Session:
        """Return the existing session for (workspace, provider) or create one.

        ``spawn_fn(workspace, provider)`` is called under the table lock only
        when no session exists yet.  It must return an
        ``asyncio.subprocess.Process`` that is ready to receive input.
        """
        key = self._make_key(workspace, provider)
        async with self._table_lock:
            if key in self._sessions:
                return self._sessions[key]
            process = await spawn_fn(workspace, provider)
            now = datetime.utcnow()
            session = Session(
                key=key,
                process=process,
                spawned_at=now,
                last_request_at=now,
            )
            self._sessions[key] = session
            return session

    async def remove(self, key: SessionKey) -> None:
        """Remove a session from the table (does NOT terminate the process)."""
        async with self._table_lock:
            self._sessions.pop(key, None)

    def all_sessions(self) -> list[Session]:
        """Return a snapshot list of all current sessions."""
        return list(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)
