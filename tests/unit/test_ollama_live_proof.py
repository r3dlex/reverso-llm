from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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
    return subprocess.CompletedProcess(argv, 0, stdout="tool 1.0")


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


def test_signin_requires_both_flags_and_ttys() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="version" if argv[-1] == "--version" else None
        )

    report = run_proof(
        _inputs(attended=True, authorize_signin=False),
        runner=runner,
        inventory_loader=lambda _: _snapshot(status="auth_required"),
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
    )
    assert report["exit_code"] == 2
    assert not any(call[-1] == "signin" for call in calls)


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
            argv, 0, stdout="version" if argv[-1] == "--version" else None
        )

    report = run_proof(
        _inputs(attended=True, authorize_signin=True),
        runner=runner,
        inventory_loader=loader,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={"OLLAMA_API_KEY": "secret"},
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
            return subprocess.CompletedProcess(argv, 0, stdout="version")
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
    )
    assert report["exit_code"] == 1
    assert proof_calls == 1
    assert len(report["lanes"]) == 1
