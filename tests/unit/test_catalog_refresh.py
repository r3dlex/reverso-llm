"""Scheduled catalog refresh runner tests."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from reverso import catalog_refresh, client_sync, client_sync_mutations, codex_sync
from reverso.client_sync_lock import ClientSyncLockBusy, HeldClientSyncLock
from reverso.client_sync_mutations import (
    PreparedGroup,
    apply_prepared_group,
    file_state,
    prepared_mutation,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_launch_agent_is_short_lived_and_has_exact_schedule() -> None:
    payload = plistlib.loads(
        Path("launchd/com.user.reverso-catalog-refresh.plist.tmpl").read_bytes()
    )

    assert payload["Label"] == "com.user.reverso-catalog-refresh"
    assert payload["StartCalendarInterval"] == [
        {"Hour": 6, "Minute": 0},
        {"Hour": 18, "Minute": 0},
    ]
    assert payload["ProgramArguments"][-1] == "reverso-catalog-refresh"
    assert "KeepAlive" not in payload
    assert "RunAtLoad" not in payload
    assert "Sockets" not in payload
    assert "MachServices" not in payload
    assert "StandardOutPath" not in payload
    assert "StandardErrorPath" not in payload
    rendered = Path("launchd/com.user.reverso-catalog-refresh.plist.tmpl").read_text(
        encoding="utf-8"
    )
    assert "reverso-proxy" not in rendered
    assert "reverso-daemon" not in rendered


def test_provider_discovery_uses_exact_ten_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {"data": [{"id": "model"}]}

    def get(_url: str, *, timeout: float) -> Response:
        observed.append(timeout)
        return Response()

    monkeypatch.setattr(codex_sync.httpx, "get", get)

    assert codex_sync.discover_provider_models("claude").models == ("model",)
    assert observed == [10.0]


def test_lock_skip_happens_before_discovery_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy(**_kwargs: Any) -> Any:
        @contextmanager
        def raise_busy() -> Any:
            raise ClientSyncLockBusy("busy")
            yield

        return raise_busy()

    monkeypatch.setattr(catalog_refresh, "acquire_client_sync_lock", busy)
    monkeypatch.setattr(
        client_sync,
        "run",
        lambda *_args, **_kwargs: pytest.fail("refresh must not run"),
    )

    assert (
        catalog_refresh.run(
            lock_path=tmp_path / "state" / "catalog-refresh.lock",
            status_path=tmp_path / "state" / "catalog-refresh-status.json",
            stdout_path=tmp_path / "logs" / "catalog-refresh.stdout.log",
            stderr_path=tmp_path / "logs" / "catalog-refresh.stderr.log",
        )
        == 0
    )
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()


def test_runner_uses_direct_unified_refresh_with_held_lock_and_complete_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = tmp_path / "state" / "catalog-refresh.lock"
    lock.parent.mkdir()
    lock.write_text("", encoding="utf-8")
    token = HeldClientSyncLock(lock, 123, os.getpid())
    observed: dict[str, Any] = {}
    events: list[str] = []
    append_log = catalog_refresh._append_log

    @contextmanager
    def held(**kwargs: Any) -> Any:
        events.append("lock")
        observed["lock_kwargs"] = kwargs
        yield token

    def refresh(mode: str, **kwargs: Any) -> dict[str, Any]:
        observed["mode"] = mode
        observed["run_kwargs"] = kwargs
        return {"exit_code": 0, "status": "success"}

    def record_append(path: Path, payload: bytes) -> None:
        events.append(f"append:{path.name}:{len(payload)}")
        append_log(path, payload)

    def set_timer(_which: int, seconds: float) -> None:
        events.append(f"timer:{seconds}")

    monkeypatch.setattr(catalog_refresh, "acquire_client_sync_lock", held)
    monkeypatch.setattr(catalog_refresh, "_append_log", record_append)
    monkeypatch.setattr(client_sync, "run", refresh)
    monkeypatch.setattr(catalog_refresh.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(catalog_refresh.signal, "setitimer", set_timer)

    assert (
        catalog_refresh.run(
            lock_path=lock,
            status_path=tmp_path / "state" / "catalog-refresh-status.json",
            stdout_path=tmp_path / "logs" / "catalog-refresh.stdout.log",
            stderr_path=tmp_path / "logs" / "catalog-refresh.stderr.log",
        )
        == 0
    )
    assert observed["lock_kwargs"] == {"path": lock, "blocking": False}
    assert observed["mode"] == "refresh"
    assert observed["run_kwargs"]["lock_path"] == lock
    assert observed["run_kwargs"]["lock_token"] is token
    assert events == [
        "timer:120.0",
        "lock",
        "append:catalog-refresh.stdout.log:0",
        "append:catalog-refresh.stderr.log:0",
        "append:catalog-refresh.stdout.log:35",
        "timer:0.0",
    ]


def test_logs_are_secure_and_rotate_independently_at_one_mib(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    stdout = logs / "catalog-refresh.stdout.log"
    stderr = logs / "catalog-refresh.stderr.log"

    @contextmanager
    def held(**_kwargs: Any) -> Any:
        state.mkdir(mode=0o700)
        lock = state / "catalog-refresh.lock"
        lock.touch(mode=0o600)
        yield HeldClientSyncLock(lock, 123, os.getpid())

    monkeypatch.setattr(catalog_refresh, "acquire_client_sync_lock", held)
    monkeypatch.setattr(catalog_refresh.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(catalog_refresh.signal, "setitimer", lambda *_args: None)
    monkeypatch.setattr(
        client_sync,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 0, "status": "success"},
    )

    logs.mkdir(mode=0o755)
    stdout.write_bytes(b"x" * catalog_refresh.LOG_MAX_BYTES)
    stderr.write_text("old-error\n", encoding="utf-8")
    for index in range(1, 4):
        stdout.with_name(f"{stdout.name}.{index}").write_text(
            str(index), encoding="utf-8"
        )

    assert (
        catalog_refresh.run(
            lock_path=state / "catalog-refresh.lock",
            status_path=state / "catalog-refresh-status.json",
            stdout_path=stdout,
            stderr_path=stderr,
        )
        == 0
    )

    assert _mode(state) == 0o700
    assert _mode(logs) == 0o700
    assert _mode(stdout) == 0o600
    assert _mode(stderr) == 0o600
    assert stdout.with_name(f"{stdout.name}.1").stat().st_size == (
        catalog_refresh.LOG_MAX_BYTES
    )
    assert stdout.with_name(f"{stdout.name}.2").read_text(encoding="utf-8") == "1"
    assert stdout.with_name(f"{stdout.name}.3").read_text(encoding="utf-8") == "2"
    assert not stderr.with_name(f"{stderr.name}.1").exists()
    assert json.loads(stdout.read_text(encoding="utf-8"))["status"] == "success"


def test_log_symlink_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    stdout = logs / "catalog-refresh.stdout.log"
    stdout.symlink_to(outside)

    @contextmanager
    def held(**_kwargs: Any) -> Any:
        yield HeldClientSyncLock(tmp_path / "lock", 123, os.getpid())

    monkeypatch.setattr(catalog_refresh, "acquire_client_sync_lock", held)
    monkeypatch.setattr(catalog_refresh.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(catalog_refresh.signal, "setitimer", lambda *_args: None)
    monkeypatch.setattr(
        client_sync,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "refresh must not run with a symlinked log"
        ),
    )

    assert (
        catalog_refresh.run(
            lock_path=tmp_path / "lock",
            status_path=tmp_path / "status.json",
            stdout_path=stdout,
            stderr_path=logs / "catalog-refresh.stderr.log",
        )
        == 5
    )
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_real_signal_timeout_during_planning_writes_exact_failed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "state" / "catalog-refresh-status.json"
    status_path.parent.mkdir(mode=0o700)
    prior = {
        "schema_version": 1,
        "status": "success",
        "last_attempt_at": "2026-07-29T05:00:00Z",
        "last_success_at": "2026-07-29T05:00:01Z",
        "duration_ms": 1000,
        "exit_code": 0,
        "stale": False,
        "stale_observed_at": "2026-07-29T05:00:01Z",
        "provider_results": {},
        "error_codes": [],
    }
    status_path.write_text(json.dumps(prior), encoding="utf-8")
    status_path.chmod(0o600)
    lock_path = tmp_path / "state" / "catalog-refresh.lock"
    events: list[str] = []
    acquire_lock = catalog_refresh.acquire_client_sync_lock

    @contextmanager
    def held(**kwargs: Any) -> Any:
        with acquire_lock(**kwargs) as token:
            events.append("locked")
            try:
                yield token
            finally:
                events.append("released")

    record_timeout = catalog_refresh._record_timeout

    def record_while_locked(path: Path, result: dict[str, Any]) -> None:
        events.append("timeout_status")
        record_timeout(path, result)

    timestamps = iter(("2026-07-30T05:00:00Z", "2026-07-30T05:02:00Z"))
    monkeypatch.setattr(catalog_refresh, "acquire_client_sync_lock", held)
    monkeypatch.setattr(catalog_refresh, "_record_timeout", record_while_locked)
    monkeypatch.setattr(catalog_refresh, "_timestamp", lambda: next(timestamps))

    def time_out_during_planning(**_kwargs: Any) -> Any:
        catalog_refresh.signal.raise_signal(catalog_refresh.signal.SIGALRM)
        pytest.fail("SIGALRM must interrupt client sync planning")

    monkeypatch.setattr(client_sync, "_plan", time_out_during_planning)

    exit_code = catalog_refresh.run(
        lock_path=lock_path,
        status_path=status_path,
        stdout_path=tmp_path / "logs" / "catalog-refresh.stdout.log",
        stderr_path=tmp_path / "logs" / "catalog-refresh.stderr.log",
    )
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    emitted = json.loads(
        (tmp_path / "logs" / "catalog-refresh.stdout.log").read_text(encoding="utf-8")
    )

    assert exit_code == 5
    assert events == ["locked", "timeout_status", "released"]
    assert set(emitted) == set(client_sync.RESULT_FIELDS)
    assert emitted["command"] == client_sync.COMMAND
    assert emitted["status"] == "repair_required"
    assert emitted["catalog_refresh"]["last_attempt_at"] == ("2026-07-30T05:00:00Z")
    assert emitted["catalog_refresh"]["last_success_at"] == prior["last_success_at"]
    assert emitted["catalog_refresh"]["stored_stale_observed_at"] == (
        "2026-07-30T05:02:00Z"
    )
    assert persisted["status"] == "failed"
    assert persisted["exit_code"] == 5
    assert persisted["error_codes"] == ["overall_timeout"]
    assert persisted["last_success_at"] == prior["last_success_at"]
    assert persisted["last_attempt_at"] == "2026-07-30T05:00:00Z"
    assert persisted["stale_observed_at"] == "2026-07-30T05:02:00Z"
    assert persisted["stale_observed_at"] != prior["stale_observed_at"]
    assert set(persisted) == {
        "schema_version",
        "status",
        "last_attempt_at",
        "last_success_at",
        "duration_ms",
        "exit_code",
        "stale",
        "stale_observed_at",
        "provider_results",
        "error_codes",
    }
    assert _mode(status_path.parent) == 0o700
    assert _mode(status_path) == 0o600


def test_real_signal_timeout_during_replacement_rolls_back_and_records_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "state" / "catalog-refresh-status.json"
    target = tmp_path / "managed-profile"
    target.write_text("before\n", encoding="utf-8")
    group = PreparedGroup(
        "provider-test",
        (prepared_mutation(target, file_state("after\n")),),
    )
    original_create = client_sync_mutations._create_state

    def time_out_during_replacement(
        parent_fd: int,
        name: str,
        state: Any,
    ) -> None:
        if name == target.name and state == group.mutations[0].after:
            catalog_refresh.signal.raise_signal(catalog_refresh.signal.SIGALRM)
            raise AssertionError("SIGALRM must interrupt candidate replacement")
        original_create(parent_fd, name, state)

    def refresh(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        apply_prepared_group(group)
        raise AssertionError("replacement timeout must escape unified refresh")

    monkeypatch.setattr(
        client_sync_mutations,
        "_create_state",
        time_out_during_replacement,
    )
    monkeypatch.setattr(client_sync, "run", refresh)

    exit_code = catalog_refresh.run(
        lock_path=tmp_path / "state" / "catalog-refresh.lock",
        status_path=status_path,
        stdout_path=tmp_path / "logs" / "catalog-refresh.stdout.log",
        stderr_path=tmp_path / "logs" / "catalog-refresh.stderr.log",
    )
    persisted = json.loads(status_path.read_text(encoding="utf-8"))

    assert exit_code == 5
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not tuple(tmp_path.glob(".*.guard"))
    assert persisted["status"] == "failed"
    assert persisted["exit_code"] == 5
    assert persisted["error_codes"] == ["overall_timeout"]


def test_uninstaller_preserves_by_default_and_purges_only_exact_artifacts() -> None:
    script = Path("scripts/uninstall-launchagents.sh").read_text(encoding="utf-8")

    assert "com.user.reverso-catalog-refresh" in script
    assert 'if [[ "$#" -gt 1 ]]' in script
    assert '"--purge-state"' in script
    assert 'rm -f "${artifact}"' in script
    assert "catalog-refresh.lock" in script
    assert "catalog-refresh-status.json" in script
    assert "catalog-refresh.stdout.log.3" in script
    assert "catalog-refresh.stderr.log.3" in script
    assert "rm -rf" not in script
    assert "catalog-refresh.*" not in script


def test_uninstaller_default_preserves_and_purge_removes_exact_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    state = home / "Library" / "Application Support" / "reverso"
    logs = home / "Library" / "Logs" / "reverso"
    bin_dir = tmp_path / "bin"
    for directory in (launch_agents, state, logs, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)

    governed = [
        state / "catalog-refresh.lock",
        state / "catalog-refresh-status.json",
        *(
            logs / f"catalog-refresh.{stream}.log{suffix}"
            for stream in ("stdout", "stderr")
            for suffix in ("", ".1", ".2", ".3")
        ),
    ]
    unrelated = state / "unrelated"
    for path in (*governed, unrelated):
        path.write_text("preserve", encoding="utf-8")
    for label in (
        "com.user.reverso-proxy",
        "com.user.reverso-daemon",
        "com.user.reverso-catalog-refresh",
    ):
        (launch_agents / f"{label}.plist").write_text("plist", encoding="utf-8")

    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    default = subprocess.run(
        ["bash", "scripts/uninstall-launchagents.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert default.returncode == 0
    assert all(path.exists() for path in governed)
    assert not any(launch_agents.iterdir())

    purged = subprocess.run(
        ["bash", "scripts/uninstall-launchagents.sh", "--purge-state"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert purged.returncode == 0
    assert not any(path.exists() for path in governed)
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_installer_renders_and_loads_scheduled_job_after_service_drift_gate() -> None:
    script = Path("scripts/install-launchagents.sh").read_text(encoding="utf-8")

    assert 'SCHEDULED_AGENT="com.user.reverso-catalog-refresh"' in script
    assert 'AGENTS=("${LONG_LIVED_AGENTS[@]}" "${SCHEDULED_AGENT}")' in script
    assert 'for AGENT in "${AGENTS[@]}"; do' in script
    assert 'for AGENT in "${LONG_LIVED_AGENTS[@]}"; do' in script
    post_restart = script.index("run_deployment_drift --phase post-restart")
    scheduled_load = script.index('launchctl load "${SCHEDULED_PLIST}"')
    one_shot = script.index(
        '"${UV_BIN}" run --project "${REVERSO_DIR}" reverso-catalog-refresh'
    )
    assert post_restart < scheduled_load
    assert scheduled_load < one_shot
    assert (
        script[
            script.index("LONG_LIVED_AGENTS=(") : script.index('SCHEDULED_AGENT="')
        ].count("com.user.reverso-")
        == 2
    )
    assert 'STATE_DIR="${USER_HOME}/Library/Application Support/reverso"' in script
    assert 'prepare_private_directory "${STATE_DIR}"' in script
    assert 'prepare_private_directory "${LOG_DIR}"' in script
    assert 'chmod 0700 "${directory}"' in script
    assert '-L "${directory}"' in script


def test_scheduled_agent_stays_outside_long_lived_drift_map() -> None:
    from reverso import deployment_drift

    assert deployment_drift.LAUNCH_AGENT_EXECUTABLES == {
        "com.user.reverso-proxy": "reverso-proxy",
        "com.user.reverso-daemon": "reverso-daemon",
    }
