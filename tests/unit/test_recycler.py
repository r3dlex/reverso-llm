"""Unit tests for the recycle policy and sweeper (src/reverso/daemon/recycler.py).

The pure policy (decide_recycle) is the test surface for the recycling rules:
idle threshold boundary, descendant veto, and probe laziness. The sweeper and
termination escalation are tested with fake processes and a fake session
table; no real process is ever signalled and psutil is never invoked against
a live pid.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta, timezone

import psutil
import pytest

import reverso.daemon.recycler as recycler
from reverso.daemon.recycler import (
    RecycleDecision,
    RecycleSweeper,
    _has_live_descendants,
    _minutes_since,
    _terminate_session,
    decide_recycle,
)
from reverso.daemon.session_table import Session


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process; records signals, never real."""

    def __init__(
        self,
        *,
        pid: int = 4242,
        returncode: int | None = None,
        exit_on: signal.Signals | None = signal.SIGTERM,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.signals: list[signal.Signals] = []
        self._exited = asyncio.Event()
        self._exit_on = exit_on

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        if self._exit_on is not None and sig == self._exit_on:
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = 0
        return 0


class _GoneProcess(_FakeProcess):
    """Process that disappears between the returncode check and the signal."""

    def send_signal(self, sig: signal.Signals) -> None:
        raise ProcessLookupError(self.pid)


def _session(
    process: _FakeProcess, *, idle_minutes: float, name: str = "ws"
) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        key=("machine", f"/tmp/{name}", "claude"),
        process=process,  # type: ignore[arg-type]
        spawned_at=now - timedelta(minutes=idle_minutes + 1),
        last_request_at=now - timedelta(minutes=idle_minutes),
    )


class _FakeTable:
    def __init__(self, sessions: list[Session]) -> None:
        self._sessions = {session.key: session for session in sessions}

    def all_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    async def remove(self, key) -> None:
        self._sessions.pop(key, None)

    def __contains__(self, key) -> bool:
        return key in self._sessions


def test_decide_recycle_keeps_active_without_probing() -> None:
    # The descendant probe must NOT run for sessions under the idle threshold:
    # the cheap idle comparison gates the psutil process-tree walk.
    probed = []

    decision = decide_recycle(
        idle_minutes=29.9,
        idle_threshold_minutes=30,
        probe_descendants=lambda: probed.append(True) or True,
    )

    assert decision is RecycleDecision.KEEP_ACTIVE
    assert probed == []


def test_decide_recycle_threshold_boundary_is_a_candidate() -> None:
    # Exactly at the threshold the session IS a candidate (strict < keeps),
    # so the descendant probe MUST run for it.
    probed = []
    decision = decide_recycle(
        idle_minutes=30.0,
        idle_threshold_minutes=30,
        probe_descendants=lambda: probed.append(True) or False,
    )
    assert decision is RecycleDecision.RECYCLE
    assert probed == [True]


def test_decide_recycle_descendants_veto() -> None:
    decision = decide_recycle(
        idle_minutes=45.0,
        idle_threshold_minutes=30,
        probe_descendants=lambda: True,
    )
    assert decision is RecycleDecision.KEEP_BUSY_DESCENDANTS


def test_decide_recycle_idle_and_quiet_recycles() -> None:
    decision = decide_recycle(
        idle_minutes=45.0,
        idle_threshold_minutes=30,
        probe_descendants=lambda: False,
    )
    assert decision is RecycleDecision.RECYCLE


def test_minutes_since_handles_naive_and_aware() -> None:
    # SessionTable records naive UTC timestamps; both forms must measure alike.
    aware = datetime.now(timezone.utc) - timedelta(minutes=10)
    naive = aware.replace(tzinfo=None)
    assert _minutes_since(aware) == pytest.approx(10.0, abs=0.1)
    assert _minutes_since(naive) == pytest.approx(10.0, abs=0.1)


class _FakeChild:
    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


class _FakeParent:
    def __init__(self, children: list[_FakeChild]) -> None:
        self._children = children

    def children(self, recursive: bool = False) -> list[_FakeChild]:
        assert recursive is True
        return self._children


def test_has_live_descendants_true_when_child_running(monkeypatch) -> None:
    monkeypatch.setattr(
        recycler.psutil, "Process", lambda pid: _FakeParent([_FakeChild(True)])
    )
    assert _has_live_descendants(123) is True


def test_has_live_descendants_false_when_no_children(monkeypatch) -> None:
    monkeypatch.setattr(recycler.psutil, "Process", lambda pid: _FakeParent([]))
    assert _has_live_descendants(123) is False


def test_has_live_descendants_false_when_process_gone(monkeypatch) -> None:
    def raise_gone(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(recycler.psutil, "Process", raise_gone)
    assert _has_live_descendants(123) is False


def test_has_live_descendants_conservative_on_access_denied(monkeypatch) -> None:
    # An uninspectable process is assumed live so it is never killed blindly.
    def raise_denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(recycler.psutil, "Process", raise_denied)
    assert _has_live_descendants(123) is True


async def test_sweep_recycles_only_idle_quiet_sessions() -> None:
    active_proc = _FakeProcess(pid=1)
    busy_proc = _FakeProcess(pid=2)
    idle_proc = _FakeProcess(pid=3)
    active = _session(active_proc, idle_minutes=5, name="active")
    busy = _session(busy_proc, idle_minutes=45, name="busy")
    idle = _session(idle_proc, idle_minutes=45, name="idle")
    table = _FakeTable([active, busy, idle])
    probed: list[int] = []

    def probe(pid: int) -> bool:
        probed.append(pid)
        return pid == busy_proc.pid

    sweeper = RecycleSweeper(table, descendant_probe=probe)  # type: ignore[arg-type]
    await sweeper._sweep()

    # Only the idle candidates were probed; the active session was not.
    assert sorted(probed) == [busy_proc.pid, idle_proc.pid]
    # The idle-and-quiet session was terminated and removed.
    assert signal.SIGTERM in idle_proc.signals
    assert idle.key not in table
    # The active and busy sessions were untouched.
    assert active_proc.signals == []
    assert busy_proc.signals == []
    assert active.key in table
    assert busy.key in table


async def test_terminate_session_skips_already_exited_process() -> None:
    proc = _FakeProcess(returncode=0)
    await _terminate_session(_session(proc, idle_minutes=45))
    assert proc.signals == []


async def test_terminate_session_sigterm_then_clean_exit() -> None:
    proc = _FakeProcess(exit_on=signal.SIGTERM)
    await _terminate_session(_session(proc, idle_minutes=45))
    assert proc.signals == [signal.SIGTERM]


async def test_terminate_session_escalates_to_sigkill(monkeypatch) -> None:
    # The process ignores SIGTERM; after the grace period SIGKILL is sent.
    monkeypatch.setattr(recycler, "_SIGTERM_GRACE_SECONDS", 0.01)
    proc = _FakeProcess(exit_on=signal.SIGKILL)

    await _terminate_session(_session(proc, idle_minutes=45))

    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]


async def test_terminate_session_tolerates_vanished_process() -> None:
    proc = _GoneProcess()
    await _terminate_session(_session(proc, idle_minutes=45))
    assert proc.returncode is None
