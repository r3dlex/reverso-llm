from __future__ import annotations

import subprocess
import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

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


def _version_output(executable: str) -> str:
    return {
        "ollama": "ollama version is 1.0",
        "codex": "codex-cli 1.0",
        "claude-ollama": "1.0 (Claude Code)",
    }[Path(executable).name]


def _valid_path(_: Path) -> bool:
    return True


def test_run_contract_is_four_sequential_secret_free_lanes() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = _version_output(argv[0]) if argv[-1] == "--version" else None
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    report = run_proof(
        _inputs(),
        runner=runner,
        inventory_loader=lambda _: _snapshot(),
        clock=Clock(),
        stdin_isatty=lambda: False,
        stdout_isatty=lambda: False,
        env={"OLLAMA_API_KEY": "canary", "PATH": "/usr/bin"},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
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
    assert all(set(call[1]["env"]) <= {"PATH"} for call in calls)
    assert all(
        "--sandbox" in argv and "read-only" in argv for argv, _ in proof_calls[::2]
    )
    assert all('approval_policy="never"' in argv for argv, _ in proof_calls[::2])
    assert all("--tools" in argv and "" in argv for argv, _ in proof_calls[1::2])
    assert all(
        "--permission-mode" in argv and "dontAsk" in argv
        for argv, _ in proof_calls[1::2]
    )
    assert "canary" not in repr(report)
    assert "Reply with" not in repr(report)


def test_preflight_records_exact_missing_external_prerequisites() -> None:
    snapshot = InventorySnapshot((), "now", "local_only", "required", "auth_required")

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=_version_output(argv[0]))

    report = run_proof(
        _inputs("preflight"),
        runner=runner,
        inventory_loader=lambda _: snapshot,
        clock=Clock(),
        stdin_isatty=lambda: True,
        stdout_isatty=lambda: True,
        env={},
        executable_validator=_valid_path,
        launcher_marker_validator=_valid_path,
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


def test_cli_run_requires_exact_deployment_before_proof(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    module = _cli_module()
    touched = False

    def tripwire(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal touched
        touched = True
        raise AssertionError

    monkeypatch.setattr(module, "_deployment_attestation", lambda: None)
    monkeypatch.setattr(module, "run_proof", tripwire)
    args = _cli_args(tmp_path / "proof.json")
    args[0] = "run"

    assert module.main(args) == 2
    assert touched is False
    report = json.loads(capsys.readouterr().out)
    assert report["prerequisites"] == ["exact_head_deployment_required"]


@pytest.mark.parametrize("dirty_status", (" M tracked.py\n", "?? untracked.txt\n"))
def test_deployment_attestation_rejects_tracked_or_untracked_changes(
    tmp_path: Path, dirty_status: str
) -> None:
    module = _cli_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    account_home = tmp_path / "home"
    provenance = (
        account_home
        / "Library"
        / "Application Support"
        / "reverso"
        / "deployment-provenance.json"
    )
    provenance.parent.mkdir(parents=True)
    commit = "a" * 40
    provenance.write_text(
        json.dumps({"canonical_checkout": str(repo_root), "commit": commit}),
        encoding="utf-8",
    )

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        stdout = commit + "\n" if "rev-parse" in argv else dirty_status
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    assert (
        module._deployment_attestation(
            repo_root=repo_root,
            account_home=account_home,
            runner=runner,
        )
        is None
    )


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


@pytest.mark.parametrize("case", ("relative", "symlink", "directory"))
def test_cli_rejects_each_unsafe_evidence_target_without_mutation(
    case: str, tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    module = _cli_module()
    touched = False
    referent = tmp_path / "referent.json"
    if case == "relative":
        target = Path("relative-proof.json")
    elif case == "symlink":
        referent.write_text("sentinel", encoding="utf-8")
        target = tmp_path / "proof.json"
        target.symlink_to(referent)
    else:
        target = tmp_path / "proof.json"
        target.mkdir()

    def tripwire(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal touched
        touched = True
        raise AssertionError

    monkeypatch.setattr(module, "run_proof", tripwire)

    assert module.main(_cli_args(target)) == 64
    assert touched is False
    assert json.loads(capsys.readouterr().out)["prerequisites"] == [
        "invalid_evidence_path"
    ]
    if case == "symlink":
        assert target.is_symlink()
        assert referent.read_text(encoding="utf-8") == "sentinel"
    elif case == "directory":
        assert target.is_dir()
    else:
        assert not target.exists()


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
