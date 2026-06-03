"""Shared utilities for Reverso CLI providers."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterator

import httpx
import yaml

_DEFAULT_SOCK = "~/Library/Application Support/reverso/daemon.sock"
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"

_THINK_START = "<think>"
_THINK_END = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def daemon_sock_path() -> str:
    """Return the UDS socket path, preferring REVERSO_DAEMON_SOCK env var."""
    if env_sock := os.environ.get("REVERSO_DAEMON_SOCK"):
        return str(Path(env_sock).expanduser())
    try:
        if _CONFIG_PATH.exists():
            with _CONFIG_PATH.open() as fh:
                cfg = yaml.safe_load(fh) or {}
            return str(Path(cfg.get("daemon_socket", _DEFAULT_SOCK)).expanduser())
    except Exception:
        pass
    return str(Path(_DEFAULT_SOCK).expanduser())


def daemon_available(sock_path: str) -> bool:
    """Return True if the daemon socket file exists."""
    return Path(sock_path).exists()


def call_daemon(
    sock_path: str,
    workspace: str,
    provider: str,
    user_message: str,
    model: str,
    timeout: float = 300,
) -> dict[str, Any]:
    """POST /session/turn on the daemon over UDS. Raises httpx.ConnectError if unreachable."""
    transport = httpx.HTTPTransport(uds=sock_path)
    with httpx.Client(transport=transport, base_url="http://daemon") as client:
        resp = client.post(
            "/session/turn",
            json={"workspace": workspace, "provider": provider, "user_message": user_message, "model": model},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()



def stream_daemon(
    sock_path: str,
    workspace: str,
    provider: str,
    user_message: str,
    model: str,
    timeout: float = 300,
) -> Iterator[dict[str, Any]]:
    """POST /session/turn/stream on the daemon over UDS and yield NDJSON events."""
    transport = httpx.HTTPTransport(uds=sock_path)
    with httpx.Client(transport=transport, base_url="http://daemon", timeout=timeout) as client:
        with client.stream(
            "POST",
            "/session/turn/stream",
            json={"workspace": workspace, "provider": provider, "user_message": user_message, "model": model},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                yield json_loads(line)


def json_loads(value: str | bytes) -> dict[str, Any]:
    """Parse an NDJSON line from daemon streaming output."""
    import json

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("daemon stream event is not an object")
    return parsed

def strip_think_blocks(value: str) -> str:
    """Remove <think>...</think> blocks from assistant text."""
    if not isinstance(value, str):
        return value
    if _THINK_START not in value and _THINK_END not in value:
        return value
    value = _THINK_BLOCK_RE.sub("", value)
    start = value.find(_THINK_START)
    if start != -1:
        value = value[:start]
    end = value.find(_THINK_END)
    if end != -1:
        value = value[end + len(_THINK_END):]
    return value.lstrip()


def resolve_cli_command(name: str, env_var: str | None = None) -> str:
    """Resolve a CLI command under both shells and launchd."""
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            return configured
    if found := shutil.which(name):
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return name


def _longest_suffix_prefix(value: str, prefix: str) -> int:
    """Length of the longest suffix of *value* that is a prefix of *prefix*."""
    max_len = min(len(value), len(prefix) - 1)
    for size in range(max_len, 0, -1):
        if prefix.startswith(value[-size:]):
            return size
    return 0


class StreamingThinkStripper:
    """Incrementally strip <think>...</think> blocks from streaming text deltas."""

    def __init__(self) -> None:
        self.in_think = False
        self.pending = ""

    def strip_delta(self, value: str) -> str:
        if not isinstance(value, str) or (not value and not self.pending):
            return value

        text = self.pending + value
        self.pending = ""
        output: list[str] = []
        index = 0

        while index < len(text):
            if self.in_think:
                end = text.find(_THINK_END, index)
                if end == -1:
                    keep = _longest_suffix_prefix(text[index:], _THINK_END)
                    self.pending = text[len(text) - keep:] if keep else ""
                    return "".join(output)
                index = end + len(_THINK_END)
                self.in_think = False
                while index < len(text) and text[index] in " \t\r\n":
                    index += 1
                continue

            start = text.find(_THINK_START, index)
            stray_end = text.find(_THINK_END, index)
            if stray_end != -1 and (start == -1 or stray_end < start):
                index = stray_end + len(_THINK_END)
                while index < len(text) and text[index] in " \t\r\n":
                    index += 1
                continue
            if start == -1:
                tail = _longest_suffix_prefix(text[index:], _THINK_START)
                if tail:
                    output.append(text[index: len(text) - tail])
                    self.pending = text[len(text) - tail:]
                else:
                    output.append(text[index:])
                break

            output.append(text[index:start])
            index = start + len(_THINK_START)
            self.in_think = True

        return "".join(output)


def last_user_message(messages: list[dict[str, Any]]) -> str:
    """Return the text content of the last user message in a chat history."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
    return ""
