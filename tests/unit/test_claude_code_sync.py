"""Tests for Claude Code settings repair."""

from __future__ import annotations

import json
from pathlib import Path

from reverso.claude_code_sync import main, sync_claude_code_settings


def _write_settings(path: Path, settings: dict[str, object]) -> None:
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _read_settings(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


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

    result = sync_claude_code_settings(settings_path)

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


def test_sync_is_idempotent_when_no_reverso_keys_exist(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings = {"model": "sonnet", "env": {"PATH": "/usr/bin"}}
    _write_settings(settings_path, settings)

    result = sync_claude_code_settings(settings_path)

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

    result = sync_claude_code_settings(settings_path, dry_run=True)

    assert result.changed is True
    assert result.dry_run is True
    assert result.backup_path is None
    assert result.removed_model == "haiku"
    assert _read_settings(settings_path) == settings
    assert not list(tmp_path.glob("settings.json.reverso.bak.*"))


def test_sync_reports_invalid_json_without_overwriting(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    result = sync_claude_code_settings(settings_path)

    assert result.changed is False
    assert result.error is not None
    assert result.error.startswith("invalid JSON:")
    assert settings_path.read_text(encoding="utf-8") == "{"


def test_cli_returns_error_for_invalid_json(tmp_path: Path, capsys) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{", encoding="utf-8")

    exit_code = main(["--settings-path", str(settings_path)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["error"].startswith("invalid JSON:")
