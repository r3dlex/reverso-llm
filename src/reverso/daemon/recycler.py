"""Recycle sweeper - periodically terminates idle wrapped CLI sessions.

Runs as a long-lived asyncio task inside the session daemon process.
Every 60 minutes it walks the session table and terminates any session that:
  1. Has had no requests for more than 30 minutes, AND
  2. Has no live descendant processes (checked via psutil).

The 30-minute idle threshold matches config.yaml session_idle_timeout_minutes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

import psutil

from reverso.daemon.session_table import Session, SessionTable

logger = logging.getLogger(__name__)

# How often the sweeper wakes up to check for idle sessions (seconds).
_SWEEP_INTERVAL_SECONDS = 60 * 60  # 60 minutes

# Sessions idle longer than this are candidates for recycling.
_IDLE_THRESHOLD_MINUTES = 30

# Grace period between SIGTERM and SIGKILL (seconds).
_SIGTERM_GRACE_SECONDS = 5


class RecycleDecision(Enum):
    """Outcome of the recycle policy for one session in one sweep pass."""

    RECYCLE = "recycle"
    KEEP_ACTIVE = "keep_active"
    KEEP_BUSY_DESCENDANTS = "keep_busy_descendants"


def decide_recycle(
    *,
    idle_minutes: float,
    idle_threshold_minutes: float,
    probe_descendants: Callable[[], bool],
) -> RecycleDecision:
    """Pure recycle policy: recycle iff idle past threshold AND no descendants.

    The descendant probe is invoked only after the idle threshold is crossed,
    so the cheap idle comparison gates the (psutil) process-tree walk. A
    session exactly at the threshold is a recycle candidate. The probe itself
    decides how to treat unobservable processes (see _has_live_descendants:
    AccessDenied is conservatively treated as live).
    """
    if idle_minutes < idle_threshold_minutes:
        return RecycleDecision.KEEP_ACTIVE
    if probe_descendants():
        return RecycleDecision.KEEP_BUSY_DESCENDANTS
    return RecycleDecision.RECYCLE


def _has_live_descendants(pid: int) -> bool:
    """Return True if the process with *pid* has any live descendant processes."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        return any(child.is_running() for child in children)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Cannot inspect the process - assume live to be conservative.
        logger.warning(
            "Access denied checking descendants of pid %d; assuming live", pid
        )
        return True


def _minutes_since(dt: datetime) -> float:
    """Return elapsed minutes since *dt* (assumed UTC naive or UTC aware)."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


async def _terminate_session(session: Session) -> None:
    """Send SIGTERM to the session process, wait, then SIGKILL if still alive."""
    proc = session.process
    pid = proc.pid

    # Check if already exited.
    if proc.returncode is not None:
        logger.debug(
            "Session %s process %d already exited (rc=%d)",
            session.key,
            pid,
            proc.returncode,
        )
        return

    logger.info(
        "Recycling idle session %s (pid=%d, idle=%.1f min)",
        session.key,
        pid,
        _minutes_since(session.last_request_at),
    )
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        logger.debug("Session %s pid %d already gone at SIGTERM", session.key, pid)
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_GRACE_SECONDS)
        logger.debug("Session %s pid %d exited after SIGTERM", session.key, pid)
    except asyncio.TimeoutError:
        logger.warning(
            "Session %s pid %d did not exit after SIGTERM; sending SIGKILL",
            session.key,
            pid,
        )
        try:
            proc.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.error(
                "Session %s pid %d did not exit after SIGKILL", session.key, pid
            )


class RecycleSweeper:
    """Asyncio background task that recycles idle sessions.

    Usage::

        sweeper = RecycleSweeper(session_table)
        task = asyncio.create_task(sweeper.run())
        # ...later:
        task.cancel()
    """

    def __init__(
        self,
        table: SessionTable,
        sweep_interval: float = _SWEEP_INTERVAL_SECONDS,
        idle_threshold_minutes: float = _IDLE_THRESHOLD_MINUTES,
        descendant_probe: Callable[[int], bool] = _has_live_descendants,
    ) -> None:
        self._table = table
        self._sweep_interval = sweep_interval
        self._idle_threshold = idle_threshold_minutes
        # Injectable for tests; defaults to the real psutil process-tree walk.
        self._descendant_probe = descendant_probe

    async def run(self) -> None:
        """Main loop - runs until cancelled."""
        logger.info(
            "RecycleSweeper started: sweep_interval=%.0fs idle_threshold=%.0fmin",
            self._sweep_interval,
            self._idle_threshold,
        )
        while True:
            try:
                await asyncio.sleep(self._sweep_interval)
                await self._sweep()
            except asyncio.CancelledError:
                logger.info("RecycleSweeper cancelled, stopping")
                raise
            except Exception as exc:
                logger.exception("RecycleSweeper sweep failed: %s", exc)

    async def _sweep(self) -> None:
        """One sweep pass: observe each session, ask the policy, then act."""
        sessions = self._table.all_sessions()
        logger.debug("RecycleSweeper sweeping %d session(s)", len(sessions))

        for session in sessions:
            idle_minutes = _minutes_since(session.last_request_at)
            pid = session.process.pid
            decision = decide_recycle(
                idle_minutes=idle_minutes,
                idle_threshold_minutes=self._idle_threshold,
                probe_descendants=lambda pid=pid: self._descendant_probe(pid),
            )
            if decision is RecycleDecision.KEEP_ACTIVE:
                continue
            if decision is RecycleDecision.KEEP_BUSY_DESCENDANTS:
                logger.debug(
                    "Session %s is idle (%.1f min) but has live descendants - keeping",
                    session.key,
                    idle_minutes,
                )
                continue

            await _terminate_session(session)
            await self._table.remove(session.key)
            logger.info("Session %s removed from table after recycling", session.key)
