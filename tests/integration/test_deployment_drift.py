from __future__ import annotations

import datetime as dt
import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reverso import deployment_drift
from reverso.deployment_drift import (
    CANONICAL_CHECKOUT,
    DEPLOYMENT_REPOSITORY,
    INSTALLER_IDENTITY,
    PROVENANCE_SCHEMA_VERSION,
    DeploymentDriftError,
    DriftEnvironment,
    check_deployment_drift,
    write_deployment_provenance,
)

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
NON_ANCESTOR_COMMIT = "c" * 40
UNKNOWN_COMMIT = "d" * 40
SELECTED_TREE = "e" * 40
SQUASH_TREE = "f" * 40
DIFFERENT_TREE = "1" * 40
RAW_TREE_OBJECT = "2" * 40
TAG_OBJECT = "3" * 40
LAUNCHER = "/opt/homebrew/bin/uv"
PREDECESSOR_LAUNCHER = "/usr/local/bin/uv"
UNAUTHORIZED_LAUNCHER = "/tmp/unauthorized-launcher"


class FakeRunner:
    def __init__(self) -> None:
        self.dirty = False
        self.head = COMMIT
        self.remote = DEPLOYMENT_REPOSITORY
        self.commit_trees = {
            COMMIT: SELECTED_TREE,
            OLD_COMMIT: SQUASH_TREE,
            NON_ANCESTOR_COMMIT: DIFFERENT_TREE,
            RAW_TREE_OBJECT: SQUASH_TREE,
        }
        self.commit_objects = {
            COMMIT: COMMIT,
            OLD_COMMIT: OLD_COMMIT,
            NON_ANCESTOR_COMMIT: NON_ANCESTOR_COMMIT,
            TAG_OBJECT: OLD_COMMIT,
        }
        self.ancestors = {(OLD_COMMIT, COMMIT), (TAG_OBJECT, COMMIT)}
        self.merge_base_failures: dict[tuple[str, str], int | OSError] = {}
        self.ancestry_trees = {COMMIT: [SELECTED_TREE]}
        self.running_environment_commit = COMMIT
        self.running_environment_checkout = str(CANONICAL_CHECKOUT)
        self.running_kimi_code_home: str | None = None
        self.running_daemon_kimi_code_home: str | None = None
        self.running_program = LAUNCHER
        self.running_argument_zero = LAUNCHER
        self.running_project = str(CANONICAL_CHECKOUT)
        self.running_working_directory = str(CANONICAL_CHECKOUT)
        self.running_executables = {
            "com.user.reverso-proxy": "reverso-proxy",
            "com.user.reverso-daemon": "reverso-daemon",
        }
        self.running_extra_arguments: dict[str, list[str]] = {}
        self.commands: list[tuple[str, ...]] = []
        self.scheduled_label = deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL
        self.scheduled_program = LAUNCHER
        self.scheduled_arguments: list[str] | None = None
        self.scheduled_project: str | None = None
        self.scheduled_working_directory: str | None = None
        self.scheduled_environment_commit = COMMIT
        self.scheduled_environment_checkout: str | None = None
        self.scheduled_properties = ""
        self.scheduled_top_level_key: str | None = None
        self.scheduled_intervals = [
            {"Hour": 6, "Minute": 0},
            {"Hour": 18, "Minute": 0},
        ]

    def __call__(self, command: tuple[str, ...], cwd: Path | None) -> str:
        self.commands.append(command)
        if command[:2] == ("git", "status"):
            return " M tracked.py\n" if self.dirty else ""
        if command[:2] == ("git", "rev-parse"):
            revision = command[2]
            if revision == "HEAD":
                return self.head + "\n"
            if revision.endswith("^{commit}"):
                commit = revision.removesuffix("^{commit}")
                if commit in self.commit_objects:
                    return self.commit_objects[commit] + "\n"
                cause = subprocess.CalledProcessError(128, command)
                raise DeploymentDriftError("unknown commit") from cause
            if revision.endswith("^{tree}"):
                commit = revision.removesuffix("^{tree}")
                try:
                    return self.commit_trees[commit] + "\n"
                except KeyError as exc:
                    raise DeploymentDriftError("unknown revision") from exc
            raise AssertionError(f"unexpected revision: {revision}")
        if command[:3] == ("git", "remote", "get-url"):
            return self.remote + "\n"
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            pair = command[3:]
            if pair in self.merge_base_failures:
                failure = self.merge_base_failures[pair]
                if isinstance(failure, OSError):
                    raise DeploymentDriftError(
                        "unable to determine ancestry"
                    ) from failure
                returncode = failure
            elif pair in self.ancestors:
                return ""
            else:
                returncode = 1
            cause = subprocess.CalledProcessError(returncode, command)
            raise DeploymentDriftError("unable to determine ancestry") from cause
        if command[:3] == ("git", "log", "--format=%T"):
            return "\n".join(self.ancestry_trees.get(command[3], ()))
        if command[:2] == ("launchctl", "print"):
            label = command[-1].rsplit("/", 1)[-1]
            if label == deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL:
                arguments = self.scheduled_arguments or [
                    LAUNCHER,
                    "run",
                    "--project",
                    self.scheduled_project or self.running_environment_checkout,
                    deployment_drift.SCHEDULED_LAUNCH_AGENT_EXECUTABLE,
                ]
                rendered_arguments = "\n".join(
                    f"        {value}" for value in arguments
                )
                rendered_intervals = "\n".join(
                    (
                        f"        trigger-{index} => {{\n"
                        "            keepalive = 0\n"
                        "            stream = com.apple.launchd.calendarinterval\n"
                        "            descriptor = {\n"
                        f'                "Hour" => {interval["Hour"]}\n'
                        f'                "Minute" => {interval["Minute"]}\n'
                        "            }\n"
                        "        }"
                    )
                    for index, interval in enumerate(self.scheduled_intervals)
                )
                top_level_key = (
                    f"    {self.scheduled_top_level_key} = true\n"
                    if self.scheduled_top_level_key is not None
                    else ""
                )
                return (
                    f"gui/501/{self.scheduled_label} = {{\n"
                    f"    program = {self.scheduled_program}\n"
                    "    arguments = {\n"
                    f"{rendered_arguments}\n"
                    "    }\n"
                    "    working directory = "
                    f"{self.scheduled_working_directory or self.running_working_directory}\n"
                    "    environment = {\n"
                    "        REVERSO_DEPLOYMENT_COMMIT => "
                    f"{self.scheduled_environment_commit}\n"
                    "        REVERSO_PROJECT_DIR => "
                    f"{self.scheduled_environment_checkout or self.running_environment_checkout}\n"
                    "    }\n"
                    f"{top_level_key}"
                    "    event triggers = {\n"
                    f"{rendered_intervals}\n"
                    "    }\n"
                    f"    properties = {self.scheduled_properties}\n"
                    "}\n"
                )
            arguments = [
                self.running_argument_zero,
                "run",
                "--project",
                self.running_project,
                self.running_executables[label],
                *self.running_extra_arguments.get(label, []),
            ]
            rendered_arguments = "\n".join(f"        {value}" for value in arguments)
            kimi_environment = (
                f"        KIMI_CODE_HOME => {self.running_kimi_code_home}\n"
                if label == "com.user.reverso-proxy"
                and self.running_kimi_code_home is not None
                else (
                    f"        KIMI_CODE_HOME => {self.running_daemon_kimi_code_home}\n"
                    if label == "com.user.reverso-daemon"
                    and self.running_daemon_kimi_code_home is not None
                    else ""
                )
            )
            return (
                f"gui/501/{label} = {{\n"
                f"    program = {self.running_program}\n"
                "    arguments = {\n"
                f"{rendered_arguments}\n"
                "    }\n"
                f"    working directory = {self.running_working_directory}\n"
                "    environment = {\n"
                "        REVERSO_DEPLOYMENT_COMMIT => "
                f"{self.running_environment_commit}\n"
                "        REVERSO_PROJECT_DIR => "
                f"{self.running_environment_checkout}\n"
                f"{kimi_environment}"
                "    }\n"
                "}\n"
            )
        raise AssertionError(f"unexpected command: {command!r}, cwd={cwd}")


def _write_plists(
    home: Path,
    *,
    checkout: Path,
    commit: str = COMMIT,
    launcher: str = LAUNCHER,
    include_kimi_home: bool = True,
) -> None:
    launchd_dir = home / "Library" / "LaunchAgents"
    launchd_dir.mkdir(parents=True, exist_ok=True)
    for label in ("com.user.reverso-proxy", "com.user.reverso-daemon"):
        executable = (
            "reverso-proxy" if label == "com.user.reverso-proxy" else "reverso-daemon"
        )
        payload = {
            "Label": label,
            "Program": launcher,
            "ProgramArguments": [
                launcher,
                "run",
                "--project",
                str(checkout),
                executable,
            ],
            "WorkingDirectory": str(checkout),
            "EnvironmentVariables": {
                "REVERSO_DEPLOYMENT_COMMIT": commit,
                "REVERSO_PROJECT_DIR": str(checkout),
            },
        }
        if label == "com.user.reverso-proxy" and include_kimi_home:
            payload["EnvironmentVariables"]["KIMI_CODE_HOME"] = str(
                home / "Library" / "Application Support" / "reverso" / "kimi-code"
            )
        with (launchd_dir / f"{label}.plist").open("wb") as handle:
            plistlib.dump(payload, handle)
    scheduled = {
        "Label": deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL,
        "Program": launcher,
        "ProgramArguments": [
            launcher,
            "run",
            "--project",
            str(checkout),
            deployment_drift.SCHEDULED_LAUNCH_AGENT_EXECUTABLE,
        ],
        "WorkingDirectory": str(checkout),
        "EnvironmentVariables": {
            "REVERSO_DEPLOYMENT_COMMIT": commit,
            "REVERSO_PROJECT_DIR": str(checkout),
            "PATH": (
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
                f"{home}/.local/bin"
            ),
        },
        "StartCalendarInterval": deployment_drift.SCHEDULED_START_CALENDAR_INTERVAL,
        "ProcessType": "Background",
    }
    with (launchd_dir / f"{deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL}.plist").open(
        "wb"
    ) as handle:
        plistlib.dump(scheduled, handle)


def _write_generated_kimi(home: Path) -> None:
    codex_dir = home / ".codex"
    catalog_dir = codex_dir / "reverso"
    catalog_dir.mkdir(parents=True)
    catalog = catalog_dir / "kimi.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "kimi-k3",
                        "context_window": 1048576,
                        "max_context_window": 1048576,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / "reverso-kimi.config.toml").write_text(
        "\n".join(
            (
                'model = "kimi-k3"',
                'model_provider = "reverso_kimi"',
                f'model_catalog_json = "{catalog}"',
                "model_context_window = 1048576",
                "model_auto_compact_token_limit = 943718",
                "",
            )
        ),
        encoding="utf-8",
    )


def _headroom_usage_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": "headroom",
        "headroom": {
            "schema_version": 2,
            "enabled": True,
            "profile": "coding",
            "requests_seen": 0,
            "requests_compressed": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "tokens_saved": 0,
            "compression_ratio": 0.0,
            "fail_open_count": 0,
            "failure_reasons": {
                "worker_busy": 0,
                "timeout": 0,
                "exception": 0,
                "inflation_guard": 0,
                "retrieval_marker": 0,
                "unsafe_output": 0,
                "other": 0,
            },
            "error_types": {
                "timeout": 0,
                "worker_busy": 0,
                "dependency_exception": 0,
                "inflation_guard": 0,
                "retrieval_marker": 0,
                "unsafe_output": 0,
                "other": 0,
            },
            "updated_at": None,
            "process_started_at": "2026-07-30T10:00:00+00:00",
            "measurement_started_at": "2026-07-30T10:00:00+00:00",
            "requests_passed_through": 0,
            "compression_success_rate": 0.0,
            "average_tokens_saved": 0.0,
            "outcome_counts": {
                "compressed": 0,
                "passed_through": 0,
                "fail_open": 0,
                "other": 0,
            },
            "provider_counts": {
                "claude": 0,
                "copilot": 0,
                "auggie": 0,
                "deepseek": 0,
                "kimi": 0,
                "ollama": 0,
                "codex-direct": 0,
                "openai-pass-through": 0,
                "other": 0,
            },
            "surface_counts": {
                "responses": 0,
                "anthropic_messages": 0,
                "other": 0,
            },
            "timeout_seconds": 2.0,
            "model_limit": 120000,
            "last_success_at": None,
            "last_failure_at": None,
            "reset_reason": "process_start",
        },
    }


def _env(tmp_path: Path) -> tuple[DriftEnvironment, FakeRunner]:
    home = tmp_path / "home"
    checkout = tmp_path / "canonical"
    checkout.mkdir(parents=True)
    runner = FakeRunner()
    runner.running_environment_checkout = str(checkout)
    runner.running_project = str(checkout)
    runner.running_working_directory = str(checkout)
    runner.scheduled_environment_checkout = str(checkout)
    runner.scheduled_project = str(checkout)
    runner.scheduled_working_directory = str(checkout)
    env = DriftEnvironment(
        repo_root=checkout,
        home=home,
        canonical_checkout=checkout,
        command_runner=runner,
        json_fetcher=lambda url: (
            _headroom_usage_payload()
            if url == deployment_drift.HEADROOM_USAGE_URL
            else {
                "data": [{"id": "kimi-k3", "owned_by": "moonshot"}],
                "model_discovery_source": "live",
            }
        ),
        uid=501,
        launcher=Path(LAUNCHER),
    )
    runner.running_kimi_code_home = str(env.kimi_code_home)
    return env, runner


def _bootstrap(env: DriftEnvironment) -> None:
    env.kimi_code_home.mkdir(parents=True, mode=0o700)
    env.kimi_code_home.chmod(0o700)
    write_deployment_provenance(
        env,
        selected_commit=COMMIT,
        installed_at_utc=dt.datetime(2026, 7, 25, tzinfo=dt.UTC),
    )
    _write_plists(env.home, checkout=env.canonical_checkout)


def _prepare_predecessor(
    env: DriftEnvironment,
    runner: FakeRunner,
    predecessor: str = OLD_COMMIT,
) -> None:
    env.kimi_code_home.mkdir(parents=True, mode=0o700)
    env.kimi_code_home.chmod(0o700)
    runner.head = predecessor
    write_deployment_provenance(
        env,
        selected_commit=predecessor,
        installed_at_utc=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
    )
    runner.head = COMMIT
    _write_plists(
        env.home,
        checkout=env.canonical_checkout,
        commit=predecessor,
    )
    runner.running_environment_commit = predecessor


def test_pre_install_allows_only_absent_first_install_record(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)

    report = check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert report["phase"] == "pre-install"
    assert report["provenance"] == "bootstrap-required"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("dirty", "clean"),
        ("wrong-head", "selected deployment commit"),
        ("arbitrary-checkout", "canonical checkout"),
    ),
)
def test_pre_install_fails_closed_on_source_drift(
    tmp_path: Path, mutation: str, message: str
) -> None:
    env, runner = _env(tmp_path)
    if mutation == "dirty":
        runner.dirty = True
    elif mutation == "wrong-head":
        runner.head = "b" * 40
    else:
        env = DriftEnvironment(
            repo_root=tmp_path / "other",
            home=env.home,
            canonical_checkout=env.canonical_checkout,
            command_runner=runner,
            json_fetcher=env.json_fetcher,
            uid=env.uid,
            launcher=env.launcher,
        )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)


def test_pre_install_allows_a_converged_known_predecessor_without_live_discovery(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner)
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda _url: (_ for _ in ()).throw(
            AssertionError("pre-install must not query live discovery")
        ),
        uid=env.uid,
        launcher=env.launcher,
    )

    report = check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert report["provenance"] == "valid-predecessor"


def test_pre_install_allows_squashed_predecessor_tree_in_selected_child_ancestry(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner)
    runner.ancestors.remove((OLD_COMMIT, COMMIT))
    runner.ancestry_trees[COMMIT] = [SELECTED_TREE, SQUASH_TREE]

    report = check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert report["provenance"] == "valid-squash-predecessor"


def test_pre_install_allows_squashed_predecessor_tree_at_selected_commit(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner)
    runner.ancestors.remove((OLD_COMMIT, COMMIT))
    runner.commit_trees[OLD_COMMIT] = SELECTED_TREE

    report = check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert report["provenance"] == "valid-squash-predecessor"


def test_pre_install_rejects_raw_tree_object_as_squashed_predecessor(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner, RAW_TREE_OBJECT)
    runner.ancestry_trees[COMMIT] = [SELECTED_TREE, SQUASH_TREE]

    with pytest.raises(DeploymentDriftError, match="known ancestor"):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert (
        "git",
        "rev-parse",
        f"{RAW_TREE_OBJECT}^{{commit}}",
    ) in runner.commands


def test_pre_install_rejects_tag_object_on_primary_ancestor_path(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner, TAG_OBJECT)

    with pytest.raises(DeploymentDriftError, match="known ancestor"):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert ("git", "rev-parse", f"{TAG_OBJECT}^{{commit}}") in runner.commands
    assert (
        "git",
        "merge-base",
        "--is-ancestor",
        TAG_OBJECT,
        COMMIT,
    ) not in runner.commands


@pytest.mark.parametrize("failure", (128, OSError("git is unavailable")))
def test_pre_install_does_not_treat_fatal_merge_base_error_as_non_ancestor(
    tmp_path: Path,
    failure: int | OSError,
) -> None:
    env, runner = _env(tmp_path)
    _prepare_predecessor(env, runner)
    runner.ancestors.remove((OLD_COMMIT, COMMIT))
    runner.merge_base_failures[(OLD_COMMIT, COMMIT)] = failure
    runner.ancestry_trees[COMMIT] = [SELECTED_TREE, SQUASH_TREE]

    with pytest.raises(
        DeploymentDriftError,
        match="determine deployment predecessor ancestry",
    ) as exc_info:
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert ("git", "rev-parse", f"{OLD_COMMIT}^{{commit}}") in runner.commands
    assert ("git", "rev-parse", f"{OLD_COMMIT}^{{tree}}") not in runner.commands
    runner_error = exc_info.value.__cause__
    assert isinstance(runner_error, DeploymentDriftError)
    if isinstance(failure, int):
        command_error = runner_error.__cause__
        assert isinstance(command_error, subprocess.CalledProcessError)
        assert command_error.returncode == failure
    else:
        assert runner_error.__cause__ is failure


def test_pre_install_validates_predecessor_with_its_recorded_launcher(
    tmp_path: Path,
) -> None:
    current_env, runner = _env(tmp_path)
    predecessor_env = DriftEnvironment(
        repo_root=current_env.repo_root,
        home=current_env.home,
        canonical_checkout=current_env.canonical_checkout,
        command_runner=runner,
        json_fetcher=current_env.json_fetcher,
        uid=current_env.uid,
        launcher=Path(PREDECESSOR_LAUNCHER),
    )
    predecessor_env.kimi_code_home.mkdir(parents=True, mode=0o700)
    predecessor_env.kimi_code_home.chmod(0o700)
    runner.head = OLD_COMMIT
    write_deployment_provenance(
        predecessor_env,
        selected_commit=OLD_COMMIT,
        installed_at_utc=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
    )
    runner.head = COMMIT
    runner.running_environment_commit = OLD_COMMIT
    runner.running_program = PREDECESSOR_LAUNCHER
    runner.running_argument_zero = PREDECESSOR_LAUNCHER
    _write_plists(
        current_env.home,
        checkout=current_env.canonical_checkout,
        commit=OLD_COMMIT,
        launcher=PREDECESSOR_LAUNCHER,
    )

    report = check_deployment_drift(
        "pre-install",
        current_env,
        selected_commit=COMMIT,
    )

    assert report["provenance"] == "valid-predecessor"


def test_pre_install_allows_one_way_schema_one_upgrade(tmp_path: Path) -> None:
    env, runner = _env(tmp_path)
    legacy_record = {
        "schema_version": 1,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": OLD_COMMIT,
        "installer": INSTALLER_IDENTITY,
        "launcher": LAUNCHER,
        "installed_at_utc": "2026-07-24T00:00:00Z",
    }
    env.provenance_path.parent.mkdir(parents=True)
    env.provenance_path.write_text(json.dumps(legacy_record), encoding="utf-8")
    _write_plists(
        env.home,
        checkout=env.canonical_checkout,
        commit=OLD_COMMIT,
        include_kimi_home=False,
    )
    runner.running_environment_commit = OLD_COMMIT
    runner.running_kimi_code_home = None

    report = check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert report["provenance"] == "valid-schema-one-predecessor"

    with pytest.raises(DeploymentDriftError, match="schema is unsupported"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


@pytest.mark.parametrize("recorded_object", (RAW_TREE_OBJECT, TAG_OBJECT))
def test_pre_install_schema_one_rejects_non_commit_predecessor_before_merge_base(
    tmp_path: Path,
    recorded_object: str,
) -> None:
    env, runner = _env(tmp_path)
    legacy_record = {
        "schema_version": 1,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": recorded_object,
        "installer": INSTALLER_IDENTITY,
        "launcher": LAUNCHER,
        "installed_at_utc": "2026-07-24T00:00:00Z",
    }
    env.provenance_path.parent.mkdir(parents=True)
    env.provenance_path.write_text(json.dumps(legacy_record), encoding="utf-8")
    _write_plists(
        env.home,
        checkout=env.canonical_checkout,
        commit=recorded_object,
        include_kimi_home=False,
    )
    runner.running_environment_commit = recorded_object
    runner.running_kimi_code_home = None

    with pytest.raises(DeploymentDriftError, match="known ancestor"):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    assert ("git", "rev-parse", f"{recorded_object}^{{commit}}") in runner.commands
    assert (
        "git",
        "merge-base",
        "--is-ancestor",
        recorded_object,
        COMMIT,
    ) not in runner.commands


@pytest.mark.parametrize("authority", ("rendered", "running"))
def test_pre_install_schema_one_rejects_legacy_kimi_home(
    tmp_path: Path,
    authority: str,
) -> None:
    env, runner = _env(tmp_path)
    legacy_record = {
        "schema_version": 1,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": OLD_COMMIT,
        "installer": INSTALLER_IDENTITY,
        "launcher": LAUNCHER,
        "installed_at_utc": "2026-07-24T00:00:00Z",
    }
    env.provenance_path.parent.mkdir(parents=True)
    env.provenance_path.write_text(json.dumps(legacy_record), encoding="utf-8")
    _write_plists(
        env.home,
        checkout=env.canonical_checkout,
        commit=OLD_COMMIT,
        include_kimi_home=authority == "rendered",
    )
    runner.running_environment_commit = OLD_COMMIT
    runner.running_kimi_code_home = (
        str(env.kimi_code_home) if authority == "running" else None
    )

    with pytest.raises(DeploymentDriftError, match="must not set KIMI_CODE_HOME"):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("rendered-authority", "rendered"),
        ("running-authority", "running revision"),
        ("non-ancestor", "known ancestor"),
        ("unknown-commit", "known ancestor"),
    ),
)
def test_pre_install_rejects_unauthorized_predecessor_transition(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    env, runner = _env(tmp_path)
    predecessor = (
        NON_ANCESTOR_COMMIT
        if mutation == "non-ancestor"
        else UNKNOWN_COMMIT
        if mutation == "unknown-commit"
        else OLD_COMMIT
    )
    _prepare_predecessor(env, runner, predecessor)
    if mutation == "rendered-authority":
        _write_plists(
            env.home,
            checkout=env.canonical_checkout,
            commit=COMMIT,
        )
    elif mutation == "running-authority":
        runner.running_environment_commit = COMMIT

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)

    if mutation in {"non-ancestor", "unknown-commit"}:
        assert (
            "git",
            "rev-parse",
            f"{predecessor}^{{commit}}",
        ) in runner.commands
        tree_command = ("git", "rev-parse", f"{predecessor}^{{tree}}")
        log_command = ("git", "log", "--format=%T", COMMIT)
        expected_tree_lookup = mutation == "non-ancestor"
        assert (tree_command in runner.commands) is expected_tree_lookup
        assert (log_command in runner.commands) is expected_tree_lookup


@pytest.mark.parametrize(
    "payload",
    (
        "{not json",
        json.dumps({"schema_version": 999}),
        json.dumps(
            {
                "schema_version": 1,
                "repository": DEPLOYMENT_REPOSITORY,
                "canonical_checkout": "/stale/reverso",
                "commit": COMMIT,
                "installer": INSTALLER_IDENTITY,
                "installed_at_utc": "2026-07-25T00:00:00Z",
            }
        ),
    ),
)
def test_existing_invalid_provenance_fails_even_at_pre_install(
    tmp_path: Path, payload: str
) -> None:
    env, _ = _env(tmp_path)
    env.provenance_path.parent.mkdir(parents=True)
    env.provenance_path.write_text(payload, encoding="utf-8")

    with pytest.raises(DeploymentDriftError, match="provenance"):
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)


def test_provenance_write_is_atomic_and_read_back(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    env.kimi_code_home.mkdir(parents=True, mode=0o700)
    env.kimi_code_home.chmod(0o700)

    record = write_deployment_provenance(
        env,
        selected_commit=COMMIT,
        installed_at_utc=dt.datetime(2026, 7, 25, 12, 30, tzinfo=dt.UTC),
    )

    assert record == json.loads(env.provenance_path.read_text(encoding="utf-8"))
    assert record == {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": COMMIT,
        "installer": INSTALLER_IDENTITY,
        "launcher": LAUNCHER,
        "kimi_code_home": str(env.kimi_code_home),
        "installed_at_utc": "2026-07-25T12:30:00Z",
    }
    assert env.provenance_path.stat().st_mode & 0o777 == 0o600
    assert not list(env.provenance_path.parent.glob("*.tmp"))


def test_provenance_write_rejects_symlinked_reverso_parent(
    tmp_path: Path,
) -> None:
    env, _ = _env(tmp_path)
    default_kimi_home = env.home / ".kimi-code"
    redirected_kimi_home = default_kimi_home / "kimi-code"
    redirected_kimi_home.mkdir(parents=True, mode=0o700)
    env.kimi_code_home.parent.parent.mkdir(parents=True)
    env.kimi_code_home.parent.symlink_to(
        default_kimi_home,
        target_is_directory=True,
    )

    with pytest.raises(DeploymentDriftError, match="must not contain symbolic links"):
        write_deployment_provenance(env, selected_commit=COMMIT)

    assert not env.provenance_path.exists()


def test_exact_deployment_rejects_provenance_launcher_drift(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    record = json.loads(env.provenance_path.read_text(encoding="utf-8"))
    record["launcher"] = UNAUTHORIZED_LAUNCHER
    env.provenance_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(DeploymentDriftError, match="provenance launcher"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


def test_pre_restart_rejects_provenance_kimi_home_drift(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    record = json.loads(env.provenance_path.read_text(encoding="utf-8"))
    record["kimi_code_home"] = str(tmp_path / "stale-kimi-home")
    env.provenance_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(DeploymentDriftError, match="kimi_code_home"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


@pytest.mark.parametrize("mode", (0o755, 0o750, 0o777))
def test_pre_restart_requires_private_kimi_home(tmp_path: Path, mode: int) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    env.kimi_code_home.chmod(mode)

    with pytest.raises(DeploymentDriftError, match="mode 0700"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


def test_pre_restart_requires_kimi_home_directory(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    env.kimi_code_home.rmdir()

    with pytest.raises(DeploymentDriftError, match="real directory"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


def test_pre_restart_rejects_symlinked_kimi_home(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    env.kimi_code_home.rmdir()
    target = tmp_path / "external-kimi-home"
    target.mkdir()
    env.kimi_code_home.symlink_to(target, target_is_directory=True)

    with pytest.raises(DeploymentDriftError, match="must not contain symbolic links"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


def test_pre_restart_rejects_symlinked_reverso_parent_to_default_kimi_home(
    tmp_path: Path,
) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    provenance = env.provenance_path.read_text(encoding="utf-8")
    env.kimi_code_home.rmdir()
    env.provenance_path.unlink()
    env.kimi_code_home.parent.rmdir()
    default_kimi_home = env.home / ".kimi-code"
    default_kimi_home.mkdir()
    (default_kimi_home / env.provenance_path.name).write_text(
        provenance,
        encoding="utf-8",
    )
    env.kimi_code_home.parent.symlink_to(
        default_kimi_home,
        target_is_directory=True,
    )
    redirected_kimi_home = default_kimi_home / "kimi-code"
    redirected_kimi_home.mkdir(mode=0o700)

    with pytest.raises(DeploymentDriftError, match="must not contain symbolic links"):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    "mutation",
    (
        "stale-project",
        "duplicate-project",
        "wrong-executable",
        "unauthorized-program",
        "unauthorized-argv0",
    ),
)
def test_pre_restart_rejects_rendered_launchagent_drift(
    tmp_path: Path, mutation: str
) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    proxy_path = env.home / "Library" / "LaunchAgents" / "com.user.reverso-proxy.plist"
    with proxy_path.open("rb") as handle:
        payload = plistlib.load(handle)
    if mutation == "stale-project":
        payload["ProgramArguments"][3] = str(tmp_path / "stale")
    elif mutation == "duplicate-project":
        payload["ProgramArguments"].extend(["--project", str(env.canonical_checkout)])
    elif mutation == "wrong-executable":
        payload["ProgramArguments"][-1] = "reverso-daemon"
    elif mutation == "unauthorized-program":
        payload["Program"] = UNAUTHORIZED_LAUNCHER
    else:
        payload["ProgramArguments"][0] = UNAUTHORIZED_LAUNCHER
    with proxy_path.open("wb") as handle:
        plistlib.dump(payload, handle)

    message = "Program$" if mutation == "unauthorized-program" else "ProgramArguments"
    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("schedule", "schedule"),
        ("keep-alive", "KeepAlive"),
        ("executable", "ProgramArguments"),
        ("argument-zero", "ProgramArguments"),
        ("listener", "Sockets"),
        ("working-directory", "WorkingDirectory"),
        ("commit", "revision provenance"),
        ("missing", "missing or malformed"),
        ("malformed", "missing or malformed"),
    ),
)
def test_pre_restart_rejects_scheduled_launchagent_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    path = (
        env.home
        / "Library"
        / "LaunchAgents"
        / f"{deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL}.plist"
    )
    if mutation == "missing":
        path.unlink()
    elif mutation == "malformed":
        path.write_text("not a plist", encoding="utf-8")
    else:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        if mutation == "schedule":
            payload["StartCalendarInterval"] = [{"Hour": 7, "Minute": 0}]
        elif mutation == "keep-alive":
            payload["KeepAlive"] = True
        elif mutation == "executable":
            payload["ProgramArguments"][-1] = "reverso-proxy"
        elif mutation == "argument-zero":
            payload["ProgramArguments"][0] = UNAUTHORIZED_LAUNCHER
        elif mutation == "listener":
            payload["Sockets"] = {"Listener": {"SockServiceName": "64946"}}
        elif mutation == "working-directory":
            payload["WorkingDirectory"] = str(tmp_path / "stale")
        else:
            payload["EnvironmentVariables"]["REVERSO_DEPLOYMENT_COMMIT"] = OLD_COMMIT
        with path.open("wb") as handle:
            plistlib.dump(payload, handle)

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


def test_scheduled_agent_stays_outside_long_lived_runtime_map() -> None:
    assert deployment_drift.LAUNCH_AGENT_EXECUTABLES == {
        "com.user.reverso-proxy": "reverso-proxy",
        "com.user.reverso-daemon": "reverso-daemon",
    }


@pytest.mark.parametrize(
    ("label", "value", "message"),
    (
        ("com.user.reverso-proxy", "/stale/kimi-home", "KIMI_CODE_HOME"),
        ("com.user.reverso-daemon", "/unauthorized/kimi-home", "must not set"),
    ),
)
def test_pre_restart_rejects_rendered_kimi_home_drift(
    tmp_path: Path,
    label: str,
    value: str,
    message: str,
) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    path = env.home / "Library" / "LaunchAgents" / f"{label}.plist"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    payload["EnvironmentVariables"]["KIMI_CODE_HOME"] = value
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("environment-commit", "running revision"),
        ("environment-checkout", "running checkout"),
        ("project", "ProgramArguments"),
        ("working-directory", "WorkingDirectory"),
        ("program", "unauthorized program"),
        ("argument-zero", "ProgramArguments"),
        ("joint-launcher", "unauthorized program"),
        ("executable", "ProgramArguments"),
        ("duplicate-project", "ProgramArguments"),
    ),
)
def test_post_restart_rejects_running_authority_without_live_discovery(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    if mutation == "environment-commit":
        runner.running_environment_commit = OLD_COMMIT
    elif mutation == "environment-checkout":
        runner.running_environment_checkout = "/stale/reverso"
    elif mutation == "project":
        runner.running_project = "/stale/reverso"
    elif mutation == "working-directory":
        runner.running_working_directory = "/stale/reverso"
    elif mutation == "program":
        runner.running_program = UNAUTHORIZED_LAUNCHER
    elif mutation == "argument-zero":
        runner.running_argument_zero = UNAUTHORIZED_LAUNCHER
    elif mutation == "joint-launcher":
        runner.running_program = UNAUTHORIZED_LAUNCHER
        runner.running_argument_zero = UNAUTHORIZED_LAUNCHER
    elif mutation == "executable":
        runner.running_executables["com.user.reverso-proxy"] = "reverso-daemon"
    else:
        runner.running_extra_arguments["com.user.reverso-proxy"] = [
            "--project",
            str(env.canonical_checkout),
        ]
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda _url: (_ for _ in ()).throw(
            AssertionError("post-restart must not query live discovery")
        ),
        uid=env.uid,
        launcher=env.launcher,
    )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("post-restart", env, selected_commit=COMMIT)


def test_post_restart_passes_without_live_discovery(tmp_path: Path) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda _url: (_ for _ in ()).throw(
            AssertionError("post-restart must not query live discovery")
        ),
        uid=env.uid,
        launcher=env.launcher,
    )

    assert (
        check_deployment_drift("post-restart", env, selected_commit=COMMIT)["status"]
        == "passed"
    )
    assert not any(
        command[-1].endswith(deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL)
        for command in runner.commands
        if command[:2] == ("launchctl", "print")
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("label", "wrong label"),
        ("program", "unauthorized program"),
        ("arguments", "ProgramArguments"),
        ("project-argument", "ProgramArguments"),
        ("working-directory", "WorkingDirectory"),
        ("project", "running checkout"),
        ("commit", "running revision"),
        ("keepalive-property", "KeepAlive"),
        ("runatload-property", "RunAtLoad"),
        ("listener", "Sockets"),
        ("log-path", "StandardOutPath"),
        ("missing-schedule", "schedule"),
        ("extra-schedule", "schedule"),
        ("wrong-schedule", "schedule"),
    ),
)
def test_post_load_rejects_running_scheduled_launchagent_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    if mutation == "label":
        runner.scheduled_label = "com.user.wrong"
    elif mutation == "program":
        runner.scheduled_program = UNAUTHORIZED_LAUNCHER
    elif mutation == "arguments":
        runner.scheduled_arguments = [
            LAUNCHER,
            "run",
            "--project",
            str(env.canonical_checkout),
            "reverso-proxy",
        ]
    elif mutation == "project-argument":
        runner.scheduled_project = "/stale/reverso"
    elif mutation == "working-directory":
        runner.scheduled_working_directory = "/stale/reverso"
    elif mutation == "project":
        runner.scheduled_environment_checkout = "/stale/reverso"
    elif mutation == "commit":
        runner.scheduled_environment_commit = OLD_COMMIT
    elif mutation == "keepalive-property":
        runner.scheduled_properties = "keepalive"
    elif mutation == "runatload-property":
        runner.scheduled_properties = "runatload"
    elif mutation == "listener":
        runner.scheduled_top_level_key = "sockets"
    elif mutation == "log-path":
        runner.scheduled_top_level_key = "stdout path"
    elif mutation == "missing-schedule":
        runner.scheduled_intervals.pop()
    elif mutation == "extra-schedule":
        runner.scheduled_intervals.append({"Hour": 23, "Minute": 0})
    else:
        runner.scheduled_intervals[0] = {"Hour": 7, "Minute": 0}

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("post-load", env, selected_commit=COMMIT)


def test_post_load_accepts_running_scheduled_launchagent_readback(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)

    report = check_deployment_drift("post-load", env, selected_commit=COMMIT)

    assert report["status"] == "passed"
    assert (
        "launchctl",
        "print",
        f"gui/{env.uid}/{deployment_drift.SCHEDULED_LAUNCH_AGENT_LABEL}",
    ) in runner.commands


def test_post_load_accepts_reversed_running_calendar_trigger_order(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    runner.scheduled_intervals.reverse()

    report = check_deployment_drift("post-load", env, selected_commit=COMMIT)

    assert report["status"] == "passed"


@pytest.mark.parametrize("value", (None, "/stale/kimi-home"))
def test_post_restart_rejects_running_proxy_kimi_home_drift(
    tmp_path: Path,
    value: str | None,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    runner.running_kimi_code_home = value

    with pytest.raises(DeploymentDriftError, match="running KIMI_CODE_HOME"):
        check_deployment_drift("post-restart", env, selected_commit=COMMIT)


def test_post_restart_rejects_running_daemon_kimi_home(tmp_path: Path) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    runner.running_daemon_kimi_code_home = str(env.kimi_code_home)

    with pytest.raises(DeploymentDriftError, match="daemon must not set"):
        check_deployment_drift("post-restart", env, selected_commit=COMMIT)


def test_pre_sync_rejects_joint_unauthorized_rendered_and_running_launcher(
    tmp_path: Path,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    _write_plists(
        env.home,
        checkout=env.canonical_checkout,
        launcher=UNAUTHORIZED_LAUNCHER,
    )
    runner.running_program = UNAUTHORIZED_LAUNCHER
    runner.running_argument_zero = UNAUTHORIZED_LAUNCHER

    with pytest.raises(DeploymentDriftError, match="unauthorized Program"):
        check_deployment_drift("pre-sync", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("running-commit", "running revision"),
        ("running-checkout", "running checkout"),
        ("live-model", "kimi-k3"),
        ("malformed-extra", "exactly one"),
        ("discovery", "live"),
    ),
)
def test_pre_sync_rejects_each_runtime_mismatch(
    tmp_path: Path, mutation: str, message: str
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    payload: dict[str, Any] = {
        "data": [{"id": "kimi-k3"}],
        "model_discovery_source": "live",
    }
    if mutation == "running-commit":
        runner.running_environment_commit = OLD_COMMIT
    elif mutation == "running-checkout":
        runner.running_environment_checkout = "/stale/reverso"
    elif mutation == "live-model":
        payload["data"] = [{"id": "kimi-k2.5"}]
    elif mutation == "malformed-extra":
        payload["data"] = [{"id": "kimi-k3"}, "malformed"]
    else:
        payload["model_discovery_source"] = "fallback"
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda _url: payload,
        uid=env.uid,
        launcher=env.launcher,
    )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-sync", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("failure", "message"),
    (
        (OSError("unavailable"), "live Kimi discovery is unavailable or malformed"),
        (
            json.JSONDecodeError("malformed", "{", 0),
            "live Kimi discovery is unavailable or malformed",
        ),
    ),
)
def test_pre_sync_reports_kimi_fetch_failures_with_kimi_authority(
    tmp_path: Path,
    failure: Exception,
    message: str,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda _url: (_ for _ in ()).throw(failure),
        uid=env.uid,
        launcher=env.launcher,
    )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("pre-sync", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("profile-model", "profile model"),
        ("profile-provider", "profile provider"),
        ("profile-auto-compact", "auto compact token limit"),
        ("catalog-slug", "catalog slug"),
        ("catalog-context", "catalog context"),
    ),
)
def test_acceptance_rejects_each_generated_metadata_mismatch(
    tmp_path: Path, mutation: str, message: str
) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    _write_generated_kimi(env.home)
    profile_path = env.home / ".codex" / "reverso-kimi.config.toml"
    catalog_path = env.home / ".codex" / "reverso" / "kimi.json"
    if mutation == "profile-model":
        profile_path.write_text(
            profile_path.read_text().replace("kimi-k3", "kimi-k2.5", 1)
        )
    elif mutation == "profile-provider":
        profile_path.write_text(
            profile_path.read_text().replace("reverso_kimi", "reverso_stale")
        )
    elif mutation == "profile-auto-compact":
        profile_path.write_text(
            profile_path.read_text().replace(
                "model_auto_compact_token_limit = 943718",
                "model_auto_compact_token_limit = 108800",
            )
        )
    else:
        catalog = json.loads(catalog_path.read_text())
        if mutation == "catalog-slug":
            catalog["models"][0]["slug"] = "kimi-k2.5"
        else:
            catalog["models"][0]["context_window"] = 262144
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("acceptance", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("outer-schema", "outer schema"),
        ("provider", "provider"),
        ("inner-schema", "inner schema"),
        ("profile", "profile"),
        ("extra-field", "exact fields"),
        ("sensitive-field", "sensitive field"),
    ),
)
def test_acceptance_rejects_each_live_headroom_mismatch(
    tmp_path: Path, mutation: str, message: str
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    _write_generated_kimi(env.home)
    payload = _headroom_usage_payload()
    if mutation == "outer-schema":
        payload["schema_version"] = 2
    elif mutation == "provider":
        payload["provider"] = "standalone"
    elif mutation == "inner-schema":
        payload["headroom"]["schema_version"] = 1
    elif mutation == "profile":
        payload["headroom"]["profile"] = "generic"
    elif mutation == "extra-field":
        payload["headroom"]["unexpected"] = True
    else:
        payload["headroom"]["prompt"] = "must not be exposed"
    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=lambda url: (
            payload
            if url == deployment_drift.HEADROOM_USAGE_URL
            else {
                "data": [{"id": "kimi-k3"}],
                "model_discovery_source": "live",
            }
        ),
        uid=env.uid,
        launcher=env.launcher,
    )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("acceptance", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("failure", "message"),
    (
        (OSError("unavailable"), "live Headroom usage is unavailable or malformed"),
        (
            json.JSONDecodeError("malformed", "{", 0),
            "live Headroom usage is unavailable or malformed",
        ),
    ),
)
def test_acceptance_reports_headroom_fetch_failures_with_headroom_authority(
    tmp_path: Path,
    failure: Exception,
    message: str,
) -> None:
    env, runner = _env(tmp_path)
    _bootstrap(env)
    _write_generated_kimi(env.home)

    def fetch(url: str) -> Any:
        if url == deployment_drift.KIMI_MODELS_URL:
            return {
                "data": [{"id": "kimi-k3"}],
                "model_discovery_source": "live",
            }
        raise failure

    env = DriftEnvironment(
        repo_root=env.repo_root,
        home=env.home,
        canonical_checkout=env.canonical_checkout,
        command_runner=runner,
        json_fetcher=fetch,
        uid=env.uid,
        launcher=env.launcher,
    )

    with pytest.raises(DeploymentDriftError, match=message):
        check_deployment_drift("acceptance", env, selected_commit=COMMIT)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("enabled", 1, "enabled"),
        ("requests_seen", -1, "requests_seen"),
        ("requests_compressed", True, "requests_compressed"),
        ("compression_ratio", float("nan"), "compression_ratio"),
        ("compression_success_rate", 2.0, "compression_success_rate"),
        ("average_tokens_saved", float("inf"), "average_tokens_saved"),
        ("timeout_seconds", 0.0, "timeout_seconds"),
        ("model_limit", False, "model_limit"),
        ("updated_at", "2026-07-30T10:00:00+02:00", "updated_at"),
        ("reset_reason", "manual", "reset_reason"),
    ),
)
def test_live_headroom_validator_rejects_invalid_scalar_contract(
    field: str, value: Any, message: str
) -> None:
    payload = _headroom_usage_payload()
    payload["headroom"][field] = value

    with pytest.raises(DeploymentDriftError, match=message):
        deployment_drift.validate_headroom_usage_payload(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("compression_ratio", 0.1),
        ("compression_success_rate", 0.1),
        ("average_tokens_saved", 1.0),
        ("requests_passed_through", 1),
    ),
)
def test_live_headroom_validator_rejects_formula_drift(
    field: str, value: float
) -> None:
    payload = _headroom_usage_payload()
    payload["headroom"][field] = value

    with pytest.raises(DeploymentDriftError, match="governed formula"):
        deployment_drift.validate_headroom_usage_payload(payload)


@pytest.mark.parametrize(
    ("configured", "expected"),
    (
        (None, "coding"),
        ("", "coding"),
        ("   ", "coding"),
        ("agent-90", "agent-90"),
    ),
)
def test_live_headroom_validator_uses_normalized_configured_profile(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: str,
) -> None:
    if configured is None:
        monkeypatch.delenv("REVERSO_HEADROOM_PROFILE", raising=False)
    else:
        monkeypatch.setenv("REVERSO_HEADROOM_PROFILE", configured)
    payload = _headroom_usage_payload()
    payload["headroom"]["profile"] = expected

    deployment_drift.validate_headroom_usage_payload(payload)


def test_live_headroom_validator_rejects_profile_other_than_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVERSO_HEADROOM_PROFILE", "agent-90")

    with pytest.raises(DeploymentDriftError, match="agent-90"):
        deployment_drift.validate_headroom_usage_payload(_headroom_usage_payload())


def test_all_phases_pass_when_authorities_converge(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    assert (
        check_deployment_drift("pre-install", env, selected_commit=COMMIT)["provenance"]
        == "bootstrap-required"
    )
    _bootstrap(env)

    assert (
        check_deployment_drift("pre-restart", env, selected_commit=COMMIT)["status"]
        == "passed"
    )
    assert (
        check_deployment_drift("post-restart", env, selected_commit=COMMIT)["status"]
        == "passed"
    )
    assert (
        check_deployment_drift("post-load", env, selected_commit=COMMIT)["status"]
        == "passed"
    )
    assert (
        check_deployment_drift("pre-sync", env, selected_commit=COMMIT)["status"]
        == "passed"
    )
    _write_generated_kimi(env.home)
    assert (
        check_deployment_drift("acceptance", env, selected_commit=COMMIT)["status"]
        == "passed"
    )


def test_installer_orders_all_drift_gates_around_launchctl() -> None:
    script = Path("scripts/install-launchagents.sh").read_text(encoding="utf-8")

    pre_install = script.index("--phase pre-install")
    write = script.index("--write-provenance")
    pre_restart = script.index("--phase pre-restart")
    launchctl = script.index("launchctl unload")
    post_restart = script.index("--phase post-restart")
    scheduled_load = script.index('launchctl load "${SCHEDULED_PLIST}"')
    post_load = script.index("--phase post-load")
    initial_refresh = script.index(
        '"${UV_BIN}" run --project "${REVERSO_DIR}" reverso-catalog-refresh'
    )
    done = script.index("Done. Reverso LaunchAgents installed.")

    assert pre_install < write < pre_restart < launchctl < post_restart < done
    assert post_restart < scheduled_load < post_load < initial_refresh < done
    assert str(CANONICAL_CHECKOUT) in script
    assert 'CANONICAL_USER_HOME="/Users/andresilvaburgstahler"' in script
    assert '"${HOME}" != "${CANONICAL_USER_HOME}"' in script
    assert 'USER_HOME="${CANONICAL_USER_HOME}"' in script
    assert 'export REVERSO_UV_BIN="${UV_BIN}"' in script
    assert '"${UV_BIN}" run --project "${REVERSO_DIR}" python' in script
    assert '"${REVERSO_DIR}/scripts/check-deployment-drift.py"' in script
    assert (
        'KIMI_CODE_HOME="${USER_HOME}/Library/Application Support/reverso/kimi-code"'
        in script
    )
    assert "require_real_kimi_home_path() {" in script
    assert '"${USER_HOME}" \\' in script
    assert '"${USER_HOME}/Library/Application Support/reverso"' in script
    assert 'if [[ -L "${path_component}" ]]; then' in script
    assert script.index('"${HOME}" != "${CANONICAL_USER_HOME}"') < script.index(
        "# Locate uv"
    )
    assert script.index("require_real_kimi_home_path\n\n# Locate uv") < script.index(
        'REVERSO_DEPLOYMENT_COMMIT="$(git'
    )
    assert script.count("require_real_kimi_home_path") == 5
    assert 'chmod 0700 "${KIMI_CODE_HOME}"' in script
    proxy_template = Path("launchd/com.user.reverso-proxy.plist.tmpl").read_text(
        encoding="utf-8"
    )
    daemon_template = Path("launchd/com.user.reverso-daemon.plist.tmpl").read_text(
        encoding="utf-8"
    )
    assert "<key>KIMI_CODE_HOME</key>" in proxy_template
    assert "KIMI_CODE_HOME" not in daemon_template


def test_drift_cli_is_available_and_rejects_this_arbitrary_checkout() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/check-deployment-drift.py",
            "--phase",
            "pre-install",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "canonical checkout" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("configured_home", (None, "/tmp/poisoned-home"))
def test_main_rejects_missing_or_overridden_home_before_provenance_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured_home: str | None,
) -> None:
    account_home = tmp_path / "account-home"
    account_home.mkdir()
    monkeypatch.setattr(deployment_drift.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        deployment_drift.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(account_home)),
    )
    if configured_home is None:
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", configured_home)

    result = deployment_drift.main(
        ["--phase", "pre-install", "--write-provenance"],
        repo_root=tmp_path,
    )

    assert result == 2
    assert "HOME must match the governed account home" in capsys.readouterr().err
    assert not (
        account_home
        / "Library"
        / "Application Support"
        / "reverso"
        / "deployment-provenance.json"
    ).exists()


def test_main_rejects_symlinked_account_home_before_provenance_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    account_home = tmp_path / "account-home"
    account_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(account_home))
    monkeypatch.setattr(deployment_drift.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        deployment_drift.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(account_home)),
    )

    result = deployment_drift.main(
        ["--phase", "pre-install", "--write-provenance"],
        repo_root=tmp_path,
    )

    assert result == 2
    assert "free of symbolic links" in capsys.readouterr().err
    assert not (
        real_home
        / "Library"
        / "Application Support"
        / "reverso"
        / "deployment-provenance.json"
    ).exists()


@pytest.mark.parametrize("poisoned_home", ("/tmp/isolated", "/"))
def test_isolated_verification_env_cannot_bypass_production_drift_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    poisoned_home: str,
) -> None:
    account_home = tmp_path / "account-home"
    account_home.mkdir()
    calls: list[tuple[str, Path, str]] = []
    monkeypatch.setenv("REVERSO_ISOLATED_VERIFICATION_HOME", poisoned_home)
    monkeypatch.setattr(deployment_drift, "_production_home", lambda: account_home)
    monkeypatch.setattr(deployment_drift, "_selected_commit", lambda _env: COMMIT)

    def check(
        phase: str,
        env: DriftEnvironment,
        *,
        selected_commit: str,
    ) -> dict[str, str]:
        calls.append((phase, env.home, selected_commit))
        return {"phase": phase, "status": "passed", "provenance": "validated"}

    monkeypatch.setattr(deployment_drift, "check_deployment_drift", check)

    result = deployment_drift.main(["--phase", "acceptance"], repo_root=tmp_path)

    assert result == 0
    assert calls == [("acceptance", account_home, COMMIT)]
    assert "isolated_convergence_verified" not in capsys.readouterr().out
