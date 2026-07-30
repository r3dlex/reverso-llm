"""Bounded scheduled runner for unified client catalog refresh."""

from __future__ import annotations

import json
import os
import signal
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from reverso import client_sync
from reverso.client_sync_lock import (
    DEFAULT_CLIENT_SYNC_LOCK_PATH,
    ClientSyncLockBusy,
    acquire_client_sync_lock,
)

OVERALL_TIMEOUT_SECONDS = 120.0
LOG_MAX_BYTES = 1_048_576
LOG_ROTATIONS = 3

DEFAULT_STATUS_PATH = client_sync.DEFAULT_CATALOG_REFRESH_STATUS_PATH
DEFAULT_STDOUT_PATH = (
    Path.home() / "Library" / "Logs" / "reverso" / "catalog-refresh.stdout.log"
)
DEFAULT_STDERR_PATH = (
    Path.home() / "Library" / "Logs" / "reverso" / "catalog-refresh.stderr.log"
)


class CatalogRefreshTimeout(BaseException):
    """The scheduled refresh exceeded its complete execution bound."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _secure_directory(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"refresh directory must be real: {current}")
    os.chmod(absolute, 0o700)
    return absolute


def _require_regular_or_absent(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"refresh log must be a regular file: {path}")


def _rotation_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate(path: Path) -> None:
    for candidate in (path, *(_rotation_path(path, i) for i in range(1, 4))):
        _require_regular_or_absent(candidate)
    oldest = _rotation_path(path, LOG_ROTATIONS)
    oldest.unlink(missing_ok=True)
    for index in range(LOG_ROTATIONS - 1, 0, -1):
        source = _rotation_path(path, index)
        if source.exists():
            source.replace(_rotation_path(path, index + 1))
    if path.exists():
        path.replace(_rotation_path(path, 1))


def _append_log(path: Path, payload: bytes) -> None:
    target = path.expanduser().absolute()
    _secure_directory(target.parent)
    _require_regular_or_absent(target)
    size = target.stat().st_size if target.exists() else 0
    if size + len(payload) > LOG_MAX_BYTES:
        _rotate(target)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"refresh log must be a regular file: {target}")
        os.fchmod(fd, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count == 0:
                raise OSError(f"refresh log write made no progress: {target}")
            written += count
    finally:
        os.close(fd)


@contextmanager
def _execution_bound() -> Iterator[None]:
    def timed_out(_signum: int, _frame: FrameType | None) -> None:
        raise CatalogRefreshTimeout("catalog refresh exceeded overall timeout")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, OVERALL_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _timeout_result(started_at: str) -> dict[str, Any]:
    result = client_sync._result(
        "refresh",
        "repair_required",
        5,
        started_at,
        errors=[
            {
                "code": "overall_timeout",
                "group": None,
                "path": None,
                "message": "CatalogRefreshTimeout",
            }
        ],
    )
    result["finished_at"] = _timestamp()
    return result


def _record_timeout(path: Path, result: dict[str, Any]) -> None:
    prior = client_sync._load_refresh_status(path)
    client_sync._write_refresh_status(
        path,
        client_sync._refresh_status_record(result, prior=prior),
    )


def run(
    *,
    lock_path: Path = DEFAULT_CLIENT_SYNC_LOCK_PATH,
    status_path: Path = DEFAULT_STATUS_PATH,
    stdout_path: Path = DEFAULT_STDOUT_PATH,
    stderr_path: Path = DEFAULT_STDERR_PATH,
) -> int:
    """Run one scheduled refresh and return its process exit code."""
    started_at = _timestamp()
    try:
        with _execution_bound():
            try:
                lock = acquire_client_sync_lock(path=lock_path, blocking=False)
                with lock as token:
                    try:
                        _append_log(stdout_path, b"")
                        _append_log(stderr_path, b"")
                        result = client_sync.run(
                            "refresh",
                            lock_path=lock_path,
                            lock_token=token,
                            status_path=status_path,
                        )
                        encoded = (
                            json.dumps(result, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        ).encode("utf-8")
                        _append_log(stdout_path, encoded)
                    except CatalogRefreshTimeout:
                        result = _timeout_result(started_at)
                        _record_timeout(status_path, result)
                        result["catalog_refresh"] = client_sync._catalog_refresh(
                            path=status_path
                        )
                        encoded = (
                            json.dumps(result, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        ).encode("utf-8")
                        _append_log(stdout_path, encoded)
            except ClientSyncLockBusy:
                return 0
        return int(result["exit_code"])
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        try:
            _append_log(
                stderr_path,
                (json.dumps({"error": type(exc).__name__}) + "\n").encode("utf-8"),
            )
        except (OSError, RuntimeError):
            pass
        return 5


def main() -> int:
    return run()
