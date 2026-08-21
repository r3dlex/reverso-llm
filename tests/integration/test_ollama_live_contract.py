from __future__ import annotations

import subprocess
import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

from reverso.ollama_convergence import InventoryEntry, InventorySnapshot
from reverso.ollama_live_proof import ProofInputs, run_proof


class Clock:
    value = 0.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


def _inputs(mode: str = "run", **changes: Any) -> ProofInputs:
    values = {
        "mode": mode,
        "inventory_path": Path("/inventory.json"),
        "ollama_executable": Path("/usr/local/bin/ollama"),
        "codex_executable": Path("/usr/local/bin/codex"),
        "claude_launcher": Path("/Users/test/.local/bin/claude-ollama"),
        "client_sync_executable": Path("/Users/test/.local/bin/reverso-client-sync"),
    }
    values.update(changes)
    return ProofInputs(**values)


def _snapshot(cloud_status: str = "current") -> InventorySnapshot:
    return InventorySnapshot(
        (
            InventoryEntry("local.raw:7b", True, False, False),
            InventoryEntry("cloud.raw:latest", False, True, False),
        ),
        "2026-08-21T00:00:00Z",
        "current",
        "current",
        cloud_status,
    )


def test_run_contract_is_four_sequential_secret_free_lanes() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = f"{Path(argv[0]).name} 1.0" if argv[-1] == "--version" else None
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    report = run_proof(
        _inputs(),
        runner=runner,
        inventory_loader=lambda _: _snapshot(),
        clock=Clock(),
        stdin_isatty=lambda: False,
        stdout_isatty=lambda: False,
        env={"OLLAMA_API_KEY": "canary", "PATH": "/usr/bin"},
    )

    assert report["exit_code"] == 0
    assert [lane["surface"] for lane in report["lanes"]] == [
        "codex_responses",
        "claude_messages",
        "codex_responses",
        "claude_messages",
    ]
    assert [lane["protocol"] for lane in report["lanes"]] == [
        "ollama_responses",
        "ollama_messages",
        "ollama_responses",
        "ollama_messages",
    ]
    proof_calls = calls[3:]
    assert len(proof_calls) == 4
    assert all(call[1]["shell"] is False for call in proof_calls)
    assert all(call[1]["stdout"] is subprocess.DEVNULL for call in proof_calls)
    assert all(call[1]["stderr"] is subprocess.DEVNULL for call in proof_calls)
    assert all("OLLAMA_API_KEY" not in call[1]["env"] for call in calls)
    assert "canary" not in repr(report)
    assert "Reply with" not in repr(report)


def test_preflight_records_exact_missing_external_prerequisites() -> None:
    snapshot = InventorySnapshot((), "now", "local_only", "required", "auth_required")

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="version 1")

    report = run_proof(
        _inputs("preflight"),
        runner=runner,
        inventory_loader=lambda _: snapshot,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
    )

    assert report["exit_code"] == 2
    assert report["prerequisites"] == [
        "cloud_auth_required",
        "local_model_required",
    ]
    assert report["lanes"] == []


def _cli_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "ollama-live-proof.py"
    spec = importlib.util.spec_from_file_location("ollama_live_proof_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli_args(evidence: Path) -> list[str]:
    return [
        "preflight",
        "--json",
        "--evidence",
        str(evidence),
        "--ollama-executable",
        "/bin/ollama",
        "--codex-executable",
        "/bin/codex",
        "--claude-launcher",
        "/bin/claude-ollama",
        "--client-sync-executable",
        "/bin/reverso-client-sync",
    ]


def test_cli_json_and_evidence_are_exact_same_public_report(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    module = _cli_module()
    report = {
        "schema_version": 1,
        "mode": "preflight",
        "status": "passed",
        "exit_code": 0,
        "prerequisites": [],
        "versions": {"ollama": "1.0"},
        "candidates": {"local": "local", "cloud": "cloud"},
        "lanes": [],
        "duration_ms": 1,
    }
    monkeypatch.setattr(module, "run_proof", lambda *_args, **_kwargs: report)
    evidence = tmp_path / "proof.json"

    assert module.main(_cli_args(evidence)) == 0

    stdout = capsys.readouterr().out
    assert evidence.read_text() == stdout
    assert json.loads(stdout) == report
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600


def test_cli_rejects_unsafe_evidence_path_before_proof(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    module = _cli_module()
    touched = False

    def tripwire(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal touched
        touched = True
        raise AssertionError

    monkeypatch.setattr(module, "run_proof", tripwire)
    target = tmp_path / "missing" / "proof.json"

    assert module.main(_cli_args(target)) == 64
    assert touched is False
    assert json.loads(capsys.readouterr().out)["prerequisites"] == [
        "invalid_evidence_path"
    ]


def test_cli_evidence_write_failure_is_bounded_failure(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    module = _cli_module()
    monkeypatch.setattr(
        module,
        "run_proof",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "mode": "preflight",
            "status": "passed",
            "exit_code": 0,
            "prerequisites": [],
            "versions": {},
            "candidates": {"local": "local", "cloud": "cloud"},
            "lanes": [],
            "duration_ms": 1,
        },
    )

    def fail_write(_path: Path, _encoded: str) -> None:
        raise OSError("private filesystem detail")

    monkeypatch.setattr(module, "_write_evidence", fail_write)

    assert module.main(_cli_args(tmp_path / "proof.json")) == 1
    public = json.loads(capsys.readouterr().out)
    assert public["status"] == "failed"
    assert public["prerequisites"] == ["evidence_write_failed"]
    assert "private filesystem detail" not in repr(public)
