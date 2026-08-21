#!/usr/bin/env python3
"""Run the bounded Ollama Reverso provider proof."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from reverso.ollama_convergence import default_inventory_path, load_inventory
from reverso.ollama_live_proof import INVALID_INPUT, ProofInputs, run_proof

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _invalid_report(code: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": None,
        "status": "invalid",
        "exit_code": INVALID_INPUT,
        "prerequisites": [code],
        "versions": {},
        "candidates": {"local": None, "cloud": None},
        "lanes": [],
        "duration_ms": 0,
    }


def _evidence_path_is_safe(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        parent_mode = path.parent.lstat().st_mode
        target_mode = path.lstat().st_mode
    except FileNotFoundError:
        try:
            return stat.S_ISDIR(path.parent.lstat().st_mode)
        except FileNotFoundError:
            return False
    return stat.S_ISDIR(parent_mode) and stat.S_ISREG(target_mode)


def _write_evidence(path: Path, encoded: str) -> None:
    """Atomically publish an already-bounded report in the target directory."""
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _deployment_attestation(
    *,
    repo_root: Path | None = None,
    account_home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str] | None:
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    try:
        completed = runner(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            shell=False,
            env={key: os.environ[key] for key in ("PATH",) if key in os.environ},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        status = runner(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            shell=False,
            env={key: os.environ[key] for key in ("PATH",) if key in os.environ},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        source_commit = completed.stdout.strip()
        account_home = account_home or Path(pwd.getpwuid(os.getuid()).pw_dir)
        provenance_path = (
            account_home
            / "Library"
            / "Application Support"
            / "reverso"
            / "deployment-provenance.json"
        )
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        deployed_commit = provenance.get("commit")
        canonical_checkout = Path(provenance.get("canonical_checkout", ""))
        if (
            completed.returncode != 0
            or status.returncode != 0
            or status.stdout != ""
            or _COMMIT_RE.fullmatch(source_commit) is None
            or not isinstance(deployed_commit, str)
            or _COMMIT_RE.fullmatch(deployed_commit) is None
            or source_commit != deployed_commit
            or canonical_checkout.resolve(strict=True) != repo_root.resolve(strict=True)
        ):
            return None
        return source_commit, deployed_commit
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
        TypeError,
    ):
        return None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        payload = _invalid_report("invalid_invocation")
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(INVALID_INPUT)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Run the Ollama Reverso provider live proof")
    parser.add_argument("mode", choices=("preflight", "run"))
    parser.add_argument("--inventory-path", type=Path, default=default_inventory_path())
    parser.add_argument("--ollama-executable", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path, required=True)
    parser.add_argument("--claude-launcher", type=Path, required=True)
    parser.add_argument("--client-sync-executable", type=Path, required=True)
    parser.add_argument("--attended", action="store_true")
    parser.add_argument("--authorize-signin", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.evidence is not None and not _evidence_path_is_safe(args.evidence):
        report = _invalid_report("invalid_evidence_path")
        print(json.dumps(report, sort_keys=True))
        return INVALID_INPUT
    attestation = _deployment_attestation() if args.mode == "run" else None
    if args.mode == "run" and attestation is None:
        report = _invalid_report("exact_head_deployment_required")
        report["mode"] = "run"
        report["status"] = "external_prerequisite"
        report["exit_code"] = 2
        print(json.dumps(report, sort_keys=True))
        return 2
    report = run_proof(
        ProofInputs(
            mode=args.mode,
            inventory_path=args.inventory_path,
            ollama_executable=args.ollama_executable,
            codex_executable=args.codex_executable,
            claude_launcher=args.claude_launcher,
            client_sync_executable=args.client_sync_executable,
            attended=args.attended,
            authorize_signin=args.authorize_signin,
        ),
        runner=subprocess.run,
        inventory_loader=load_inventory,
        clock=time.monotonic,
        stdin_isatty=sys.stdin.isatty,
        stdout_isatty=sys.stdout.isatty,
        env=os.environ,
        timeout=args.timeout,
    )
    if attestation is not None:
        report["source_commit"], report["deployed_commit"] = attestation
    encoded = json.dumps(report, sort_keys=True) + "\n"
    if args.evidence is not None:
        try:
            _write_evidence(args.evidence, encoded)
        except OSError:
            report = {**report}
            report["status"] = "failed"
            report["exit_code"] = 1
            report["prerequisites"] = ["evidence_write_failed"]
            encoded = json.dumps(report, sort_keys=True) + "\n"
    sys.stdout.write(encoded)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
