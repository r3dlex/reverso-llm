"""Ephemeral attended-grant registry for the OpenRouter provider (OR-G1, U4).

Capabilities are unguessable strings bound to an exact launcher instance and
exact upstream model. They live only in memory and are revoked immediately on
launcher exit, on gateway restart, or after the 24-hour maximum TTL. PID,
catalog membership, parent process, or model alone never authorize a request.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable, Protocol

__all__ = [
    "GrantRegistry",
    "InvalidGrantError",
    "LauncherLiveness",
    "RevocationReason",
]


class InvalidGrantError(PermissionError):
    """A capability cannot authorize the requested admission."""


class LauncherLiveness(Protocol):
    """A reversible liveness flag the registry can check on every admission."""

    def __call__(self) -> bool: ...


@dataclass(frozen=True)
class RevocationReason:
    """Why a grant was rejected."""

    cause: str


@dataclass
class _Grant:
    capability: str
    launcher_pid: int
    model: str
    issued_at: float
    ttl_seconds: float

    def is_alive(self, clock: Callable[[], float], liveness: LauncherLiveness) -> bool:
        if not liveness():
            return False
        return clock() - self.issued_at <= self.ttl_seconds


class GrantRegistry:
    """In-memory registry of per-launcher, per-model capabilities."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        self._clock: clock = clock or _default_clock
        self._max_ttl_seconds = max_ttl_seconds
        self._grants: dict[str, _Grant] = {}

    @property
    def active_capabilities(self) -> int:
        return len(self._grants)

    def issue(
        self,
        *,
        launcher_pid: int,
        model: str,
        liveness: LauncherLiveness,
        ttl_seconds: float | None = None,
    ) -> str:
        """Issue a new grant bound to ``launcher_pid`` and ``model``."""
        ttl = min(self._max_ttl_seconds, ttl_seconds or self._max_ttl_seconds)
        capability = secrets.token_urlsafe(24)
        self._grants[capability] = _Grant(
            capability=capability,
            launcher_pid=launcher_pid,
            model=model,
            issued_at=self._clock(),
            ttl_seconds=ttl,
        )
        # touch liveness so dead launchers cannot be issued grants
        if not liveness():
            self._grants.pop(capability, None)
            raise InvalidGrantError("launcher is not alive; cannot issue grant")
        return capability

    def verify(
        self,
        *,
        capability: str,
        launcher_pid: int,
        model: str,
        liveness: LauncherLiveness,
    ) -> _Grant:
        grant = self._grants.get(capability)
        if grant is None:
            raise InvalidGrantError("capability is unknown")
        if grant.launcher_pid != launcher_pid:
            self._revoke(capability)
            raise InvalidGrantError("capability is bound to a different launcher")
        if grant.model != model:
            self._revoke(capability)
            raise InvalidGrantError("capability is bound to a different model")
        if not grant.is_alive(self._clock, liveness):
            self._revoke(capability)
            raise InvalidGrantError("capability expired or launcher is dead")
        return grant

    def revoke(self, capability: str) -> None:
        self._revoke(capability)

    def revoke_all(self) -> None:
        self._grants.clear()

    def _revoke(self, capability: str) -> None:
        self._grants.pop(capability, None)


def _default_clock() -> float:
    import time

    return time.monotonic()
