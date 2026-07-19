"""Offline contract tests for the provider-pinned Claude Code Kimi launcher."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


LAUNCHER = Path("scripts/claude-kimi.sh")


def _fake_claude(tmp_path: Path) -> Path:
    fake = tmp_path / "claude"
    fake.write_text(
        """#!/bin/sh
python3 - "$@" <<'PY'
import json
import os
import sys

print(json.dumps({
    "argv": sys.argv[1:],
    "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
    "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN"),
    "discovery": os.environ.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"),
    "headers": os.environ.get("ANTHROPIC_CUSTOM_HEADERS"),
}))
PY
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_launcher_constructs_provider_pinned_kimi_messages_contract(
    tmp_path: Path,
) -> None:
    fake = _fake_claude(tmp_path)
    env = os.environ.copy()
    env["CLAUDE_BIN"] = str(fake)

    result = subprocess.run(
        [str(LAUNCHER), "--print", "hello"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["base_url"] == "http://127.0.0.1:64946/kimi"
    assert f'{payload["base_url"]}/v1/messages' == (
        "http://127.0.0.1:64946/kimi/v1/messages"
    )
    assert payload["auth_token"] == "reverso-local-loopback"
    assert payload["discovery"] == "1"
    assert payload["headers"] == f"x-reverso-workspace: {Path.cwd()}"
    assert payload["argv"] == ["--model", "kimi-k2.5", "--print", "hello"]


def test_launcher_accepts_only_a_bare_kimi_model(tmp_path: Path) -> None:
    fake = _fake_claude(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_BIN": str(fake),
            "REVERSO_KIMI_MODEL": "kimi/kimi-k2.5",
        }
    )

    result = subprocess.run(
        [str(LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "bare Kimi model id" in result.stderr
    assert result.stdout == ""


def test_launcher_model_override_remains_bare(tmp_path: Path) -> None:
    fake = _fake_claude(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_BIN": str(fake),
            "REVERSO_KIMI_MODEL": "kimi-k2-thinking",
        }
    )

    result = subprocess.run(
        [str(LAUNCHER)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert json.loads(result.stdout)["argv"][:2] == [
        "--model",
        "kimi-k2-thinking",
    ]
