"""Tests for Claude Code settings repair."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reverso import claude_code_sync
from reverso.claude_code_sync import (
    LAUNCHER_MANAGED_MARKER,
    main,
    sync_claude_code_settings,
)
from reverso.client_sync_lock import acquire_client_sync_lock
from reverso.client_sync_mutations import PreparedStateChanged


def _write_settings(path: Path, settings: dict[str, object]) -> None:
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _read_settings(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_claude(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
printf '%s\\n' "$0" > "$REVERSO_TEST_CAPTURE"
env | sort >> "$REVERSO_TEST_CAPTURE"
printf '%s\\n' -- >> "$REVERSO_TEST_CAPTURE"
printf '%s\\n' "$@" >> "$REVERSO_TEST_CAPTURE"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_dry_run_does_not_acquire_client_sync_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_claude = tmp_path / "claude"
    _fake_claude(real_claude)

    def fail_lock(**_kwargs: object) -> object:
        raise AssertionError("dry-run must not acquire the writer lock")

    monkeypatch.setattr(claude_code_sync, "acquire_client_sync_lock", fail_lock)
    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=tmp_path / "bin",
        claude_executable=real_claude,
        dry_run=True,
    )
    assert result.changed is True


def test_sync_reuses_explicit_client_sync_lock_token(tmp_path: Path) -> None:
    real_claude = tmp_path / "claude"
    _fake_claude(real_claude)
    lock_path = tmp_path / "catalog-refresh.lock"
    with acquire_client_sync_lock(path=lock_path) as token:
        result = sync_claude_code_settings(
            tmp_path / "settings.json",
            launcher_dir=tmp_path / "bin",
            claude_executable=real_claude,
            lock_path=lock_path,
            lock_token=token,
        )
        assert token.released is False
    assert result.changed is True


@pytest.mark.parametrize("changed_source", ["settings", "launcher"])
def test_apply_prepared_rejects_source_change_before_any_write(
    tmp_path: Path,
    changed_source: str,
) -> None:
    settings_path = tmp_path / "settings.json"
    _write_settings(
        settings_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946",
                "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
            }
        },
    )
    claude = tmp_path / "claude"
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    prepared = claude_code_sync.prepare_sync(
        settings_path,
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    if changed_source == "settings":
        settings_path.write_text('{"owner": "changed"}\n', encoding="utf-8")
        changed_path = settings_path
    else:
        launcher_dir.mkdir()
        changed_path = launcher_dir / "claude-codex"
        changed_path.write_text("#!/bin/sh\n# owner changed\n", encoding="utf-8")

    lock_path = tmp_path / "catalog-refresh.lock"
    with (
        acquire_client_sync_lock(path=lock_path) as token,
        pytest.raises(PreparedStateChanged),
    ):
        claude_code_sync.apply_prepared(prepared, lock_token=token)

    assert changed_path.exists()
    assert not (launcher_dir / "claude-reverso").exists()
    assert not tuple(tmp_path.glob("settings.json.reverso.bak.*"))


def test_prepare_keeps_authoritative_settings_snapshot_when_owner_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    _write_settings(
        settings_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946",
                "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
            }
        },
    )
    original = settings_path.read_bytes()
    claude = tmp_path / "claude"
    _fake_claude(claude)
    original_resolve = claude_code_sync._resolve_claude_executable

    def change_owner_during_render(
        explicit: Path | None,
    ) -> tuple[Path | None, str | None]:
        settings_path.write_text('{"owner": "changed"}\n', encoding="utf-8")
        return original_resolve(explicit)

    monkeypatch.setattr(
        claude_code_sync,
        "_resolve_claude_executable",
        change_owner_during_render,
    )
    launcher_dir = tmp_path / "bin"
    prepared = claude_code_sync.prepare_sync(
        settings_path,
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    settings_mutation = next(
        mutation
        for mutation in prepared.group.mutations
        if mutation.path == settings_path
    )
    assert settings_mutation.before.data == original
    assert settings_path.read_text(encoding="utf-8") == '{"owner": "changed"}\n'

    with (
        acquire_client_sync_lock(path=tmp_path / "sync.lock") as token,
        pytest.raises(PreparedStateChanged),
    ):
        claude_code_sync.apply_prepared(prepared, lock_token=token)

    assert settings_path.read_text(encoding="utf-8") == '{"owner": "changed"}\n'
    assert not launcher_dir.exists()
    assert not tuple(tmp_path.glob("settings.json.reverso.bak.*"))


def test_sync_removes_reverso_global_overrides_and_preserves_stock_settings(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    _write_settings(
        settings_path,
        {
            "env": {
                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946",
                "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
                "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
            },
            "hooks": {"PreToolUse": []},
            "_reverso_prev_model": "claude-opus-4-8",
            "model": "haiku",
        },
    )

    claude = tmp_path / "claude"
    _fake_claude(claude)
    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.changed is True
    assert result.error is None
    assert result.removed_env_keys == (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_SMALL_FAST_MODEL",
    )
    assert result.removed_model == "haiku"
    assert result.backup_path is not None
    assert Path(result.backup_path).exists()
    assert _read_settings(settings_path) == {
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "hooks": {"PreToolUse": []},
    }


def test_sync_preserves_user_owned_global_credentials_and_unrelated_headers(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    settings: dict[str, object] = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "user-owned-auth-token",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5",
            "ANTHROPIC_API_KEY": "user-owned-api-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "user-owned-oauth-token",
            "ANTHROPIC_CUSTOM_HEADERS": "x-user-header: keep",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }
    }
    _write_settings(settings_path, settings)
    claude = tmp_path / "claude"
    _fake_claude(claude)

    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.error is None
    assert result.removed_env_keys == ()
    assert _read_settings(settings_path) == settings


def test_sync_removes_only_demonstrably_reverso_owned_global_settings(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    _write_settings(
        settings_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946",
                "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
                "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash",
                "ANTHROPIC_API_KEY": "user-owned-api-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "user-owned-oauth-token",
                "ANTHROPIC_CUSTOM_HEADERS": (
                    "x-user-header: keep\nx-reverso-model-catalog: all"
                ),
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            }
        },
    )
    claude = tmp_path / "claude"
    _fake_claude(claude)

    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.error is None
    assert result.removed_env_keys == (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    )
    assert result.rewritten_env_keys == ("ANTHROPIC_CUSTOM_HEADERS",)
    assert _read_settings(settings_path) == {
        "env": {
            "ANTHROPIC_API_KEY": "user-owned-api-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "user-owned-oauth-token",
            "ANTHROPIC_CUSTOM_HEADERS": "x-user-header: keep",
        }
    }


def test_sync_is_idempotent_when_no_reverso_keys_exist(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings = {"model": "sonnet", "env": {"PATH": "/usr/bin"}}
    _write_settings(settings_path, settings)

    claude = tmp_path / "claude"
    _fake_claude(claude)
    sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )
    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.changed is False
    assert result.backup_path is None
    assert result.removed_env_keys == ()
    assert _read_settings(settings_path) == settings
    assert not list(tmp_path.glob("settings.json.reverso.bak.*"))


def test_sync_dry_run_reports_changes_without_writing_backup(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings = {
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:64946"},
        "_reverso_prev_model": "claude-opus-4-8",
        "model": "haiku",
    }
    _write_settings(settings_path, settings)

    claude = tmp_path / "claude"
    _fake_claude(claude)
    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
        dry_run=True,
    )

    assert result.changed is True
    assert result.dry_run is True
    assert result.backup_path is None
    assert result.removed_model == "haiku"
    assert _read_settings(settings_path) == settings
    assert not list(tmp_path.glob("settings.json.reverso.bak.*"))


def test_sync_reports_invalid_json_without_overwriting(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    claude = tmp_path / "claude"
    _fake_claude(claude)
    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.changed is False
    assert result.error is not None
    assert result.error.startswith("invalid JSON:")
    assert settings_path.read_text(encoding="utf-8") == "{"


def test_sync_rejects_symlinked_settings_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "user-settings.json"
    settings = {
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:64946"},
        "_reverso_prev_model": "claude-opus-4-8",
        "model": "haiku",
    }
    _write_settings(target, settings)
    settings_path = tmp_path / "settings.json"
    settings_path.symlink_to(target)
    claude = tmp_path / "claude"
    _fake_claude(claude)

    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=tmp_path / "bin",
        claude_executable=claude,
    )

    assert result.changed is False
    assert result.error == f"settings path must not be a symlink: {settings_path}"
    assert settings_path.is_symlink()
    assert _read_settings(target) == settings
    assert not (tmp_path / "bin").exists()
    assert not list(tmp_path.glob("settings.json.reverso.bak.*"))


def test_backup_skips_symlink_collision_without_touching_target(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(b'{"model":"sonnet"}\n')
    now = datetime(2026, 7, 25, 10, 11, 12, tzinfo=UTC)
    collision = tmp_path / "settings.json.reverso.bak.20260725T101112Z"
    user_file = tmp_path / "user-file"
    user_file.write_bytes(b"user-owned\n")
    collision.symlink_to(user_file)

    backup = claude_code_sync._backup_settings(settings_path, now=now)

    assert backup == tmp_path / "settings.json.reverso.bak.20260725T101112Z.1"
    assert backup.read_bytes() == settings_path.read_bytes()
    assert collision.is_symlink()
    assert user_file.read_bytes() == b"user-owned\n"


def test_backup_uses_distinct_paths_within_same_second(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(b'{"model":"sonnet"}\n')
    now = datetime(2026, 7, 25, 10, 11, 12, tzinfo=UTC)

    first = claude_code_sync._backup_settings(settings_path, now=now)
    second = claude_code_sync._backup_settings(settings_path, now=now)

    assert first != second
    assert first.read_bytes() == settings_path.read_bytes()
    assert second.read_bytes() == settings_path.read_bytes()
    assert first.exists()
    assert second.exists()


def test_cli_returns_error_for_invalid_json(tmp_path: Path, capsys) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    exit_code = main(
        [
            "--settings-path",
            str(settings_path),
            "--launcher-dir",
            str(tmp_path / "bin"),
            "--claude-executable",
            str(tmp_path / "claude"),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["error"].startswith("invalid JSON:")


def test_sync_installs_provider_scoped_managed_launchers(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    claude = tmp_path / "real" / "claude"
    claude.parent.mkdir()
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"

    result = sync_claude_code_settings(
        settings_path,
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )

    expected = {
        "claude-reverso": "all",
        "claude-claude": "claude",
        "claude-codex": "codex",
        "claude-copilot": "copilot",
        "claude-auggie": "auggie",
        "claude-deepseek": "deepseek",
        "claude-kimi": "kimi",
        "claude-ollama": "ollama",
    }
    assert result.error is None
    assert result.changed_launchers == tuple(expected)
    for name, catalog in expected.items():
        launcher = launcher_dir / name
        text = launcher.read_text(encoding="utf-8")
        assert text.startswith(f"#!/bin/sh\n{LAUNCHER_MANAGED_MARKER}\n")
        assert str(claude.resolve()) in text
        assert "http://127.0.0.1:64946" in text
        assert "reverso-local-loopback" in text
        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" in text
        assert f"x-reverso-model-catalog: {catalog}" in text
        assert "x-reverso-workspace: $PWD" in text
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o755


def test_launcher_scrubs_auth_and_uses_process_local_settings(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "real" / "claude"
    claude.parent.mkdir()
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    assert result.error is None
    capture = tmp_path / "capture"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        **os.environ,
        "REVERSO_TEST_CAPTURE": str(capture),
        "ANTHROPIC_API_KEY": "secret-api-key",
        "ANTHROPIC_AUTH_TOKEN": "secret-bearer",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth",
        "ANTHROPIC_CUSTOM_HEADERS": "x-poisoned: yes",
    }

    completed = subprocess.run(
        [launcher_dir / "claude-codex", "--model", "gpt-5.5"],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    captured = capture.read_text(encoding="utf-8")
    captured_lines = captured.splitlines()
    assert captured.splitlines()[0] == str(claude.resolve())
    assert not any(line.startswith("ANTHROPIC_API_KEY=") for line in captured_lines)
    assert not any(
        line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in captured_lines
    )
    assert "ANTHROPIC_AUTH_TOKEN=secret-bearer" not in captured_lines
    assert "ANTHROPIC_CUSTOM_HEADERS=x-reverso-model-catalog: codex" in captured
    assert f"x-reverso-workspace: {workspace}" in captured
    args = captured.split("\n--\n", 1)[1].splitlines()
    assert args[0] == "--settings"
    settings = json.loads(args[1])
    assert settings["env"] == {
        "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    assert args[2:] == ["--model", "gpt-5.5"]


def test_kimi_launcher_sets_documented_one_million_token_context(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "real" / "claude"
    claude.parent.mkdir()
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    assert result.error is None
    capture = tmp_path / "capture"

    completed = subprocess.run(
        [launcher_dir / "claude-kimi", "--version"],
        env={
            **os.environ,
            "REVERSO_TEST_CAPTURE": str(capture),
            "ANTHROPIC_MODEL": "claude-opus-5",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "123",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "456",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    captured = capture.read_text(encoding="utf-8")
    captured_lines = captured.splitlines()
    assert "ANTHROPIC_MODEL=claude-opus-5" not in captured_lines
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=123" not in captured_lines
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS=456" not in captured_lines
    args = captured.split("\n--\n", 1)[1].splitlines()
    settings = json.loads(args[1])
    assert settings["env"]["ANTHROPIC_MODEL"] == "kimi-k3"
    assert settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1048576"
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1048576"


def test_non_kimi_launcher_preserves_caller_model_and_context(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "real" / "claude"
    claude.parent.mkdir()
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    assert result.error is None
    capture = tmp_path / "capture"

    completed = subprocess.run(
        [launcher_dir / "claude-codex", "--version"],
        env={
            **os.environ,
            "REVERSO_TEST_CAPTURE": str(capture),
            "ANTHROPIC_MODEL": "gpt-5.5",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "123",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "456",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    captured_lines = capture.read_text(encoding="utf-8").splitlines()
    assert "ANTHROPIC_MODEL=gpt-5.5" in captured_lines
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=123" in captured_lines
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS=456" in captured_lines


def test_launcher_rejects_caller_settings_flags(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    for args in (
        ["--settings", "{}"],
        ["--settings={}"],
        ["--setting-sources", "user"],
        ["--setting-sources=user"],
    ):
        capture = tmp_path / "capture"
        completed = subprocess.run(
            [launcher_dir / "claude-reverso", *args],
            env={**os.environ, "REVERSO_TEST_CAPTURE": str(capture)},
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 2
        assert "managed by reverso" in completed.stderr
        assert not capture.exists()


def test_sync_is_idempotent_and_dry_run_does_not_create_launchers(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude"
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"

    dry_run = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
        dry_run=True,
    )
    assert dry_run.changed is True
    assert dry_run.changed_launchers
    assert not launcher_dir.exists()

    first = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    second = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )
    assert first.changed is True
    assert second.changed is False
    assert second.changed_launchers == ()


def test_sync_preserves_and_reports_unmarked_launcher_conflict(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude"
    _fake_claude(claude)
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    conflict = launcher_dir / "claude-codex"
    conflict.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )

    assert result.changed is False
    assert result.conflicting_launchers == ("claude-codex",)
    assert result.error == "unmanaged launcher conflict: claude-codex"
    assert conflict.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert sorted(path.name for path in launcher_dir.iterdir()) == ["claude-codex"]


def test_sync_resolves_real_claude_outside_managed_launcher_dir(
    tmp_path: Path, monkeypatch
) -> None:
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    recursive = launcher_dir / "claude"
    _fake_claude(recursive)
    recursive.write_text(
        recursive.read_text(encoding="utf-8").replace(
            "#!/bin/sh\n", f"#!/bin/sh\n{LAUNCHER_MANAGED_MARKER}\n"
        ),
        encoding="utf-8",
    )
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "claude"
    _fake_claude(real)
    monkeypatch.setenv("PATH", f"{launcher_dir}{os.pathsep}{real_dir}")

    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
    )

    assert result.error is None
    assert result.claude_executable == str(real.resolve())
    assert str(real.resolve()) in (launcher_dir / "claude-reverso").read_text()
