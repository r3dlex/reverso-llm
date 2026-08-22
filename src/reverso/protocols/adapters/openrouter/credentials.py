"""Call-time credential resolution for the OpenRouter provider (OR-G1, U6).

The OpenRouter API key is sourced from ``$HOME/.zsh_exports`` only, never from
the process environment or command arguments. Resolution happens at call time
so a removed key surfaces as a bounded auth error and never falls back to a
different provider.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["OpenRouterCredentialError", "resolve_api_key"]


class OpenRouterCredentialError(RuntimeError):
    """The OpenRouter API key is missing or unreadable."""


_KEY_NAME = "OPENROUTER_API_KEY"
_ZSH_EXPORTS_NAME = ".zsh_exports"
_KEY_PATTERN = re.compile(r"^export OPENROUTER_API_KEY=(.+)$")


def resolve_api_key(*, home: Path | None = None) -> str:
    """Return the OpenRouter API key sourced from ``$HOME/.zsh_exports``.

    The key is read once per call from the user's zsh exports file. The value
    is returned to the caller verbatim and never persisted, logged, or copied
    into generated artifacts, status JSON, or telemetry.
    """
    base = home or Path.home()
    exports = base / _ZSH_EXPORTS_NAME
    if not exports.is_file():
        raise OpenRouterCredentialError(
            f"OPENROUTER_API_KEY not found in {exports}"
        )
    for line in exports.read_text(encoding="utf-8").splitlines():
        match = _KEY_PATTERN.match(line.strip())
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if value:
                return value
    raise OpenRouterCredentialError(
        f"OPENROUTER_API_KEY not found in {exports}"
    )
