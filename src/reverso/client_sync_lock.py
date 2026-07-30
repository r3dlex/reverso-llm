"""Shared advisory lock for every write-capable client convergence entrypoint."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLIENT_SYNC_LOCK_PATH = (
    Path.home() / "Library" / "Application Support" / "reverso" / "catalog-refresh.lock"
)


class ClientSyncLockBusy(RuntimeError):
    """The shared client convergence lock could not be acquired in time."""


@dataclass
class HeldClientSyncLock:
    """An explicit ownership token passed to nested convergence operations."""

    path: Path
    fd: int
    owner_pid: int
    released: bool = False


@dataclass(frozen=True)
class _LockCapability:
    path: Path
    fd: int
    owner_pid: int
    device: int
    inode: int


@dataclass(frozen=True)
class _OpenedLock:
    absolute: Path
    fd: int
    directory_fds: tuple[int, ...]
    links: tuple[tuple[int, str, int], ...]


_ACTIVE_LOCKS: dict[int, _LockCapability] = {}
_ACTIVE_LOCKS_GUARD = threading.Lock()


def _canonical_lock_path(path: Path) -> Path:
    """Normalize the path and trusted root-level macOS aliases."""
    absolute = path.expanduser().absolute()
    parts = absolute.parts
    if len(parts) < 2:
        return absolute
    first = Path("/") / parts[1]
    try:
        info = first.lstat()
    except OSError:
        return absolute
    if not stat.S_ISLNK(info.st_mode):
        return absolute
    target = Path(os.readlink(first))
    if not target.is_absolute():
        target = first.parent / target
    return Path(os.path.normpath(str(target.joinpath(*parts[2:]))))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _close_directory_fds(opened: _OpenedLock) -> None:
    for directory_fd in reversed(opened.directory_fds):
        os.close(directory_fd)


def _validate_opened_lock(opened: _OpenedLock) -> None:
    try:
        for parent_fd, component, child_fd in opened.links:
            linked = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            child = os.fstat(child_fd)
            if not stat.S_ISDIR(linked.st_mode) or not _same_identity(linked, child):
                raise RuntimeError(
                    f"lock ancestor changed while opening: {opened.absolute.parent}"
                )
        linked_lock = os.stat(
            opened.absolute.name,
            dir_fd=opened.directory_fds[-1],
            follow_symlinks=False,
        )
        held_lock = os.fstat(opened.fd)
    except OSError as exc:
        raise RuntimeError(
            f"lock ancestry changed while opening: {opened.absolute.parent}"
        ) from exc
    if not _same_identity(linked_lock, held_lock):
        raise RuntimeError(f"client sync lock changed while opening: {opened.absolute}")
    if not stat.S_ISREG(held_lock.st_mode):
        raise RuntimeError(
            f"client sync lock must be a regular file: {opened.absolute}"
        )


def _open_lock_bound(path: Path) -> _OpenedLock:
    absolute = _canonical_lock_path(path)
    directory_fds = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
    links: list[tuple[int, str, int]] = []
    fd = -1
    try:
        for component in absolute.parent.parts[1:]:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            parent_fd = directory_fds[-1]
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(component, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise RuntimeError(
                        f"lock ancestor must be a real directory: {absolute.parent}"
                    ) from exc
                os.fchmod(child_fd, 0o700)
            except OSError as exc:
                raise RuntimeError(
                    f"lock ancestor must be a real directory: {absolute.parent}"
                ) from exc
            directory_fds.append(child_fd)
            links.append((parent_fd, component, child_fd))

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(
                absolute.name,
                flags,
                0o600,
                dir_fd=directory_fds[-1],
            )
        except OSError as exc:
            raise RuntimeError(
                f"client sync lock could not be opened: {absolute}"
            ) from exc
        opened = _OpenedLock(
            absolute=absolute,
            fd=fd,
            directory_fds=tuple(directory_fds),
            links=tuple(links),
        )
        _validate_opened_lock(opened)
        os.fchmod(fd, 0o600)
        return opened
    except BaseException:
        if fd >= 0:
            os.close(fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        raise


def _open_lock(path: Path) -> int:
    opened = _open_lock_bound(path)
    _close_directory_fds(opened)
    return opened.fd


def validate_client_sync_lock(
    token: HeldClientSyncLock,
    *,
    path: Path | None = None,
) -> None:
    """Require a live capability issued after this process acquired the lock."""
    requested = _canonical_lock_path(path) if path is not None else None
    with _ACTIVE_LOCKS_GUARD:
        capability = _ACTIVE_LOCKS.get(id(token))
    if capability is None:
        raise RuntimeError("client sync lock token was not issued by this process")
    if (
        token.released
        or token.path != capability.path
        or token.fd != capability.fd
        or token.owner_pid != capability.owner_pid
        or capability.owner_pid != os.getpid()
        or (requested is not None and requested != capability.path)
    ):
        raise RuntimeError("client sync lock token is not active in this process")
    try:
        current = os.fstat(capability.fd)
    except OSError as exc:
        raise RuntimeError("client sync lock token descriptor is not active") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != capability.device
        or current.st_ino != capability.inode
    ):
        raise RuntimeError("client sync lock token descriptor identity changed")


@contextmanager
def acquire_client_sync_lock(
    *,
    path: Path | None = None,
    token: HeldClientSyncLock | None = None,
    blocking: bool = True,
    timeout_seconds: float = 30.0,
) -> Iterator[HeldClientSyncLock]:
    """Acquire the shared lock or reuse an explicit already-held token.

    A nested caller given ``token`` neither reacquires nor releases the lock.
    """
    requested_path = _canonical_lock_path(path or DEFAULT_CLIENT_SYNC_LOCK_PATH)
    if token is not None:
        validate_client_sync_lock(token, path=requested_path)
        yield token
        return

    opened = _open_lock_bound(requested_path)
    fd = opened.fd
    ancestry_open = True
    acquired = False
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = timeout_seconds - (time.monotonic() - started)
                if not blocking or remaining <= 0:
                    raise ClientSyncLockBusy(
                        f"client sync lock is busy: {requested_path}"
                    ) from exc
                time.sleep(min(0.05, remaining))
        _validate_opened_lock(opened)
        _close_directory_fds(opened)
        ancestry_open = False
        held = HeldClientSyncLock(
            path=requested_path,
            fd=fd,
            owner_pid=os.getpid(),
        )
        identity = os.fstat(fd)
        with _ACTIVE_LOCKS_GUARD:
            _ACTIVE_LOCKS[id(held)] = _LockCapability(
                path=requested_path,
                fd=fd,
                owner_pid=os.getpid(),
                device=identity.st_dev,
                inode=identity.st_ino,
            )
        try:
            yield held
        finally:
            held.released = True
            with _ACTIVE_LOCKS_GUARD:
                _ACTIVE_LOCKS.pop(id(held), None)
    finally:
        if ancestry_open:
            _close_directory_fds(opened)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
