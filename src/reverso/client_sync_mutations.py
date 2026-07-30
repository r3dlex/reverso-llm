"""Immutable filesystem candidates for client convergence."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ObjectKind = Literal["absent", "file", "directory", "symlink"]
_CREATION_OBSERVER: ContextVar[Callable[[], None] | None] = ContextVar(
    "client_sync_creation_observer",
    default=None,
)


class PreparedStateChanged(RuntimeError):
    """A prepared path changed before its candidate could be applied."""

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        transitioned: bool = False,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.transitioned = transitioned


class PreparedApplyFailed(RuntimeError):
    """A prepared group failed and was restored successfully."""


class PreparedRollbackFailed(RuntimeError):
    """A prepared group failed and could not be restored completely."""


@dataclass(frozen=True)
class FileState:
    """Complete restorable state for one supported filesystem object."""

    kind: ObjectKind
    data: bytes | str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class _TransitionGuard:
    path: Path
    state: FileState
    target_path: Path


class _TransitionGuardCleanupFailed(RuntimeError):
    def __init__(self, guard: _TransitionGuard) -> None:
        super().__init__(f"transition guard cleanup failed: {guard.path}")
        self.guard = guard


class _TransitionGuardApplyFailed(RuntimeError):
    def __init__(
        self,
        guard: _TransitionGuard,
        *,
        target_transitioned: bool,
    ) -> None:
        super().__init__(f"candidate creation failed: {guard.path}")
        self.guard = guard
        self.target_transitioned = target_transitioned


@dataclass(frozen=True)
class PreparedMutation:
    """One exact path transition validated before the first group write."""

    path: Path
    before: FileState
    after: FileState

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class PreparedGroup:
    """One handled-failure atomic group of immutable path transitions."""

    id: str
    mutations: tuple[PreparedMutation, ...]

    @property
    def changed(self) -> bool:
        return any(mutation.changed for mutation in self.mutations)


def capture_state(path: Path) -> FileState:
    """Capture bytes, existence, object type, symlink target, and mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return FileState("absent")
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return FileState("symlink", os.readlink(path), mode)
    if stat.S_ISREG(info.st_mode):
        return FileState("file", path.read_bytes(), mode)
    if stat.S_ISDIR(info.st_mode):
        return FileState("directory", None, mode)
    raise RuntimeError(f"unsupported filesystem object: {path}")


def file_state(data: bytes | str, mode: int = 0o600) -> FileState:
    """Build an immutable regular-file state."""
    encoded = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return FileState("file", encoded, mode)


def directory_state(mode: int = 0o700) -> FileState:
    """Build an immutable real-directory state."""
    return FileState("directory", None, mode)


def symlink_state(target: Path | str) -> FileState:
    """Build an immutable symlink state without resolving its target."""
    return FileState("symlink", str(target), None)


def prepared_mutation(path: Path, after: FileState) -> PreparedMutation:
    """Capture a path's current state and pair it with an exact candidate."""
    return PreparedMutation(path=path, before=capture_state(path), after=after)


def missing_parent_mutations(
    paths: Iterable[Path],
    *,
    mode: int = 0o700,
) -> tuple[PreparedMutation, ...]:
    """Prepare every absent parent needed by ``paths``, outermost first."""
    missing: set[Path] = set()
    for path in paths:
        parent = path.parent
        while capture_state(parent).kind == "absent":
            missing.add(parent)
            parent = parent.parent
    return tuple(
        prepared_mutation(path, directory_state(mode))
        for path in sorted(missing, key=lambda item: (len(item.parts), str(item)))
    )


def _capture_at(parent_fd: int, name: str) -> FileState:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return FileState("absent")
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return FileState("symlink", os.readlink(name, dir_fd=parent_fd), mode)
    if stat.S_ISREG(info.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return FileState("file", handle.read(), mode)
        finally:
            if fd >= 0:
                os.close(fd)
    if stat.S_ISDIR(info.st_mode):
        return FileState("directory", None, mode)
    raise RuntimeError(f"unsupported filesystem object: {name}")


def _open_parent_fd(path: Path) -> tuple[int, str]:
    absolute = path.absolute()
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parent.parts[1:]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise PreparedStateChanged(
                    f"prepared parent changed before apply: {absolute.parent}"
                ) from exc
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd, absolute.name
    except BaseException:
        os.close(parent_fd)
        raise


def _remove_current(parent_fd: int, name: str) -> None:
    state = _capture_at(parent_fd, name)
    if state.kind == "absent":
        return
    if state.kind in {"file", "symlink"}:
        os.unlink(name, dir_fd=parent_fd)
        return
    os.rmdir(name, dir_fd=parent_fd)


def _atomic_write(parent_fd: int, name: str, data: bytes, mode: int) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _create_state(
    parent_fd: int,
    name: str,
    state: FileState,
) -> None:
    if state.kind == "absent":
        return
    if state.kind == "directory":
        os.mkdir(name, state.mode or 0o700, dir_fd=parent_fd)
        observer = _CREATION_OBSERVER.get()
        if observer is not None:
            observer()
        return
    if state.kind == "symlink":
        os.symlink(str(state.data), name, dir_fd=parent_fd)
        observer = _CREATION_OBSERVER.get()
        if observer is not None:
            observer()
        return
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        state.mode or 0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(fd, state.mode or 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(state.data if isinstance(state.data, bytes) else b"")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        observer = _CREATION_OBSERVER.get()
        if observer is not None:
            observer()
        os.unlink(temporary, dir_fd=parent_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _remove_named_state(parent_fd: int, name: str, state: FileState) -> None:
    if state.kind == "directory":
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _apply_expected_state(
    parent_fd: int,
    name: str,
    expected: FileState,
    state: FileState,
    *,
    path: Path,
) -> None:
    current = _capture_at(parent_fd, name)
    if current != expected:
        raise PreparedStateChanged(f"prepared leaf changed before apply: {name}")
    if current == state:
        return
    if expected.kind == "absent":
        try:
            _create_state(parent_fd, name, state)
        except FileExistsError as exc:
            raise PreparedStateChanged(
                f"prepared leaf changed during apply: {name}"
            ) from exc
        return
    if expected.kind == "directory" and state.kind == "directory":
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(directory_fd, state.mode or 0o700)
            if _capture_at(parent_fd, name) != state:
                try:
                    os.fchmod(directory_fd, expected.mode or 0o700)
                except OSError as exc:
                    raise PreparedStateChanged(
                        f"prepared leaf changed during apply: {name}",
                        transitioned=True,
                    ) from exc
                if _capture_at(parent_fd, name) != expected:
                    raise PreparedStateChanged(
                        f"prepared leaf changed during apply: {name}",
                        transitioned=True,
                    )
                raise PreparedStateChanged(
                    f"prepared leaf changed during apply: {name}"
                )
        finally:
            os.close(directory_fd)
        return

    guard = f".{name}.{secrets.token_hex(8)}.guard"
    os.rename(name, guard, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    guarded = _capture_at(parent_fd, guard)
    transition_guard = _TransitionGuard(
        path.with_name(guard),
        guarded,
        path,
    )
    if guarded != expected:
        raise _TransitionGuardApplyFailed(
            transition_guard,
            target_transitioned=False,
        ) from PreparedStateChanged(f"prepared leaf changed during apply: {name}")
    target_transitioned = False

    def mark_target_transitioned() -> None:
        nonlocal target_transitioned
        target_transitioned = True

    observer_token = _CREATION_OBSERVER.set(mark_target_transitioned)
    try:
        _create_state(parent_fd, name, state)
    except BaseException as exc:
        raise _TransitionGuardApplyFailed(
            transition_guard,
            target_transitioned=target_transitioned,
        ) from exc
    finally:
        _CREATION_OBSERVER.reset(observer_token)
    try:
        _remove_named_state(parent_fd, guard, guarded)
    except (OSError, RuntimeError) as exc:
        raise _TransitionGuardCleanupFailed(transition_guard) from exc


def apply_state(
    path: Path,
    state: FileState,
    *,
    expected: FileState | None = None,
) -> None:
    """Replace one path with the exact supported object state."""
    parent_fd, name = _open_parent_fd(path)
    try:
        if expected is not None:
            _apply_expected_state(
                parent_fd,
                name,
                expected,
                state,
                path=path.absolute(),
            )
            return
        current = _capture_at(parent_fd, name)
        if current == state:
            return
        if state.kind == "absent":
            _remove_current(parent_fd, name)
            return
        if state.kind == "directory":
            if current.kind == "absent":
                os.mkdir(name, state.mode or 0o700, dir_fd=parent_fd)
            elif current.kind != "directory":
                _remove_current(parent_fd, name)
                os.mkdir(name, state.mode or 0o700, dir_fd=parent_fd)
            directory_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(directory_fd, state.mode or 0o700)
            finally:
                os.close(directory_fd)
            return
        if current.kind == "directory":
            _remove_current(parent_fd, name)
        if state.kind == "symlink":
            _remove_current(parent_fd, name)
            os.symlink(str(state.data), name, dir_fd=parent_fd)
            return
        _atomic_write(
            parent_fd,
            name,
            state.data if isinstance(state.data, bytes) else b"",
            state.mode or 0o600,
        )
    finally:
        os.close(parent_fd)


def _parents(path: Path) -> Iterable[Path]:
    parent = path.parent
    while parent != parent.parent:
        yield parent
        parent = parent.parent


def validate_prepared_group(group: PreparedGroup) -> None:
    """Prove all path preconditions still match before the first mutation."""
    seen: set[Path] = set()
    mutation_by_path = {mutation.path: mutation for mutation in group.mutations}
    for mutation in group.mutations:
        if mutation.path in seen:
            raise RuntimeError(f"prepared group has duplicate path: {mutation.path}")
        seen.add(mutation.path)
        if capture_state(mutation.path) != mutation.before:
            raise PreparedStateChanged(
                f"prepared path changed before apply: {mutation.path}",
                path=mutation.path,
            )
        if mutation.after.kind == "absent":
            continue
        for parent in _parents(mutation.path):
            parent_mutation = mutation_by_path.get(parent)
            if parent_mutation is not None:
                if parent_mutation.after.kind != "directory":
                    raise RuntimeError(f"prepared parent is not a directory: {parent}")
                break
            parent_state = capture_state(parent)
            if parent_state.kind == "directory":
                break
            if parent_state.kind != "absent":
                raise RuntimeError(f"prepared parent is not a directory: {parent}")
            raise RuntimeError(
                f"missing parent directory is not in mutation ledger: {parent}"
            )


MutationObserver = Callable[[PreparedMutation, int], None]


def _settle_transition_guard(
    guard: _TransitionGuard,
) -> None:
    parent_fd, guard_name = _open_parent_fd(guard.path)
    try:
        if _capture_at(parent_fd, guard_name) != guard.state:
            raise PreparedStateChanged(
                f"transition guard changed before rollback: {guard.path}"
            )
        target_name = guard.target_path.absolute().name
        if _capture_at(parent_fd, target_name).kind == "absent":
            try:
                _create_state(parent_fd, target_name, guard.state)
            except FileExistsError:
                pass
        if _capture_at(parent_fd, guard_name) != guard.state:
            raise PreparedStateChanged(
                f"transition guard changed during rollback: {guard.path}"
            )
        _remove_named_state(parent_fd, guard_name, guard.state)
    finally:
        os.close(parent_fd)


def apply_prepared_group(
    group: PreparedGroup,
    *,
    observer: MutationObserver | None = None,
) -> None:
    """Apply exact candidates and restore every touched path on handled failure."""
    validate_prepared_group(group)
    changed = tuple(mutation for mutation in group.mutations if mutation.changed)
    touched: list[PreparedMutation] = []
    try:
        for index, mutation in enumerate(changed):
            if observer is not None:
                observer(mutation, index)
            touched.append(mutation)
            apply_state(
                mutation.path,
                mutation.after,
                expected=mutation.before,
            )
    except (OSError, RuntimeError) as exc:
        transition_guard = (
            exc.guard
            if isinstance(
                exc,
                (_TransitionGuardApplyFailed, _TransitionGuardCleanupFailed),
            )
            else None
        )
        fatal_cause = (
            exc.__cause__
            if isinstance(exc, _TransitionGuardApplyFailed)
            and exc.__cause__ is not None
            and not isinstance(exc.__cause__, Exception)
            else None
        )
        if touched:
            failed = touched[-1]
            current = capture_state(failed.path)
            if isinstance(exc, _TransitionGuardApplyFailed):
                if not exc.target_transitioned or current != failed.after:
                    touched.pop()
            elif (
                isinstance(exc, PreparedStateChanged) and not exc.transitioned
            ) or current == failed.before:
                touched.pop()
        try:
            for mutation in reversed(touched):
                apply_state(
                    mutation.path,
                    mutation.before,
                    expected=mutation.after,
                )
            if transition_guard is not None:
                _settle_transition_guard(transition_guard)
        except (OSError, RuntimeError) as rollback_exc:
            raise PreparedRollbackFailed(type(rollback_exc).__name__) from exc
        if fatal_cause is not None:
            raise fatal_cause
        raise PreparedApplyFailed(type(exc).__name__) from exc


def verify_prepared_group(group: PreparedGroup) -> tuple[Path, ...]:
    """Return paths whose current readback differs from the prepared candidate."""
    return tuple(
        mutation.path
        for mutation in group.mutations
        if capture_state(mutation.path) != mutation.after
    )
