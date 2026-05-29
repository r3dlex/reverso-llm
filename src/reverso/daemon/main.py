"""Session daemon entrypoint (Phase 2).

Creates the Unix-domain socket path, starts the FastAPI app via uvicorn,
and launches the RecycleSweeper background task.

The UDS path is read from config.yaml (daemon_socket key).  It defaults to
~/Library/Application Support/reverso/daemon.sock.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SOCK_PATH = "~/Library/Application Support/reverso/daemon.sock"
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"


def _load_sock_path() -> str:
    """Return the UDS socket path from config.yaml or the env var override."""
    # Env var takes priority (useful for tests and local dev overrides).
    if env_sock := os.environ.get("REVERSO_DAEMON_SOCK"):
        return str(Path(env_sock).expanduser())

    try:
        if _CONFIG_PATH.exists():
            with _CONFIG_PATH.open() as fh:
                cfg = yaml.safe_load(fh) or {}
            sock = cfg.get("daemon_socket", _DEFAULT_SOCK_PATH)
            return str(Path(sock).expanduser())
    except Exception as exc:
        logger.warning("Could not read config.yaml (%s); using default sock path", exc)

    return str(Path(_DEFAULT_SOCK_PATH).expanduser())


async def _run_daemon(sock_path: str) -> None:
    """Start the FastAPI app on a UDS and run the recycle sweeper."""
    # Import here so module-level table is shared with the FastAPI app.
    from reverso.daemon.session_daemon import app, _session_table
    from reverso.daemon.recycler import RecycleSweeper

    sock_file = Path(sock_path)
    sock_file.parent.mkdir(parents=True, exist_ok=True)

    # Remove stale socket file from a previous run if present.
    if sock_file.exists():
        sock_file.unlink()
        logger.info("Removed stale socket file: %s", sock_path)

    # Start the recycle sweeper as a background task.
    sweeper = RecycleSweeper(_session_table)
    sweep_task = asyncio.create_task(sweeper.run(), name="recycle-sweeper")

    logger.info("reverso-daemon starting on UDS: %s", sock_path)

    config = uvicorn.Config(
        app=app,
        uds=sock_path,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        logger.info("reverso-daemon stopped")


def main() -> None:
    """CLI entrypoint registered in pyproject.toml as reverso-daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    sock_path = _load_sock_path()
    logger.info("Socket path: %s", sock_path)

    try:
        asyncio.run(_run_daemon(sock_path))
    except KeyboardInterrupt:
        logger.info("reverso-daemon interrupted, exiting")


if __name__ == "__main__":
    main()
