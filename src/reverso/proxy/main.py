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


_CONFIG_PATH = Path(__file__).parent.parent.parent.parent.parent / "config" / "litellm_config.yaml"
_KEYCHAIN_KEYS = {
    "DEEPSEEK_API_KEY": "reverso/DEEPSEEK_API_KEY",
    "MINIMAX_API_KEY": "reverso/MINIMAX_API_KEY",
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


def main() -> None:
    """Start the LiteLLM proxy."""
    _inject_keychain_secrets()

    config_path = _CONFIG_PATH.resolve()
    if not config_path.exists():
        print(f"ERROR: litellm_config.yaml not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    # Import litellm here so the env vars are already set before litellm
    # initialises its providers.
    try:
        from litellm.proxy.proxy_server import app  # noqa: F401
        import uvicorn

        host = os.environ.get("REVERSO_HOST", "127.0.0.1")
        port = int(os.environ.get("REVERSO_PORT", "4000"))
        print(f"Starting reverso proxy on {host}:{port} (config: {config_path})", flush=True)
        uvicorn.run(
            "litellm.proxy.proxy_server:app",
            host=host,
            port=port,
            config=str(config_path),
        )
    except ImportError as exc:
        print(f"ERROR: litellm not installed – run `uv sync`: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
