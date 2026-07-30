"""Contract tests for the complete client convergence runbook."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CANONICAL_SEQUENCE = """uv sync --frozen
./scripts/install-launchagents.sh
uv run python scripts/check-deployment-drift.py --phase pre-sync
uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/smoke.sh
./scripts/convergence-acceptance.sh
uv run python scripts/check-deployment-drift.py --phase acceptance"""

SENSITIVE_DIAGNOSTICS = (
    ("Authorization bearer auth-do-not-print", "auth-do-not-print"),
    ("Bearer bearer-do-not-print", "bearer-do-not-print"),
    ("API key: api-do-not-print", "api-do-not-print"),
    ("https://user:url-do-not-print@example.invalid/path", "url-do-not-print"),
    ("token is token-do-not-print", "token-do-not-print"),
)


def test_agent_and_operator_guides_publish_the_same_canonical_sequence() -> None:
    for path in (Path("README.md"), Path("AGENTS.md")):
        assert CANONICAL_SEQUENCE in path.read_text(encoding="utf-8")


def test_acceptance_script_proves_the_complete_isolated_contract() -> None:
    script = Path("scripts/convergence-acceptance.sh").read_text(encoding="utf-8")
    manifest = json.loads(
        Path("config/supported-client-surfaces.json").read_text(encoding="utf-8")
    )

    assert len({surface["id"] for surface in manifest["surfaces"]}) == 17
    assert 'expected_surfaces = {item["id"] for item in manifest["surfaces"]}' in script
    assert script.index("run_sync dry-run") < script.index("run_sync apply")
    assert script.count("run_sync apply") == 2
    assert script.index("run_sync refresh") < script.index("run_sync verify")
    assert 'mktemp -d "${HOST_HOME}/.reverso-convergence.XXXXXX"' in script
    assert 'RESULTS_DIR="${ACCEPTANCE_ROOT}/results"' in script
    assert 'FIRST_DIGEST="$(state_digest)"' in script
    assert 'SECOND_DIGEST="$(state_digest)"' in script
    assert 'SECOND_APPLY_JSON="${RESULTS_DIR}/second-apply.json"' in script
    assert '--rtk-bin "${RTK_BIN}"' in script
    assert not re.search(r'(^|[;&|]\\s*)"?\\$\\{?RTK_BIN\\}?"?(\\s|$)', script)
    assert "http://127.0.0.1:64946/usage/headroom" not in script
    assert "HEADROOM_USAGE_URL" in script
    assert '"headroom_schema_version": 2' in script
    assert '"headroom_profile": expected_profile' in script
    assert "REVERSO_ACCEPTANCE_BASE_COMMIT" not in script
    assert "status --porcelain --untracked-files=all" in script
    assert "refs/remotes/origin/main" in script
    assert '"codex_profiles_executed": codex_profile_count' in script
    assert '"claude_launchers_executed": claude_launcher_count' in script
    assert '"long_lived_service_count": 2' in script
    assert '"catalog_refresh_schedule": ["06:00", "18:00"]' in script
    assert '"frozen_adapter": "unchanged"' in script
    assert set(re.findall(r'"[a-z_]+":', script[script.index("json.dumps(") :])) >= {
        '"schema_version":',
        '"status":',
        '"commit":',
        '"surface_count":',
        '"long_lived_service_count":',
        '"catalog_refresh_schedule":',
        '"headroom_endpoint":',
        '"headroom_schema_version":',
        '"headroom_profile":',
        '"codex_profiles_executed":',
        '"claude_launchers_executed":',
        '"rtk_discovered_without_execution":',
        '"second_apply":',
        '"frozen_adapter":',
    }


def test_smoke_and_live_matrix_use_manifest_and_fixed_reverso_endpoint() -> None:
    smoke = Path("scripts/smoke.sh").read_text(encoding="utf-8")
    matrix = Path("scripts/codex-e2e-matrix.sh").read_text(encoding="utf-8")

    assert 'BASE="http://127.0.0.1:64946"' in smoke
    assert "validate_headroom_usage_payload" in smoke
    assert 'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' in matrix
    assert "supported-client-surfaces.json" in matrix
    assert 'expected = {surface["id"] for surface in manifest["surfaces"]}' in matrix
    assert 'actual = {surface["id"] for surface in result["surfaces"]}' in matrix
    assert 'evidence_status="failed"' in matrix
    assert "return 1" in matrix


def _run_surface_inventory_matrix(
    tmp_path: Path, surface_ids: list[str]
) -> subprocess.CompletedProcess[str]:
    repo_root = Path.cwd().resolve()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    payload_path = tmp_path / "verify.json"
    payload_path.write_text(
        json.dumps({"surfaces": [{"id": surface_id} for surface_id in surface_ids]}),
        encoding="utf-8",
    )
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env bash
set -eu
[ "$PWD" = "$EXPECTED_REPO_ROOT" ]
cat "$FAKE_VERIFY_PAYLOAD"
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "EXPECTED_REPO_ROOT": str(repo_root),
            "FAKE_VERIFY_PAYLOAD": str(payload_path),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "REVERSO_E2E_EVIDENCE_FILE": str(tmp_path / "evidence.md"),
        }
    )
    return subprocess.run(
        ["bash", "scripts/codex-e2e-matrix.sh", "--surface-inventory-only"],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_live_matrix_inventory_resolves_checkout_and_passes_exact_manifest(
    tmp_path: Path,
) -> None:
    manifest = json.loads(
        Path("config/supported-client-surfaces.json").read_text(encoding="utf-8")
    )
    result = _run_surface_inventory_matrix(
        tmp_path,
        [surface["id"] for surface in manifest["surfaces"]],
    )

    assert result.returncode == 0, result.stderr
    assert "surface_inventory" in result.stdout
    assert "PASS" in result.stdout
    assert "status: complete" in (tmp_path / "evidence.md").read_text(encoding="utf-8")


def test_live_matrix_inventory_mismatch_fails_process_and_evidence(
    tmp_path: Path,
) -> None:
    manifest = json.loads(
        Path("config/supported-client-surfaces.json").read_text(encoding="utf-8")
    )
    result = _run_surface_inventory_matrix(
        tmp_path,
        [surface["id"] for surface in manifest["surfaces"][:-1]],
    )

    assert result.returncode == 1
    assert "surface_inventory" in result.stdout
    assert "FAIL" in result.stdout
    assert "matrix failed with 1 failing cell(s)" in result.stderr
    assert "status: failed" in (tmp_path / "evidence.md").read_text(encoding="utf-8")


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _acceptance_fixture(
    tmp_path: Path,
    *,
    headroom_profile: str = "coding",
) -> tuple[Path, dict[str, str], Path]:
    source_root = Path.cwd().resolve()
    repo = tmp_path / "repo"
    tools = tmp_path / "tools"
    host_home = tmp_path / "host-home"
    repo.mkdir()
    tools.mkdir()
    host_home.mkdir()

    (repo / "scripts").mkdir()
    shutil.copy2(
        source_root / "scripts/convergence-acceptance.sh",
        repo / "scripts/convergence-acceptance.sh",
    )
    (repo / "config").mkdir()
    shutil.copy2(
        source_root / "config/supported-client-surfaces.json",
        repo / "config/supported-client-surfaces.json",
    )
    (repo / "launchd").mkdir()
    for name in (
        "com.user.reverso-proxy.plist.tmpl",
        "com.user.reverso-daemon.plist.tmpl",
        "com.user.reverso-catalog-refresh.plist.tmpl",
    ):
        shutil.copy2(source_root / "launchd" / name, repo / "launchd" / name)
    adapter = repo / "src/reverso/protocols/adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("# frozen adapter\n", encoding="utf-8")

    headroom_payload = {
        "schema_version": 1,
        "provider": "headroom",
        "headroom": {"schema_version": 2, "profile": headroom_profile},
    }
    headroom_path = repo / "headroom.json"
    headroom_path.write_text(json.dumps(headroom_payload), encoding="utf-8")
    package = repo / "reverso"
    (package / "protocols").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "protocols/__init__.py").write_text("", encoding="utf-8")
    (package / "deployment_drift.py").write_text(
        textwrap.dedent(
            f"""\
            HEADROOM_USAGE_URL = {headroom_path.as_uri()!r}
            SCHEDULED_LAUNCH_AGENT_LABEL = "com.user.reverso-catalog-refresh"
            SCHEDULED_START_CALENDAR_INTERVAL = [
                {{"Hour": 6, "Minute": 0}},
                {{"Hour": 18, "Minute": 0}},
            ]

            def validate_headroom_usage_payload(payload, *, expected_profile=None):
                import os
                if diagnostic := os.environ.get("FAKE_VALIDATION_DIAGNOSTIC"):
                    raise ValueError(diagnostic)
                if payload["headroom"]["profile"] != expected_profile:
                    raise ValueError("profile")
            """
        ),
        encoding="utf-8",
    )
    (package / "protocols/headroom_compression.py").write_text(
        textwrap.dedent(
            """\
            import os

            class HeadroomCompressionConfig:
                def __init__(self, profile):
                    self.profile = profile

                @classmethod
                def from_env(cls):
                    return cls(os.environ.get("REVERSO_HEADROOM_PROFILE", "").strip() or "coding")
            """
        ),
        encoding="utf-8",
    )

    codex_log = tmp_path / "codex.log"
    claude_log = tmp_path / "claude.log"
    uv_log = tmp_path / "uv.log"
    fake_codex = _write_executable(
        tools / "codex",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import pathlib
            import sys

            config = pathlib.Path(os.environ["CODEX_HOME"]) / "config.toml"
            if not config.is_file():
                raise SystemExit(9)
            with open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8") as handle:
                handle.write(f"{{os.environ['CODEX_HOME']}}|{{' '.join(sys.argv[1:])}}\\n")
            model = None
            for index, argument in enumerate(sys.argv):
                if argument == "-c" and sys.argv[index + 1].startswith("model="):
                    model = json.loads(sys.argv[index + 1].partition("=")[2])
            if sys.argv[1:3] != ["debug", "models"] or model is None:
                raise SystemExit(9)
            print(json.dumps({{"models": [{{"slug": model}}]}}))
            """
        ),
    )
    fake_claude = _write_executable(
        tools / "claude",
        """#!/usr/bin/env bash
set -eu
printf '%s|%s\\n' "${REVERSO_TEST_LAUNCHER:-unknown}" "$*" >>"${FAKE_CLAUDE_LOG}"
printf '%s\\n' "claude test"
""",
    )
    fake_rtk = _write_executable(
        tools / "rtk",
        """#!/usr/bin/env bash
echo "RTK must not execute" >&2
exit 99
""",
    )
    fake_uv = _write_executable(
        tools / "uv",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import pathlib
            import shlex
            import sys

            args = sys.argv[1:]
            if "python" in args:
                index = args.index("python")
                env = os.environ.copy()
                env["PYTHONPATH"] = os.environ["FAKE_REPO"]
                os.execve({sys.executable!r}, [{sys.executable!r}, *args[index + 1:]], env)

            mode = args[args.index("reverso-client-sync") + 1]
            with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as handle:
                handle.write(mode + "\\n")
            if os.environ.get("FAKE_SYNC_FAIL_MODE") == mode:
                print(os.environ["FAKE_SYNC_DIAGNOSTIC"], file=sys.stderr)
                raise SystemExit(9)

            def option(name):
                return pathlib.Path(args[args.index(name) + 1])

            home = pathlib.Path(os.environ["HOME"])
            codex_config = option("--codex-config")
            claude_config = option("--claude-config-dir")
            catalog_dir = option("--catalog-dir")
            launcher_dir = option("--launch-agent-dir")
            rtk = option("--rtk-bin")
            manifest = json.loads(
                (pathlib.Path(os.environ["FAKE_REPO"]) / "config/supported-client-surfaces.json").read_text()
            )
            marker = home / ".applied"
            status = "planned" if mode == "dry-run" else "success"
            if mode == "apply":
                status = "no_op" if marker.exists() else "success"
                if not marker.exists():
                    codex_config.parent.mkdir(parents=True, exist_ok=True)
                    codex_config.write_text("# base\\n")
                    catalog_dir.mkdir(parents=True, exist_ok=True)
                    for surface in manifest["surfaces"]:
                        if surface["kind"] != "reverso_route":
                            continue
                        path = pathlib.Path(
                            surface["path_template"].replace(
                                "<codex_config_dir>", str(codex_config.parent)
                            )
                        )
                        catalog = catalog_dir / f"{{surface['id']}}.json"
                        catalog.write_text(
                            json.dumps({{"models": [{{"slug": surface["id"]}}]}})
                        )
                        path.write_text(
                            f'model = "{{surface["id"]}}"\\n'
                            'model_provider = "reverso_test"\\n'
                            f'model_catalog_json = "{{catalog}}"\\n'
                        )
                    claude_config.mkdir(parents=True, exist_ok=True)
                    (claude_config / "settings.json").write_text("{{}}")
                    launcher_dir.mkdir(parents=True, exist_ok=True)
                    for launcher in manifest["claude_launchers"]:
                        path = launcher_dir / launcher
                        path.write_text(
                            "#!/usr/bin/env bash\\n"
                            f"REVERSO_TEST_LAUNCHER={{shlex.quote(launcher)}} "
                            f"exec {{shlex.quote(os.environ['FAKE_CLAUDE'])}} \\"$@\\"\\n"
                        )
                        path.chmod(0o755)
                    link = home / ".headroom/bin/rtk"
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(rtk)
                    marker.write_text("applied\\n")
            refresh = {{
                "last_attempt_at": "2026-07-30T10:00:00+00:00",
                "last_success_at": "2026-07-30T10:00:00+00:00",
                "stored_stale": False,
                "stored_stale_observed_at": None,
                "stale": False,
                "observed_at": "2026-07-30T10:00:00+00:00",
            }}
            payload = {{
                "status": status,
                "surfaces": [{{"id": surface["id"]}} for surface in manifest["surfaces"]],
                "catalog_refresh": refresh,
            }}
            print(json.dumps(payload))
            """
        ),
    )

    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Acceptance Test")
    _git(repo, "config", "user.email", "acceptance@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(host_home),
            "FAKE_CLAUDE": str(fake_claude),
            "FAKE_CLAUDE_LOG": str(claude_log),
            "FAKE_CODEX_LOG": str(codex_log),
            "FAKE_REPO": str(repo),
            "FAKE_SYNC_DIAGNOSTIC": "safe catalog diagnostic",
            "FAKE_UV_LOG": str(uv_log),
            "REVERSO_ACCEPTANCE_CODEX_BIN": str(fake_codex),
            "REVERSO_ACCEPTANCE_RTK_BIN": str(fake_rtk),
            "REVERSO_HEADROOM_PROFILE": headroom_profile,
            "REVERSO_UV_BIN": str(fake_uv),
        }
    )
    return repo, env, uv_log


def _run_acceptance(
    repo: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/convergence-acceptance.sh"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_acceptance_executes_generated_clients_with_configured_profile(
    tmp_path: Path,
) -> None:
    repo, env, _ = _acceptance_fixture(tmp_path, headroom_profile="agent-90")

    result = _run_acceptance(repo, env)

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["headroom_profile"] == "agent-90"
    assert evidence["codex_profiles_executed"] == 5
    assert evidence["claude_launchers_executed"] == 7
    codex_calls = (tmp_path / "codex.log").read_text(encoding="utf-8").splitlines()
    claude_calls = (tmp_path / "claude.log").read_text(encoding="utf-8").splitlines()
    assert len(codex_calls) == 5
    assert all("|debug models " in call for call in codex_calls)
    assert all("-c model=" in call for call in codex_calls)
    assert all("-c model_provider=" in call for call in codex_calls)
    assert all("-c model_catalog_json=" in call for call in codex_calls)
    assert {call.partition("|")[0] for call in claude_calls} == {
        "claude-reverso",
        "claude-claude",
        "claude-codex",
        "claude-copilot",
        "claude-auggie",
        "claude-deepseek",
        "claude-kimi",
    }
    assert all(call.endswith("|--version") for call in claude_calls)


def test_acceptance_preserves_safe_sync_failure_diagnostic(tmp_path: Path) -> None:
    repo, env, _ = _acceptance_fixture(tmp_path)
    env["FAKE_SYNC_FAIL_MODE"] = "dry-run"
    env["FAKE_SYNC_DIAGNOSTIC"] = "provider catalog unavailable"

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "dry-run failed" in result.stderr
    assert "provider catalog unavailable" in result.stderr


@pytest.mark.parametrize(("diagnostic", "secret"), SENSITIVE_DIAGNOSTICS)
def test_acceptance_redacts_secret_like_sync_failure_diagnostic(
    tmp_path: Path,
    diagnostic: str,
    secret: str,
) -> None:
    repo, env, _ = _acceptance_fixture(tmp_path)
    env["FAKE_SYNC_FAIL_MODE"] = "dry-run"
    env["FAKE_SYNC_DIAGNOSTIC"] = diagnostic

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "[redacted sensitive child diagnostic]" in result.stderr
    assert secret not in result.stderr


def test_acceptance_preserves_safe_validation_failure_diagnostic(
    tmp_path: Path,
) -> None:
    repo, env, _ = _acceptance_fixture(tmp_path)
    env["FAKE_VALIDATION_DIAGNOSTIC"] = "generated client catalog mismatch"

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "ValueError: generated client catalog mismatch" in result.stderr


@pytest.mark.parametrize(("diagnostic", "secret"), SENSITIVE_DIAGNOSTICS)
def test_acceptance_redacts_secret_like_validation_failure_diagnostic(
    tmp_path: Path,
    diagnostic: str,
    secret: str,
) -> None:
    repo, env, _ = _acceptance_fixture(tmp_path)
    env["FAKE_VALIDATION_DIAGNOSTIC"] = diagnostic

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "[redacted sensitive child diagnostic]" in result.stderr
    assert secret not in result.stderr


def test_acceptance_rejects_dirty_checkout_before_sync(tmp_path: Path) -> None:
    repo, env, uv_log = _acceptance_fixture(tmp_path)
    (repo / "headroom.json").write_text("{}\n", encoding="utf-8")

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "checkout must be clean" in result.stderr
    assert not uv_log.exists()


def test_acceptance_rejects_adapter_change_from_trusted_merge_base(
    tmp_path: Path,
) -> None:
    repo, env, uv_log = _acceptance_fixture(tmp_path)
    adapter = repo / "src/reverso/protocols/adapter.py"
    adapter.write_text("# changed adapter\n", encoding="utf-8")
    _git(repo, "add", str(adapter.relative_to(repo)))
    _git(repo, "commit", "-qm", "mutate adapter")
    env["REVERSO_ACCEPTANCE_BASE_COMMIT"] = _git(repo, "rev-parse", "HEAD")

    result = _run_acceptance(repo, env)

    assert result.returncode == 2
    assert "frozen adapter changed" in result.stderr
    assert not uv_log.exists()


def test_scheduler_and_long_lived_service_contracts_are_explicit() -> None:
    installer = Path("scripts/install-launchagents.sh").read_text(encoding="utf-8")
    scheduled = Path("launchd/com.user.reverso-catalog-refresh.plist.tmpl").read_text(
        encoding="utf-8"
    )

    assert "<key>KeepAlive</key>" not in scheduled
    assert scheduled.count("<key>Hour</key>") == 2
    assert "<integer>6</integer>" in scheduled
    assert "<integer>18</integer>" in scheduled
    long_lived_block = installer[
        installer.index("LONG_LIVED_AGENTS=(") : installer.index('SCHEDULED_AGENT="')
    ]
    assert long_lived_block.count("com.user.reverso-") == 2
    assert "com.user.reverso-proxy" in long_lived_block
    assert "com.user.reverso-daemon" in long_lived_block


def test_usage_contract_publishes_the_headroom_v2_schema() -> None:
    contract = Path("docs/specifications/ACTIVE/reverso-usage-contract.md").read_text(
        encoding="utf-8"
    )
    fixture = json.loads(
        Path(
            "tests/fixtures/client_convergence/headroom_usage_v2_contract.json"
        ).read_text(encoding="utf-8")
    )

    assert '"schema_version": 2' in contract
    for field in fixture["preserved_fields"] + fixture["additive_fields"]:
        assert f'"{field}"' in contract
    for mapping, keys in fixture["maps"].items():
        assert f'"{mapping}"' in contract
        for key in keys:
            assert f'"{key}"' in contract
    for formula in fixture["formulas"]:
        assert f"`{formula}`" in contract

    examples = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", contract, re.DOTALL)
    ]
    assert examples[0]["headroom"] == examples[2]["headroom"]
