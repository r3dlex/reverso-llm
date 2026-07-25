from __future__ import annotations

import datetime as dt
import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reverso.deployment_drift import (
    CANONICAL_CHECKOUT,
    DEPLOYMENT_REPOSITORY,
    INSTALLER_IDENTITY,
    DeploymentDriftError,
    DriftEnvironment,
    check_deployment_drift,
    write_deployment_provenance,
)

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
NON_ANCESTOR_COMMIT = "c" * 40
UNKNOWN_COMMIT = "d" * 40
LAUNCHER = "/opt/homebrew/bin/uv"
PREDECESSOR_LAUNCHER = "/usr/local/bin/uv"
UNAUTHORIZED_LAUNCHER = "/tmp/unauthorized-launcher"


class FakeRunner:
    def __init__(self) -> None:
        self.dirty = False
        self.head = COMMIT
        self.remote = DEPLOYMENT_REPOSITORY
        self.running_environment_commit = COMMIT
        self.running_environment_checkout = str(CANONICAL_CHECKOUT)
        self.running_program = LAUNCHER
        self.running_argument_zero = LAUNCHER
        self.running_project = str(CANONICAL_CHECKOUT)
        self.running_working_directory = str(CANONICAL_CHECKOUT)
        self.running_executables = {
            "com.user.reverso-proxy": "reverso-proxy",
            "com.user.reverso-daemon": "reverso-daemon",
        }
        self.running_extra_arguments: dict[str, list[str]] = {}

    def __call__(self, command: tuple[str, ...], cwd: Path | None) -> str:
        if command[:2] == ("git", "status"):
            return " M tracked.py\n" if self.dirty else ""
        if command[:2] == ("git", "rev-parse"):
            return self.head + "\n"
        if command[:3] == ("git", "remote", "get-url"):
            return self.remote + "\n"
        if command[:3] == ("git", "merge-base", "--is-ancestor"):
            if command[3:] == (OLD_COMMIT, COMMIT):
                return ""
            raise DeploymentDriftError("commit is not an ancestor")
        if command[:2] == ("launchctl", "print"):
            label = command[-1].rsplit("/", 1)[-1]
            arguments = [
                self.running_argument_zero,
                "run",
                "--project",
                self.running_project,
                self.running_executables[label],
                *self.running_extra_arguments.get(label, []),
            ]
            rendered_arguments = "\n".join(f"        {value}" for value in arguments)
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
        with (launchd_dir / f"{label}.plist").open("wb") as handle:
            plistlib.dump(payload, handle)


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
    (codex_dir / "kimi.config.toml").write_text(
        "\n".join(
            (
                'model = "kimi-k3"',
                'model_provider = "reverso_kimi"',
                f'model_catalog_json = "{catalog}"',
                "model_context_window = 1048576",
                "",
            )
        ),
        encoding="utf-8",
    )


def _env(tmp_path: Path) -> tuple[DriftEnvironment, FakeRunner]:
    home = tmp_path / "home"
    checkout = tmp_path / "canonical"
    checkout.mkdir(parents=True)
    runner = FakeRunner()
    runner.running_environment_checkout = str(checkout)
    runner.running_project = str(checkout)
    runner.running_working_directory = str(checkout)
    env = DriftEnvironment(
        repo_root=checkout,
        home=home,
        canonical_checkout=checkout,
        command_runner=runner,
        json_fetcher=lambda _url: {
            "data": [{"id": "kimi-k3", "owned_by": "moonshot"}],
            "model_discovery_source": "live",
        },
        uid=501,
        launcher=Path(LAUNCHER),
    )
    return env, runner


def _bootstrap(env: DriftEnvironment) -> None:
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

    record = write_deployment_provenance(
        env,
        selected_commit=COMMIT,
        installed_at_utc=dt.datetime(2026, 7, 25, 12, 30, tzinfo=dt.UTC),
    )

    assert record == json.loads(env.provenance_path.read_text(encoding="utf-8"))
    assert record == {
        "schema_version": 1,
        "repository": DEPLOYMENT_REPOSITORY,
        "canonical_checkout": str(env.canonical_checkout),
        "commit": COMMIT,
        "installer": INSTALLER_IDENTITY,
        "launcher": LAUNCHER,
        "installed_at_utc": "2026-07-25T12:30:00Z",
    }
    assert env.provenance_path.stat().st_mode & 0o777 == 0o600
    assert not list(env.provenance_path.parent.glob("*.tmp"))


def test_exact_deployment_rejects_provenance_launcher_drift(tmp_path: Path) -> None:
    env, _ = _env(tmp_path)
    _bootstrap(env)
    record = json.loads(env.provenance_path.read_text(encoding="utf-8"))
    record["launcher"] = UNAUTHORIZED_LAUNCHER
    env.provenance_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(DeploymentDriftError, match="provenance launcher"):
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
    ("mutation", "message"),
    (
        ("profile-model", "profile model"),
        ("profile-provider", "profile provider"),
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
    profile_path = env.home / ".codex" / "kimi.config.toml"
    catalog_path = env.home / ".codex" / "reverso" / "kimi.json"
    if mutation == "profile-model":
        profile_path.write_text(
            profile_path.read_text().replace("kimi-k3", "kimi-k2.5", 1)
        )
    elif mutation == "profile-provider":
        profile_path.write_text(
            profile_path.read_text().replace("reverso_kimi", "reverso_stale")
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
    done = script.index("Done. Reverso LaunchAgents installed.")

    assert pre_install < write < pre_restart < launchctl < post_restart < done
    assert str(CANONICAL_CHECKOUT) in script
    assert 'export REVERSO_UV_BIN="${UV_BIN}"' in script
    assert '"${UV_BIN}" run --project "${REVERSO_DIR}" python' in script
    assert '"${REVERSO_DIR}/scripts/check-deployment-drift.py"' in script


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
