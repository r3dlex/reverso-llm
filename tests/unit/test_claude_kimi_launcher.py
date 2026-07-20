"""Offline contract tests for the provider-pinned Claude Code Kimi launcher."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


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
    "api_key": os.environ.get("ANTHROPIC_API_KEY"),
    "oauth_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
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
    env["ANTHROPIC_API_KEY"] = "anthropic-secret-must-not-reach-loopback"
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "claude-oauth-must-not-reach-loopback"

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
    assert payload["api_key"] is None
    assert payload["oauth_token"] is None
    assert payload["discovery"] == "1"
    assert payload["headers"] == f"x-reverso-workspace: {Path.cwd()}"
    assert payload["argv"][:3] == ["--settings", payload["argv"][1], "--model"]
    pinned_settings = json.loads(payload["argv"][1])
    assert pinned_settings == {
        "env": {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "reverso-local-loopback",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:64946/kimi",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
        }
    }
    assert payload["argv"][2:] == [
        "--model",
        "kimi-for-coding",
        "--print",
        "hello",
    ]


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

    assert json.loads(result.stdout)["argv"][2:4] == [
        "--model",
        "kimi-k2-thinking",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "kimi/qualified"),
        ("--model=anthropic-kimi-kimi-k2.5",),
        ("--model", "gpt-5.5"),
        ("--", "--model", "kimi/qualified"),
        ("--fallback-model", "gpt-5.5"),
        ("--fallback-model=gpt-5.5",),
        ("--settings", "{}"),
        ("--settings={}",),
        ("--setting-sources", "user"),
        ("--setting-sources=user",),
    ],
)
def test_launcher_rejects_forwarded_model_overrides(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fake = _fake_claude(tmp_path)
    env = os.environ.copy()
    env["CLAUDE_BIN"] = str(fake)

    result = subprocess.run(
        [str(LAUNCHER), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "launcher-owned options are not accepted" in result.stderr
    assert result.stdout == ""
