"""Pure, injected coordinator for the attended Ollama client proof."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reverso.ollama_convergence import InventorySnapshot

PASSED = 0
FAILED = 1
EXTERNAL_PREREQUISITE = 2
INVALID_INPUT = 64

_PROOF_INSTRUCTION = "Reply with only the literal text: ok"
_VERSION_LIMIT = 120
_SAFE_ENV_KEYS = frozenset(
    {
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
    }
)

Runner = Callable[..., subprocess.CompletedProcess[Any]]
InventoryLoader = Callable[[Path], InventorySnapshot]
Clock = Callable[[], float]
TtyProbe = Callable[[], bool]
PathValidator = Callable[[Path], bool]


@dataclass(frozen=True)
class ProofInputs:
    mode: str
    inventory_path: Path
    ollama_executable: Path
    codex_executable: Path
    claude_launcher: Path
    client_sync_executable: Path
    attended: bool = False
    authorize_signin: bool = False


@dataclass(frozen=True)
class CandidateSet:
    local: str | None
    cloud: str | None


@dataclass(frozen=True)
class Lane:
    model: str
    surface: str
    protocol: str
    argv: tuple[str, ...]


def _milliseconds(start: float, end: float) -> int:
    return max(0, round((end - start) * 1000))


def _report(
    *,
    mode: str,
    status: str,
    exit_code: int,
    started: float,
    clock: Clock,
    prerequisites: Sequence[str] = (),
    versions: Mapping[str, str] | None = None,
    candidates: CandidateSet | None = None,
    lanes: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    selected = candidates or CandidateSet(None, None)
    return {
        "schema_version": 1,
        "mode": mode,
        "status": status,
        "exit_code": exit_code,
        "prerequisites": sorted(set(prerequisites)),
        "versions": dict(sorted((versions or {}).items())),
        "candidates": {"local": selected.local, "cloud": selected.cloud},
        "lanes": list(lanes),
        "duration_ms": _milliseconds(started, clock()),
    }


def select_candidates(snapshot: InventorySnapshot) -> CandidateSet:
    """Select candidates only from current marker-owned inventory authority."""
    local = next((entry.raw_id for entry in snapshot.entries if entry.local), None)
    cloud = None
    if snapshot.cloud_status == "current":
        cloud = next(
            (
                entry.raw_id
                for entry in snapshot.entries
                if entry.cloud and not entry.stale
            ),
            None,
        )
    return CandidateSet(local, cloud)


def prerequisite_codes(
    snapshot: InventorySnapshot, candidates: CandidateSet
) -> tuple[str, ...]:
    codes: list[str] = []
    if candidates.local is None:
        codes.append("local_model_required")
    if candidates.cloud is None:
        codes.append(
            "cloud_auth_required"
            if snapshot.cloud_status == "auth_required"
            else "cloud_model_required"
        )
    return tuple(codes)


def build_lanes(inputs: ProofInputs, candidates: CandidateSet) -> tuple[Lane, ...]:
    """Build the fixed four-lane client contract from raw inventory ids."""
    if candidates.local is None or candidates.cloud is None:
        return ()

    def codex(model: str) -> tuple[str, ...]:
        return (
            str(inputs.codex_executable),
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "--profile",
            "reverso-ollama",
            "--model",
            model,
            _PROOF_INSTRUCTION,
        )

    def claude(model: str) -> tuple[str, ...]:
        return (
            str(inputs.claude_launcher),
            "--print",
            "--model",
            f"anthropic-ollama-{model}",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--",
            _PROOF_INSTRUCTION,
        )

    return (
        Lane(
            candidates.local,
            "codex_responses",
            "ollama_responses",
            codex(candidates.local),
        ),
        Lane(
            candidates.local,
            "claude_messages",
            "ollama_messages",
            claude(candidates.local),
        ),
        Lane(
            candidates.cloud,
            "codex_responses",
            "ollama_responses",
            codex(candidates.cloud),
        ),
        Lane(
            candidates.cloud,
            "claude_messages",
            "ollama_messages",
            claude(candidates.cloud),
        ),
    )


def _is_executable_file(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return path.is_absolute() and resolved.is_file() and os.access(resolved, os.X_OK)


def _has_managed_launcher_marker(path: Path) -> bool:
    try:
        first_lines = path.read_text(encoding="utf-8").splitlines()[:3]
    except (OSError, UnicodeError):
        return False
    return "# Managed by reverso-claude-code-sync." in first_lines


def _invalid_paths(
    inputs: ProofInputs,
    *,
    executable_validator: PathValidator,
    launcher_marker_validator: PathValidator,
) -> tuple[str, ...]:
    paths = {
        "ollama_executable": inputs.ollama_executable,
        "codex_executable": inputs.codex_executable,
        "claude_launcher": inputs.claude_launcher,
        "client_sync_executable": inputs.client_sync_executable,
    }
    invalid = [] if inputs.inventory_path.is_absolute() else ["inventory_path"]
    invalid.extend(
        name for name, path in paths.items() if not executable_validator(path)
    )
    if "claude_launcher" not in invalid and not launcher_marker_validator(
        inputs.claude_launcher
    ):
        invalid.append("claude_launcher")
    return tuple(invalid)


def _safe_env(env: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if key in _SAFE_ENV_KEYS}


def _version(
    name: str,
    executable: Path,
    *,
    runner: Runner,
    env: Mapping[str, str],
    timeout: float,
) -> str:
    completed = runner(
        [str(executable), "--version"],
        shell=False,
        env=_safe_env(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("version probe failed")
    value = (completed.stdout or "").strip().splitlines()
    if not value:
        raise OSError("version probe returned no version")
    version = value[0][:_VERSION_LIMIT]
    version_token = r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?"
    patterns = {
        "ollama": rf"^ollama version is {version_token}$",
        "codex": rf"^codex-cli {version_token}$",
        "claude": rf"^{version_token} \(Claude Code\)$",
    }
    if re.fullmatch(patterns[name], version) is None:
        raise OSError("unexpected tool identity")
    return version


def run_proof(
    inputs: ProofInputs,
    *,
    runner: Runner,
    inventory_loader: InventoryLoader,
    clock: Clock,
    stdin_isatty: TtyProbe,
    stdout_isatty: TtyProbe,
    env: Mapping[str, str],
    timeout: float = 180.0,
    executable_validator: PathValidator = _is_executable_file,
    launcher_marker_validator: PathValidator = _has_managed_launcher_marker,
) -> dict[str, object]:
    """Run preflight or the bounded four-lane proof using injected effects."""
    started = clock()
    if inputs.mode not in {"preflight", "run"} or timeout <= 0:
        return _report(
            mode=inputs.mode,
            status="invalid",
            exit_code=INVALID_INPUT,
            started=started,
            clock=clock,
            prerequisites=("invalid_input",),
        )
    invalid_paths = _invalid_paths(
        inputs,
        executable_validator=executable_validator,
        launcher_marker_validator=launcher_marker_validator,
    )
    if invalid_paths:
        return _report(
            mode=inputs.mode,
            status="invalid",
            exit_code=INVALID_INPUT,
            started=started,
            clock=clock,
            prerequisites=tuple(
                f"absolute_path_required:{name}" for name in invalid_paths
            ),
        )

    versions: dict[str, str] = {}
    try:
        for name, executable in (
            ("ollama", inputs.ollama_executable),
            ("codex", inputs.codex_executable),
            ("claude", inputs.claude_launcher),
        ):
            versions[name] = _version(
                name, executable, runner=runner, env=env, timeout=timeout
            )
        snapshot = inventory_loader(inputs.inventory_path)
    except (
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.TimeoutExpired,
    ):
        return _report(
            mode=inputs.mode,
            status="external_prerequisite",
            exit_code=EXTERNAL_PREREQUISITE,
            started=started,
            clock=clock,
            prerequisites=("proof_environment_required",),
            versions=versions,
        )

    candidates = select_candidates(snapshot)
    prerequisites = prerequisite_codes(snapshot, candidates)
    needs_auth = "cloud_auth_required" in prerequisites
    can_signin = (
        inputs.mode == "run"
        and needs_auth
        and inputs.attended
        and inputs.authorize_signin
        and stdin_isatty()
        and stdout_isatty()
    )
    if can_signin:
        try:
            signin = runner(
                [str(inputs.ollama_executable), "signin"],
                shell=False,
                env=_safe_env(env),
                timeout=timeout,
                check=False,
            )
            if signin.returncode != 0:
                raise OSError("sign-in failed")
            refresh = runner(
                [str(inputs.client_sync_executable), "refresh", "--json"],
                shell=False,
                env=_safe_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if refresh.returncode != 0:
                raise OSError("refresh failed")
            snapshot = inventory_loader(inputs.inventory_path)
            candidates = select_candidates(snapshot)
            prerequisites = prerequisite_codes(snapshot, candidates)
        except (OSError, subprocess.TimeoutExpired):
            return _report(
                mode=inputs.mode,
                status="failed",
                exit_code=FAILED,
                started=started,
                clock=clock,
                prerequisites=("signin_or_refresh_failed",),
                versions=versions,
                candidates=candidates,
            )

    if prerequisites:
        return _report(
            mode=inputs.mode,
            status="external_prerequisite",
            exit_code=EXTERNAL_PREREQUISITE,
            started=started,
            clock=clock,
            prerequisites=prerequisites,
            versions=versions,
            candidates=candidates,
        )
    if inputs.mode == "preflight":
        return _report(
            mode=inputs.mode,
            status="passed",
            exit_code=PASSED,
            started=started,
            clock=clock,
            versions=versions,
            candidates=candidates,
        )

    lane_reports: list[dict[str, object]] = []
    for lane in build_lanes(inputs, candidates):
        lane_started = clock()
        status = "passed"
        try:
            completed = runner(
                list(lane.argv),
                shell=False,
                env=_safe_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                status = "failed"
        except (OSError, subprocess.TimeoutExpired):
            status = "failed"
        lane_reports.append(
            {
                "model": lane.model,
                "surface": lane.surface,
                "protocol": lane.protocol,
                "status": status,
                "duration_ms": _milliseconds(lane_started, clock()),
            }
        )
        if status == "failed":
            return _report(
                mode=inputs.mode,
                status="failed",
                exit_code=FAILED,
                started=started,
                clock=clock,
                versions=versions,
                candidates=candidates,
                lanes=lane_reports,
            )
    return _report(
        mode=inputs.mode,
        status="passed",
        exit_code=PASSED,
        started=started,
        clock=clock,
        versions=versions,
        candidates=candidates,
        lanes=lane_reports,
    )
