"""LiteLLM proxy entrypoint.

Starts the LiteLLM proxy server with the reverso litellm_config.yaml.
Reads Keychain secrets at startup and injects them into the environment
before handing off to LiteLLM.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _resolve_config_path() -> Path:
    override = os.environ.get("REVERSO_CONFIG")
    if override:
        return Path(override).expanduser()
    # src/reverso/proxy/main.py -> ../../../ = reverso/
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "config"
        / "litellm_config.yaml"
    )


_CONFIG_PATH = _resolve_config_path()
_KEYCHAIN_KEYS = {
    "DEEPSEEK_API_KEY": "reverso/DEEPSEEK_API_KEY",
}


def _load_keychain_secret(service: str) -> str | None:
    """Read a secret from macOS Keychain using the `security` CLI."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def _inject_keychain_secrets() -> None:
    """Populate env vars from Keychain; warn if any are missing."""
    for env_var, service in _KEYCHAIN_KEYS.items():
        if os.environ.get(env_var):
            continue  # already set (e.g., from a test env)
        secret = _load_keychain_secret(service)
        if secret:
            os.environ[env_var] = secret
        else:
            print(
                f"WARNING: Keychain secret '{service}' not found. "
                f"Provider requiring {env_var} will return 503.",
                file=sys.stderr,
            )


def _resolve_host() -> str:
    """Return the only supported bind host for Reverso."""
    host = os.environ.get("REVERSO_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise ValueError(
            "REVERSO_HOST must be 127.0.0.1; non-loopback binds are forbidden"
        )
    return host


def main() -> None:
    """Start the LiteLLM proxy."""
    _inject_keychain_secrets()

    config_path = _CONFIG_PATH.resolve()
    if not config_path.exists():
        print(f"ERROR: litellm_config.yaml not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    # LiteLLM's proxy_server reads its config from CONFIG_FILE_PATH at import time.
    os.environ["CONFIG_FILE_PATH"] = str(config_path)
    try:
        import uvicorn

        from reverso.proxy.bootstrap import register_litellm_extensions

        register_litellm_extensions()

        host = _resolve_host()
        port = int(os.environ.get("REVERSO_PORT", "64946"))
        print(
            f"Starting reverso proxy on {host}:{port} (config: {config_path})",
            flush=True,
        )
        # Boot the composition root (ADR 0003): first-party provider prefixes
        # are served by the first-party gateway, everything else falls through
        # to the legacy LiteLLM app. Rollback is repointing this to
        # "reverso.proxy.app:app".
        uvicorn.run(
            "reverso.proxy.compose:app",
            host=host,
            port=port,
        )
    except ImportError as exc:
        print(f"ERROR: litellm not installed - run `uv sync`: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
