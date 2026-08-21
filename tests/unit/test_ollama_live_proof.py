from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from reverso.ollama_convergence import InventoryEntry, InventorySnapshot
from reverso.ollama_live_proof import ProofInputs, run_proof, select_candidates


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


def _inputs(**changes: Any) -> ProofInputs:
    values = {
        "mode": "run",
        "inventory_path": Path("/inventory"),
        "ollama_executable": Path("/bin/ollama"),
        "codex_executable": Path("/bin/codex"),
        "claude_launcher": Path("/bin/claude-ollama"),
        "client_sync_executable": Path("/bin/reverso-client-sync"),
    }
    values.update(changes)
    return ProofInputs(**values)


def _snapshot(*entries: InventoryEntry, status: str = "current") -> InventorySnapshot:
    return InventorySnapshot(entries, "now", "current", "current", status)


def _version_runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, stdout=_version_output(argv[0]))


def _version_output(executable: str) -> str:
    name = Path(executable).name
    return {
        "ollama": "ollama version is 1.0",
        "codex": "codex-cli 1.0",
        "claude-ollama": "1.0 (Claude Code)",
    }[name]


def _valid_path(_: Path) -> bool:
    return True


def test_candidate_authority_excludes_stale_or_noncurrent_cloud() -> None:
    entries = (
        InventoryEntry("local", True, False, False),
        InventoryEntry("stale", False, True, True),
        InventoryEntry("current-cloud", False, True, False),
    )
    assert select_candidates(_snapshot(*entries)).cloud == "current-cloud"
    assert select_candidates(_snapshot(*entries, status="timeout")).cloud is None


def test_relative_executable_is_invalid_without_effects() -> None:
    touched = False

    def loader(_: Path) -> InventorySnapshot:
        nonlocal touched
        touched = True
        raise AssertionError

    report = run_proof(
        _inputs(ollama_executable=Path("ollama")),
        runner=_version_runner,
        inventory_loader=loader,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
    )
    assert report["exit_code"] == 64
    assert touched is False


def test_relative_inventory_is_invalid_without_effects() -> None:
    touched = False

    def loader(_: Path) -> InventorySnapshot:
        nonlocal touched
        touched = True
        raise AssertionError

    report = run_proof(
        _inputs(inventory_path=Path("relative-inventory.json")),
        runner=_version_runner,
        inventory_loader=loader,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )

    assert report["exit_code"] == 64
    assert touched is False


def test_missing_or_unmarked_executables_cannot_pass(tmp_path: Path) -> None:
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    report = run_proof(
        _inputs(
            ollama_executable=executable,
            codex_executable=executable,
            claude_launcher=executable,
            client_sync_executable=tmp_path / "missing-sync",
        ),
        runner=_version_runner,
        inventory_loader=lambda _: _snapshot(),
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
    )

    assert report["exit_code"] == 64
    assert report["status"] == "invalid"


@pytest.mark.parametrize(
    "unsafe_version",
    (
        "echo 1.0",
        "ollama version is https://enroll.invalid/?token=secret",
        "codex-cli sk-secret-token",
        "https://secret.invalid/token (Claude Code)",
    ),
)
def test_unexpected_or_unsafe_tool_identity_cannot_pass(unsafe_version: str) -> None:
    report = run_proof(
        _inputs(),
        runner=lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, stdout=unsafe_version
        ),
        inventory_loader=lambda _: _snapshot(
            InventoryEntry("local", True, False, False),
            InventoryEntry("cloud", False, True, False),
        ),
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )

    assert report["exit_code"] == 2
    assert report["status"] == "external_prerequisite"
    assert report["versions"] == {}


def test_version_timeout_is_bounded_without_exception_text() -> None:
    def timeout_runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 0.01, output="private output")

    report = run_proof(
        _inputs(),
        runner=timeout_runner,
        inventory_loader=lambda _: _snapshot(),
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
        timeout=0.01,
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )

    assert report["exit_code"] == 2
    assert report["prerequisites"] == ["proof_environment_required"]
    assert "private output" not in repr(report)


@pytest.mark.parametrize(
    ("mode", "status", "attended", "authorize_signin", "stdin_tty", "stdout_tty"),
    (
        ("preflight", "auth_required", True, True, True, True),
        ("run", "current", True, True, True, True),
        ("run", "auth_required", False, True, True, True),
        ("run", "auth_required", True, False, True, True),
        ("run", "auth_required", True, True, False, True),
        ("run", "auth_required", True, True, True, False),
    ),
)
def test_signin_requires_every_mode_flag_auth_and_tty_gate(
    mode: str,
    status: str,
    attended: bool,
    authorize_signin: bool,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_version_output(argv[0]) if argv[-1] == "--version" else None,
        )

    report = run_proof(
        _inputs(
            mode=mode,
            attended=attended,
            authorize_signin=authorize_signin,
        ),
        runner=runner,
        inventory_loader=lambda _: _snapshot(status=status),
        clock=Clock(),
        stdin_isatty=lambda: stdin_tty,
        stdout_isatty=lambda: stdout_tty,
        env={},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )
    assert report["exit_code"] == 2
    assert not any(call[-1] == "signin" for call in calls)
    assert not any(call[-2:] == ["refresh", "--json"] for call in calls)


def test_authorized_signin_runs_once_then_one_refresh_and_one_reload() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    loads = 0

    def loader(_: Path) -> InventorySnapshot:
        nonlocal loads
        loads += 1
        if loads == 1:
            return _snapshot(
                InventoryEntry("local", True, False, False),
                status="auth_required",
            )
        return _snapshot(
            InventoryEntry("local", True, False, False),
            InventoryEntry("cloud", False, True, False),
        )

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_version_output(argv[0]) if argv[-1] == "--version" else None,
        )

    report = run_proof(
        _inputs(attended=True, authorize_signin=True),
        runner=runner,
        inventory_loader=loader,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={"OLLAMA_API_KEY": "secret"},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )

    assert report["exit_code"] == 0
    assert loads == 2
    assert sum(argv[-1] == "signin" for argv, _ in calls) == 1
    assert sum(argv[-2:] == ["refresh", "--json"] for argv, _ in calls) == 1
    signin_kwargs = next(kwargs for argv, kwargs in calls if argv[-1] == "signin")
    assert "stdin" not in signin_kwargs
    assert "stdout" not in signin_kwargs
    assert "stderr" not in signin_kwargs
    assert "OLLAMA_API_KEY" not in signin_kwargs["env"]


def test_failed_lane_stops_remaining_lanes() -> None:
    proof_calls = 0

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        nonlocal proof_calls
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout=_version_output(argv[0]))
        proof_calls += 1
        return subprocess.CompletedProcess(argv, 1)

    report = run_proof(
        _inputs(),
        runner=runner,
        inventory_loader=lambda _: _snapshot(
            InventoryEntry("local", True, False, False),
            InventoryEntry("cloud", False, True, False),
        ),
        clock=Clock(),
        stdin_isatty=lambda: False,
        stdout_isatty=lambda: False,
        env={},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
    )
    assert report["exit_code"] == 1
    assert proof_calls == 1
    assert len(report["lanes"]) == 1
