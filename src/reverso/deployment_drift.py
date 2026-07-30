"""Fail-closed deployment provenance and drift validation.

The production command intentionally has one governed checkout. Tests inject
paths and fact readers through :class:`DriftEnvironment`; production callers do
not get a path override that could weaken the canonical-checkout gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reverso.protocols.headroom_compression import HeadroomCompressionConfig

CANONICAL_CHECKOUT = Path("/Users/andresilvaburgstahler/.local/share/reverso")
DEPLOYMENT_REPOSITORY = "git@github.com:r3dlex/reverso-llm.git"
INSTALLER_IDENTITY = str(CANONICAL_CHECKOUT / "scripts/install-launchagents.sh")
PROVENANCE_SCHEMA_VERSION = 2
PROVENANCE_RELATIVE_PATH = (
    Path("Library") / "Application Support" / "reverso" / "deployment-provenance.json"
)
LAUNCH_AGENT_EXECUTABLES = {
    "com.user.reverso-proxy": "reverso-proxy",
    "com.user.reverso-daemon": "reverso-daemon",
}
LAUNCH_AGENT_LABELS = tuple(LAUNCH_AGENT_EXECUTABLES)
SCHEDULED_LAUNCH_AGENT_LABEL = "com.user.reverso-catalog-refresh"
SCHEDULED_LAUNCH_AGENT_EXECUTABLE = "reverso-catalog-refresh"
SCHEDULED_START_CALENDAR_INTERVAL = [
    {"Hour": 6, "Minute": 0},
    {"Hour": 18, "Minute": 0},
]
KIMI_MODEL = "kimi-k3"
KIMI_CONTEXT_WINDOW = 1048576
KIMI_AUTO_COMPACT_TOKEN_LIMIT = 943718
KIMI_MODELS_URL = "http://127.0.0.1:64946/kimi/v1/models"
HEADROOM_USAGE_URL = "http://127.0.0.1:64946/usage/headroom"
HEADROOM_USAGE_FIELDS = {
    "schema_version",
    "enabled",
    "profile",
    "requests_seen",
    "requests_compressed",
    "tokens_before",
    "tokens_after",
    "tokens_saved",
    "compression_ratio",
    "fail_open_count",
    "failure_reasons",
    "error_types",
    "updated_at",
    "process_started_at",
    "measurement_started_at",
    "requests_passed_through",
    "compression_success_rate",
    "average_tokens_saved",
    "outcome_counts",
    "provider_counts",
    "surface_counts",
    "timeout_seconds",
    "model_limit",
    "last_success_at",
    "last_failure_at",
    "reset_reason",
}
HEADROOM_USAGE_MAP_FIELDS = {
    "failure_reasons": {
        "worker_busy",
        "timeout",
        "exception",
        "inflation_guard",
        "retrieval_marker",
        "unsafe_output",
        "other",
    },
    "error_types": {
        "timeout",
        "worker_busy",
        "dependency_exception",
        "inflation_guard",
        "retrieval_marker",
        "unsafe_output",
        "other",
    },
    "outcome_counts": {"compressed", "passed_through", "fail_open", "other"},
    "provider_counts": {
        "claude",
        "copilot",
        "auggie",
        "deepseek",
        "kimi",
        "codex-direct",
        "openai-pass-through",
        "other",
    },
    "surface_counts": {"responses", "anthropic_messages", "other"},
}
HEADROOM_SENSITIVE_FIELDS = {
    "request_body",
    "prompt",
    "response",
    "tool_content",
    "workspace",
    "session_id",
    "request_id",
    "raw_model",
    "raw_error",
}
HEADROOM_COUNTER_FIELDS = {
    "requests_seen",
    "requests_compressed",
    "tokens_before",
    "tokens_after",
    "tokens_saved",
    "fail_open_count",
    "requests_passed_through",
}
HEADROOM_RATIO_FIELDS = {
    "compression_ratio",
    "compression_success_rate",
}
HEADROOM_TIMESTAMP_FIELDS = {
    "updated_at",
    "process_started_at",
    "measurement_started_at",
    "last_success_at",
    "last_failure_at",
}
PHASES = ("pre-install", "pre-restart", "post-restart", "pre-sync", "acceptance")

CommandRunner = Callable[[tuple[str, ...], Path | None], str]
JsonFetcher = Callable[[str], Any]


class DeploymentDriftError(RuntimeError):
    """A governed deployment authority disagrees with the selected revision."""


def _run_command(command: tuple[str, ...], cwd: Path | None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeploymentDriftError(
            f"unable to read deployment fact from {command[0]}"
        ) from exc
    return result.stdout


def _fetch_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=5.0) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        raise DeploymentDriftError("live Kimi discovery is unavailable") from exc


@dataclass(frozen=True)
class DriftEnvironment:
    """Filesystem and process seams used by the deployment checker."""

    repo_root: Path
    home: Path
    canonical_checkout: Path = CANONICAL_CHECKOUT
    command_runner: CommandRunner = _run_command
    json_fetcher: JsonFetcher = _fetch_json
    uid: int = -1
    launcher: Path | None = None

    def __post_init__(self) -> None:
        if self.uid < 0:
            object.__setattr__(self, "uid", os.getuid())
        launcher = self.launcher
        if launcher is None:
            selected = os.environ.get("REVERSO_UV_BIN") or shutil.which("uv")
            if selected is None:
                raise DeploymentDriftError("unable to select the uv launcher")
            launcher = Path(selected)
        if not launcher.is_absolute():
            raise DeploymentDriftError("selected uv launcher must be an absolute path")
        object.__setattr__(self, "launcher", launcher)

    @property
    def provenance_path(self) -> Path:
        return self.home / PROVENANCE_RELATIVE_PATH

    @property
    def launch_agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def kimi_profile_path(self) -> Path:
        return self.home / ".codex" / "reverso-kimi.config.toml"

    @property
    def kimi_code_home(self) -> Path:
        return self.home / "Library" / "Application Support" / "reverso" / "kimi-code"


def _git(env: DriftEnvironment, *args: str) -> str:
    return env.command_runner(("git", *args), env.repo_root).strip()


def _validate_source(env: DriftEnvironment, selected_commit: str) -> None:
    if env.repo_root.resolve() != env.canonical_checkout.resolve():
        raise DeploymentDriftError(
            f"deployment must run from canonical checkout {env.canonical_checkout}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", selected_commit):
        raise DeploymentDriftError("selected deployment commit must be a full Git SHA")
    if _git(env, "status", "--porcelain=v1", "--untracked-files=all"):
        raise DeploymentDriftError("canonical checkout must have a clean Git tree")
    if _git(env, "rev-parse", "HEAD") != selected_commit:
        raise DeploymentDriftError(
            "canonical checkout HEAD differs from selected deployment commit"
        )
    if _git(env, "remote", "get-url", "origin") != DEPLOYMENT_REPOSITORY:
        raise DeploymentDriftError("canonical checkout repository identity is invalid")


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentDriftError(f"{label} is missing or malformed") from exc
    if not isinstance(payload, dict):
        raise DeploymentDriftError(f"{label} must be a JSON object")
    return payload


def _expected_provenance(
    env: DriftEnvironment,
    selected_commit: str,
) -> dict[str, object]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": selected_commit,
        "installer": INSTALLER_IDENTITY,
        "launcher": str(env.launcher),
        "kimi_code_home": str(env.kimi_code_home),
    }


def _validate_installed_at(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DeploymentDriftError(
            "deployment provenance installed_at_utc must be a UTC timestamp"
        )
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeploymentDriftError(
            "deployment provenance installed_at_utc is malformed"
        ) from exc
    if parsed.tzinfo != dt.UTC:
        raise DeploymentDriftError("deployment provenance installed_at_utc must be UTC")


def _validate_provenance(
    env: DriftEnvironment,
    selected_commit: str,
    *,
    allow_absent: bool,
    allow_predecessor: bool = False,
) -> str:
    path = env.provenance_path
    if not path.exists():
        if allow_absent:
            return "bootstrap-required"
        raise DeploymentDriftError("deployment provenance is required for this phase")

    record = _load_json_object(path, "deployment provenance")
    if record.get("schema_version") == 1 and allow_predecessor:
        return _validate_schema_one_predecessor(env, record, selected_commit)
    if record.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise DeploymentDriftError("deployment provenance schema is unsupported")
    required = {
        "schema_version",
        "repository",
        "canonical_checkout",
        "commit",
        "installer",
        "launcher",
        "kimi_code_home",
        "installed_at_utc",
    }
    if set(record) != required:
        raise DeploymentDriftError(
            "deployment provenance has missing or unsupported fields"
        )
    expected = _expected_provenance(env, selected_commit)
    for field, expected_value in expected.items():
        if field in {"commit", "launcher"}:
            continue
        if record.get(field) != expected_value:
            raise DeploymentDriftError(
                f"deployment provenance {field} does not match governed source"
            )
    recorded_commit = record.get("commit")
    if not isinstance(recorded_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", recorded_commit
    ):
        raise DeploymentDriftError(
            "deployment provenance commit must be a full Git SHA"
        )
    recorded_launcher = record.get("launcher")
    if (
        not isinstance(recorded_launcher, str)
        or not Path(recorded_launcher).is_absolute()
    ):
        raise DeploymentDriftError(
            "deployment provenance launcher must be an absolute path"
        )
    _validate_installed_at(record["installed_at_utc"])
    if recorded_commit != selected_commit:
        if not allow_predecessor:
            raise DeploymentDriftError(
                "deployment provenance commit does not match governed source"
            )
        _validate_launch_agents(env, recorded_commit, recorded_launcher)
        _validate_running_agents(env, recorded_commit, recorded_launcher)
        try:
            env.command_runner(
                (
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    recorded_commit,
                    selected_commit,
                ),
                env.repo_root,
            )
        except DeploymentDriftError as exc:
            raise DeploymentDriftError(
                "deployment predecessor is not a known ancestor of selected commit"
            ) from exc
        return "valid-predecessor"
    if recorded_launcher != str(env.launcher):
        raise DeploymentDriftError(
            "deployment provenance launcher does not match governed source"
        )
    return "valid"


def _validate_schema_one_predecessor(
    env: DriftEnvironment,
    record: dict[str, Any],
    selected_commit: str,
) -> str:
    """Authorize only a fully converged S4 deployment for one S4A upgrade."""
    required = {
        "schema_version",
        "repository",
        "canonical_checkout",
        "commit",
        "installer",
        "launcher",
        "installed_at_utc",
    }
    if set(record) != required:
        raise DeploymentDriftError(
            "legacy deployment provenance has missing or unsupported fields"
        )
    expected = _expected_provenance(env, selected_commit)
    for field in ("repository", "canonical_checkout", "installer"):
        if record.get(field) != expected[field]:
            raise DeploymentDriftError(
                f"legacy deployment provenance {field} does not match governed source"
            )
    recorded_commit = record.get("commit")
    if not isinstance(recorded_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", recorded_commit
    ):
        raise DeploymentDriftError(
            "legacy deployment provenance commit must be a full Git SHA"
        )
    recorded_launcher = record.get("launcher")
    if (
        not isinstance(recorded_launcher, str)
        or not Path(recorded_launcher).is_absolute()
    ):
        raise DeploymentDriftError(
            "legacy deployment provenance launcher must be an absolute path"
        )
    _validate_installed_at(record["installed_at_utc"])
    _validate_launch_agents(
        env,
        recorded_commit,
        recorded_launcher,
        require_kimi_home=False,
    )
    _validate_running_agents(
        env,
        recorded_commit,
        recorded_launcher,
        require_kimi_home=False,
    )
    try:
        env.command_runner(
            (
                "git",
                "merge-base",
                "--is-ancestor",
                recorded_commit,
                selected_commit,
            ),
            env.repo_root,
        )
    except DeploymentDriftError as exc:
        raise DeploymentDriftError(
            "legacy deployment predecessor is not a known ancestor of selected commit"
        ) from exc
    return "valid-schema-one-predecessor"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_deployment_provenance(
    env: DriftEnvironment,
    *,
    selected_commit: str,
    installed_at_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    """Atomically write and fully read back the governed deployment record."""
    _validate_source(env, selected_commit)
    installed_at = installed_at_utc or dt.datetime.now(dt.UTC)
    if installed_at.tzinfo is None or installed_at.utcoffset() != dt.timedelta(0):
        raise DeploymentDriftError("installed_at_utc must be timezone-aware UTC")
    _validate_kimi_code_home(env)
    record: dict[str, object] = {
        **_expected_provenance(env, selected_commit),
        "installed_at_utc": installed_at.astimezone(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    _atomic_write_json(env.provenance_path, record)
    _validate_provenance(env, selected_commit, allow_absent=False)
    return _load_json_object(env.provenance_path, "deployment provenance")


def _read_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {path.name} is missing or malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise DeploymentDriftError(
            f"rendered LaunchAgent {path.name} must be a plist dictionary"
        )
    return payload


def _validate_program_arguments(
    label: str,
    executable: str,
    arguments: object,
    canonical: str,
    expected_launcher: str,
    *,
    authority: str,
) -> None:
    expected_arguments = [
        expected_launcher,
        "run",
        "--project",
        canonical,
        executable,
    ]
    if not isinstance(arguments, list) or arguments != expected_arguments:
        raise DeploymentDriftError(
            f"{authority} LaunchAgent {label} has unauthorized ProgramArguments"
        )


def _validate_launch_agents(
    env: DriftEnvironment,
    selected_commit: str,
    expected_launcher: str,
    *,
    require_kimi_home: bool = True,
) -> None:
    canonical = str(env.canonical_checkout)
    for label in LAUNCH_AGENT_LABELS:
        payload = _read_plist(env.launch_agents_dir / f"{label}.plist")
        if payload.get("Label") != label:
            raise DeploymentDriftError(f"rendered LaunchAgent {label} has wrong label")
        if payload.get("WorkingDirectory") != canonical:
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} has stale WorkingDirectory"
            )
        if payload.get("Program") != expected_launcher:
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} has unauthorized Program"
            )
        _validate_program_arguments(
            label,
            LAUNCH_AGENT_EXECUTABLES[label],
            payload.get("ProgramArguments"),
            canonical,
            expected_launcher,
            authority="rendered",
        )
        environment = payload.get("EnvironmentVariables")
        if not isinstance(environment, dict):
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} has no deployment environment"
            )
        if environment.get("REVERSO_PROJECT_DIR") != canonical:
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} project provenance is stale"
            )
        if environment.get("REVERSO_DEPLOYMENT_COMMIT") != selected_commit:
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} revision provenance is stale"
            )
        rendered_kimi_home = environment.get("KIMI_CODE_HOME")
        if label == "com.user.reverso-proxy":
            if require_kimi_home and rendered_kimi_home != str(env.kimi_code_home):
                raise DeploymentDriftError(
                    "rendered LaunchAgent com.user.reverso-proxy "
                    "KIMI_CODE_HOME is stale"
                )
            if not require_kimi_home and rendered_kimi_home is not None:
                raise DeploymentDriftError(
                    "legacy rendered LaunchAgent com.user.reverso-proxy "
                    "must not set KIMI_CODE_HOME"
                )
        elif rendered_kimi_home is not None:
            raise DeploymentDriftError(
                "rendered LaunchAgent com.user.reverso-daemon "
                "must not set KIMI_CODE_HOME"
            )


def _validate_scheduled_launch_agent(
    env: DriftEnvironment,
    selected_commit: str,
    expected_launcher: str,
) -> None:
    label = SCHEDULED_LAUNCH_AGENT_LABEL
    canonical = str(env.canonical_checkout)
    payload = _read_plist(env.launch_agents_dir / f"{label}.plist")
    if payload.get("Label") != label:
        raise DeploymentDriftError(f"rendered LaunchAgent {label} has wrong label")
    if payload.get("WorkingDirectory") != canonical:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} has stale WorkingDirectory"
        )
    if payload.get("Program") != expected_launcher:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} has unauthorized Program"
        )
    _validate_program_arguments(
        label,
        SCHEDULED_LAUNCH_AGENT_EXECUTABLE,
        payload.get("ProgramArguments"),
        canonical,
        expected_launcher,
        authority="rendered",
    )
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} has no deployment environment"
        )
    if environment.get("REVERSO_PROJECT_DIR") != canonical:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} project provenance is stale"
        )
    if environment.get("REVERSO_DEPLOYMENT_COMMIT") != selected_commit:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} revision provenance is stale"
        )
    if payload.get("StartCalendarInterval") != SCHEDULED_START_CALENDAR_INTERVAL:
        raise DeploymentDriftError(
            f"rendered LaunchAgent {label} has unauthorized schedule"
        )
    for key in (
        "KeepAlive",
        "RunAtLoad",
        "Sockets",
        "MachServices",
        "StandardOutPath",
        "StandardErrorPath",
    ):
        if key in payload:
            raise DeploymentDriftError(
                f"rendered LaunchAgent {label} must not set {key}"
            )


def _launchctl_value(output: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=>\s*(.*?)\s*$", re.MULTILINE)
    match = pattern.search(output)
    if match is None:
        return None
    return match.group(1).strip("'\"")


def _launchctl_assignment(output: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", re.MULTILINE)
    match = pattern.search(output)
    if match is None:
        return None
    return match.group(1).strip("'\"")


def _launchctl_block(output: str, key: str) -> list[str] | None:
    lines = output.splitlines()
    start_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*\{{\s*$")
    for index, line in enumerate(lines):
        if not start_pattern.match(line):
            continue
        values: list[str] = []
        for value_line in lines[index + 1 :]:
            value = value_line.strip()
            if value == "}":
                return values
            if value:
                values.append(value.strip("'\""))
        return None
    return None


def _validate_running_agents(
    env: DriftEnvironment,
    selected_commit: str,
    expected_launcher: str,
    *,
    require_kimi_home: bool = True,
) -> None:
    canonical = str(env.canonical_checkout)
    for label in LAUNCH_AGENT_LABELS:
        output = env.command_runner(
            ("launchctl", "print", f"gui/{env.uid}/{label}"),
            None,
        )
        environment = _launchctl_block(output, "environment")
        if environment is None:
            raise DeploymentDriftError(
                f"running LaunchAgent {label} has no deployment environment"
            )
        environment_output = "\n".join(environment)
        if (
            _launchctl_value(environment_output, "REVERSO_DEPLOYMENT_COMMIT")
            != selected_commit
        ):
            raise DeploymentDriftError(
                f"running revision for LaunchAgent {label} is stale"
            )
        if _launchctl_value(environment_output, "REVERSO_PROJECT_DIR") != canonical:
            raise DeploymentDriftError(
                f"running checkout for LaunchAgent {label} is stale"
            )
        running_kimi_home = _launchctl_value(environment_output, "KIMI_CODE_HOME")
        if label == "com.user.reverso-proxy":
            if require_kimi_home and running_kimi_home != str(env.kimi_code_home):
                raise DeploymentDriftError(
                    "running KIMI_CODE_HOME for LaunchAgent "
                    "com.user.reverso-proxy is stale"
                )
            if not require_kimi_home and running_kimi_home is not None:
                raise DeploymentDriftError(
                    "legacy running LaunchAgent com.user.reverso-proxy "
                    "must not set KIMI_CODE_HOME"
                )
        elif running_kimi_home is not None:
            raise DeploymentDriftError(
                "running LaunchAgent com.user.reverso-daemon "
                "must not set KIMI_CODE_HOME"
            )
        if _launchctl_assignment(output, "working directory") != canonical:
            raise DeploymentDriftError(
                f"running WorkingDirectory for LaunchAgent {label} is stale"
            )
        if _launchctl_assignment(output, "program") != expected_launcher:
            raise DeploymentDriftError(
                f"running LaunchAgent {label} has unauthorized program"
            )
        running_arguments = _launchctl_block(output, "arguments")
        _validate_program_arguments(
            label,
            LAUNCH_AGENT_EXECUTABLES[label],
            running_arguments,
            canonical,
            expected_launcher,
            authority="running",
        )
        rendered_arguments = _read_plist(env.launch_agents_dir / f"{label}.plist").get(
            "ProgramArguments"
        )
        if running_arguments != rendered_arguments:
            raise DeploymentDriftError(
                f"running LaunchAgent {label} does not match rendered ProgramArguments"
            )


def _validate_kimi_code_home(env: DriftEnvironment) -> None:
    path = env.kimi_code_home
    try:
        resolved_path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise DeploymentDriftError(
            "governed KIMI_CODE_HOME path cannot be resolved safely"
        ) from exc
    if not path.is_absolute() or resolved_path != path:
        raise DeploymentDriftError(
            "governed KIMI_CODE_HOME path must not contain symbolic links"
        )
    if not path.is_dir():
        raise DeploymentDriftError("governed KIMI_CODE_HOME must be a real directory")
    if path.stat().st_mode & 0o777 != 0o700:
        raise DeploymentDriftError("governed KIMI_CODE_HOME must have mode 0700")


def _validate_live_kimi(env: DriftEnvironment) -> None:
    payload = env.json_fetcher(KIMI_MODELS_URL)
    if not isinstance(payload, dict):
        raise DeploymentDriftError("live Kimi discovery payload is malformed")
    if payload.get("model_discovery_source") != "live":
        raise DeploymentDriftError("live Kimi discovery source must be live")
    data = payload.get("data")
    if not isinstance(data, list):
        raise DeploymentDriftError("live Kimi discovery has malformed model data")
    if (
        len(data) != 1
        or not isinstance(data[0], dict)
        or data[0].get("id") != KIMI_MODEL
    ):
        raise DeploymentDriftError(
            "live Kimi discovery must contain exactly one kimi-k3 entry"
        )


def validate_headroom_usage_payload(
    payload: Any,
    *,
    expected_profile: str | None = None,
) -> None:
    """Validate the prompt-free live Headroom acceptance contract."""
    configured_profile = (
        HeadroomCompressionConfig.from_env().profile
        if expected_profile is None
        else HeadroomCompressionConfig.from_env(
            {"REVERSO_HEADROOM_PROFILE": expected_profile}
        ).profile
    )
    if not isinstance(payload, dict):
        raise DeploymentDriftError("live Headroom usage payload is malformed")
    if payload.get("schema_version") != 1:
        raise DeploymentDriftError("live Headroom outer schema must be version 1")
    if payload.get("provider") != "headroom":
        raise DeploymentDriftError("live Headroom provider must be headroom")
    if set(payload) != {"schema_version", "provider", "headroom"}:
        raise DeploymentDriftError("live Headroom outer payload has unsupported fields")

    usage = payload.get("headroom")
    if not isinstance(usage, dict):
        raise DeploymentDriftError("live Headroom inner usage payload is malformed")
    if set(usage).intersection(HEADROOM_SENSITIVE_FIELDS):
        raise DeploymentDriftError("live Headroom usage contains a sensitive field")
    if set(usage) != HEADROOM_USAGE_FIELDS:
        raise DeploymentDriftError("live Headroom usage must contain exact fields")
    if usage.get("schema_version") != 2:
        raise DeploymentDriftError("live Headroom inner schema must be version 2")
    if not isinstance(usage.get("enabled"), bool):
        raise DeploymentDriftError("live Headroom enabled must be boolean")
    if usage.get("profile") != configured_profile:
        raise DeploymentDriftError(
            f"live Headroom profile must be {configured_profile}"
        )
    for field in HEADROOM_COUNTER_FIELDS:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DeploymentDriftError(
                f"live Headroom {field} must be a nonnegative integer"
            )
    model_limit = usage.get("model_limit")
    if (
        isinstance(model_limit, bool)
        or not isinstance(model_limit, int)
        or model_limit < 1
    ):
        raise DeploymentDriftError(
            "live Headroom model_limit must be a positive integer"
        )
    timeout_seconds = usage.get("timeout_seconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise DeploymentDriftError(
            "live Headroom timeout_seconds must be a positive finite number"
        )
    for field in HEADROOM_RATIO_FIELDS:
        value = usage.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise DeploymentDriftError(f"live Headroom {field} must be a finite ratio")
    average_tokens_saved = usage.get("average_tokens_saved")
    if (
        isinstance(average_tokens_saved, bool)
        or not isinstance(average_tokens_saved, (int, float))
        or not math.isfinite(average_tokens_saved)
        or average_tokens_saved < 0
    ):
        raise DeploymentDriftError(
            "live Headroom average_tokens_saved must be a finite nonnegative number"
        )
    for field in HEADROOM_TIMESTAMP_FIELDS:
        value = usage.get(field)
        if value is None:
            continue
        try:
            parsed = dt.datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise DeploymentDriftError(
                f"live Headroom {field} must be RFC3339 UTC or null"
            ) from exc
        if (
            not isinstance(value, str)
            or "T" not in value
            or parsed.utcoffset() != dt.timedelta(0)
            or not value.endswith(("Z", "+00:00"))
        ):
            raise DeploymentDriftError(
                f"live Headroom {field} must be RFC3339 UTC or null"
            )
    if usage.get("reset_reason") not in {"process_start", "manual_test_reset"}:
        raise DeploymentDriftError(
            "live Headroom reset_reason must use a governed value"
        )
    for field, expected_keys in HEADROOM_USAGE_MAP_FIELDS.items():
        counts = usage.get(field)
        if (
            not isinstance(counts, dict)
            or set(counts) != expected_keys
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts.values()
            )
        ):
            raise DeploymentDriftError(
                f"live Headroom {field} must use bounded nonnegative counters"
            )

    requests_seen = usage["requests_seen"]
    requests_compressed = usage["requests_compressed"]
    fail_open_count = usage["fail_open_count"]
    tokens_before = usage["tokens_before"]
    tokens_saved = usage["tokens_saved"]
    formulas = {
        "compression_ratio": tokens_saved / tokens_before if tokens_before else 0.0,
        "compression_success_rate": (
            requests_compressed / requests_seen if requests_seen else 0.0
        ),
        "average_tokens_saved": (
            tokens_saved / requests_compressed if requests_compressed else 0.0
        ),
        "requests_passed_through": max(
            requests_seen - requests_compressed - fail_open_count,
            0,
        ),
    }
    for field, expected in formulas.items():
        actual = usage[field]
        matches = (
            actual == expected
            if isinstance(expected, int)
            else math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
        )
        if not matches:
            raise DeploymentDriftError(
                f"live Headroom {field} does not match its governed formula"
            )


def _validate_live_headroom(env: DriftEnvironment) -> None:
    validate_headroom_usage_payload(env.json_fetcher(HEADROOM_USAGE_URL))


def _validate_generated_kimi(env: DriftEnvironment) -> None:
    try:
        profile = tomllib.loads(env.kimi_profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DeploymentDriftError(
            "generated Kimi profile is missing or malformed"
        ) from exc
    if profile.get("model") != KIMI_MODEL:
        raise DeploymentDriftError("generated Kimi profile model must be kimi-k3")
    if profile.get("model_provider") != "reverso_kimi":
        raise DeploymentDriftError(
            "generated Kimi profile provider must be reverso_kimi"
        )
    if profile.get("model_context_window") != KIMI_CONTEXT_WINDOW:
        raise DeploymentDriftError(
            "generated Kimi profile context window must be 1048576"
        )
    if profile.get("model_auto_compact_token_limit") != KIMI_AUTO_COMPACT_TOKEN_LIMIT:
        raise DeploymentDriftError(
            "generated Kimi profile auto compact token limit must be 943718"
        )
    catalog_value = profile.get("model_catalog_json")
    if not isinstance(catalog_value, str):
        raise DeploymentDriftError(
            "generated Kimi profile must reference its model catalog"
        )
    expected_catalog = env.home / ".codex" / "reverso" / "kimi.json"
    catalog_path = Path(catalog_value).expanduser()
    if catalog_path.resolve() != expected_catalog.resolve():
        raise DeploymentDriftError(
            "generated Kimi profile catalog path is not canonical"
        )
    catalog = _load_json_object(catalog_path, "generated Kimi catalog")
    models = catalog.get("models")
    if not isinstance(models, list) or len(models) != 1:
        raise DeploymentDriftError(
            "generated Kimi catalog must contain exactly one model"
        )
    model = models[0]
    if not isinstance(model, dict) or model.get("slug") != KIMI_MODEL:
        raise DeploymentDriftError("generated Kimi catalog slug must be kimi-k3")
    if (
        model.get("context_window") != KIMI_CONTEXT_WINDOW
        or model.get("max_context_window") != KIMI_CONTEXT_WINDOW
    ):
        raise DeploymentDriftError("generated Kimi catalog context must be 1048576")


def check_deployment_drift(
    phase: str,
    env: DriftEnvironment,
    *,
    selected_commit: str,
) -> dict[str, str]:
    """Validate the authorities applicable before one deployment action."""
    if phase not in PHASES:
        raise DeploymentDriftError(f"unsupported deployment drift phase: {phase}")
    _validate_source(env, selected_commit)
    provenance = _validate_provenance(
        env,
        selected_commit,
        allow_absent=phase == "pre-install",
        allow_predecessor=phase == "pre-install",
    )
    if phase != "pre-install":
        _validate_kimi_code_home(env)
        _validate_launch_agents(env, selected_commit, str(env.launcher))
        _validate_scheduled_launch_agent(env, selected_commit, str(env.launcher))
    if phase in {"post-restart", "pre-sync", "acceptance"}:
        _validate_running_agents(env, selected_commit, str(env.launcher))
    if phase in {"pre-sync", "acceptance"}:
        _validate_live_kimi(env)
    if phase == "acceptance":
        _validate_generated_kimi(env)
        _validate_live_headroom(env)
    return {"phase": phase, "status": "passed", "provenance": provenance}


def _selected_commit(env: DriftEnvironment) -> str:
    selected = os.environ.get("REVERSO_DEPLOYMENT_COMMIT")
    return selected if selected is not None else _git(env, "rev-parse", "HEAD")


def _production_home() -> Path:
    """Return the immutable account home used by the production command."""
    try:
        account_home_value = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError) as exc:
        raise DeploymentDriftError(
            "unable to resolve the governed account home"
        ) from exc
    if not isinstance(account_home_value, str) or not account_home_value:
        raise DeploymentDriftError("governed account home is missing")

    account_home = Path(account_home_value)
    configured_home = os.environ.get("HOME")
    if configured_home is None:
        raise DeploymentDriftError("HOME must match the governed account home")
    if Path(configured_home) != account_home:
        raise DeploymentDriftError("HOME must match the governed account home")

    try:
        first_resolution = account_home.resolve(strict=True)
        second_resolution = account_home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeploymentDriftError(
            "governed account home cannot be resolved safely"
        ) from exc
    if (
        not account_home.is_absolute()
        or account_home.is_symlink()
        or not account_home.is_dir()
        or first_resolution != account_home
        or second_resolution != first_resolution
    ):
        raise DeploymentDriftError(
            "governed account home must be absolute, real, and free of symbolic links"
        )
    return account_home


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed on Reverso deployment authority drift"
    )
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument(
        "--write-provenance",
        action="store_true",
        help="atomically write and read back provenance after pre-install",
    )
    args = parser.parse_args(argv)
    if args.write_provenance and args.phase != "pre-install":
        parser.error("--write-provenance is valid only with --phase pre-install")

    try:
        root = (repo_root or Path.cwd()).resolve()
        env = DriftEnvironment(repo_root=root, home=_production_home())
        selected_commit = _selected_commit(env)
        report = check_deployment_drift(
            args.phase,
            env,
            selected_commit=selected_commit,
        )
        if args.write_provenance:
            write_deployment_provenance(env, selected_commit=selected_commit)
            report["provenance"] = "written-and-validated"
    except DeploymentDriftError as exc:
        print(f"deployment-drift: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0
