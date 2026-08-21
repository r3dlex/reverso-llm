"""G2 integration contracts for the managed Claude Ollama surface."""

from __future__ import annotations

from pathlib import Path

from reverso.claude_code_sync import sync_claude_code_settings


def test_sync_installs_marker_owned_ollama_launcher(tmp_path: Path) -> None:
    claude = tmp_path / "real" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    claude.chmod(0o755)
    launcher_dir = tmp_path / "bin"

    result = sync_claude_code_settings(
        tmp_path / "settings.json",
        launcher_dir=launcher_dir,
        claude_executable=claude,
    )

    launcher = launcher_dir / "claude-ollama"
    assert result.error is None
    assert "claude-ollama" in result.changed_launchers
    assert launcher.stat().st_mode & 0o777 == 0o755
    text = launcher.read_text(encoding="utf-8")
    assert "# Managed by reverso-claude-code-sync." in text
    assert "ANTHROPIC_BASE_URL" in text
    assert "x-reverso-model-catalog: ollama" in text
    assert "x-reverso-workspace: $PWD" in text


def test_manifest_registers_claude_ollama_surface() -> None:
    import json

    manifest = json.loads(
        Path("config/supported-client-surfaces.json").read_text(encoding="utf-8")
    )
    assert manifest["claude_launchers"]["claude-ollama"] == "ollama"
    surface = next(row for row in manifest["surfaces"] if row["id"] == "claude-ollama")
    assert surface["group"] == "provider-ollama"
    assert surface["selector_template"] == "anthropic-ollama-<raw-model-id>"
