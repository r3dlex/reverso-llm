"""Gateway-owned supervision for the official ``kimi login`` command."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

_LOGIN_TIMEOUT_SECONDS = 15 * 60 + 30
_PROCESS_EXIT_GRACE_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024
_CLEANUP_FAILURE_MESSAGE = "Kimi login cleanup failed; terminate the Kimi CLI manually"

ProcessFactory = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


def _log_event(
    name: str,
    *,
    outcome: str | None = None,
    failure_kind: str | None = None,
) -> None:
    """Emit one bounded lifecycle event without child or credential data."""
    extra = {"event": f"kimi_login.{name}"}
    if outcome is not None:
        extra["outcome"] = outcome
    if failure_kind is not None:
        extra["failure_kind"] = failure_kind
    logger.info("Kimi login lifecycle", extra=extra)


class KimiLoginError(RuntimeError):
    """A bounded, secret-free official-login failure."""

    @property
    def public_message(self) -> str:
        """Return the only message safe to expose through the gateway."""
        return str(self)


class KimiLoginCoordinator:
    """Run at most one official Kimi login process for this gateway."""

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None = None,
        timeout_seconds: float = _LOGIN_TIMEOUT_SECONDS,
        exit_grace_seconds: float = _PROCESS_EXIT_GRACE_SECONDS,
    ) -> None:
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._timeout_seconds = timeout_seconds
        self._exit_grace_seconds = exit_grace_seconds
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._waiters = 0
        self._closed = False
        self._fatal_error: str | None = None

    async def ensure_authenticated(self) -> None:
        """Compatibility name for awaiting the shared official login."""
        await self.login()

    async def login(self) -> None:
        """Await the shared official login attempt."""
        while True:
            async with self._lock:
                if self._fatal_error is not None:
                    raise KimiLoginError(self._fatal_error)
                if self._closed:
                    raise KimiLoginError(
                        "Kimi login is unavailable during gateway shutdown"
                    )
                cleanup_task = self._cleanup_task
                if cleanup_task is None:
                    task = self._task
                    if task is None:
                        _log_event("start")
                        task = asyncio.create_task(self._run_login())
                        self._task = task
                    else:
                        _log_event("join")
                    self._waiters += 1
                    break
            await asyncio.shield(cleanup_task)

        cancelled = False
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if self._closed:
                raise KimiLoginError(
                    "Kimi login is unavailable during gateway shutdown"
                ) from None
            raise
        finally:
            cancel_cleanup_task: asyncio.Task[None] | None = None
            async with self._lock:
                self._waiters -= 1
                if (
                    cancelled
                    and self._waiters == 0
                    and self._task is task
                    and not task.done()
                    and not self._closed
                ):
                    task.cancel()
                    self._task = None
                    cleanup_task = asyncio.create_task(
                        self._finish_cancelled_login(task)
                    )
                    self._cleanup_task = cleanup_task
                    cancel_cleanup_task = cleanup_task
                elif task.done() and self._waiters == 0 and self._task is task:
                    self._task = None
            if cancel_cleanup_task is not None:
                await asyncio.shield(cancel_cleanup_task)

    async def close(self) -> None:
        """Stop accepting login work and reap any active child."""
        async with self._lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                close_task = asyncio.create_task(
                    self._close_active_login(self._task, self._cleanup_task)
                )
                self._close_task = close_task
        await asyncio.shield(close_task)

    async def _finish_cancelled_login(self, task: asyncio.Task[None]) -> None:
        await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            if self._cleanup_task is asyncio.current_task():
                self._cleanup_task = None

    async def _close_active_login(
        self,
        task: asyncio.Task[None] | None,
        cleanup_task: asyncio.Task[None] | None,
    ) -> None:
        _log_event("shutdown_cleanup", outcome="started")
        if task is not None and not task.done():
            task.cancel()
        results: list[Any] = []
        if task is not None:
            results.extend(await asyncio.gather(task, return_exceptions=True))
        if cleanup_task is not None:
            await asyncio.shield(cleanup_task)
        async with self._lock:
            if self._task is task:
                self._task = None
            fatal_error = self._fatal_error
        cleanup_error = next(
            (
                result
                for result in results
                if isinstance(result, KimiLoginError)
                and "cleanup failed" in str(result)
            ),
            None,
        )
        if cleanup_error is not None or fatal_error is not None:
            _log_event("shutdown_cleanup", outcome="failed")
            if cleanup_error is not None:
                raise cleanup_error
            raise KimiLoginError(fatal_error)
        _log_event("shutdown_cleanup", outcome="completed")

    async def _run_login(self) -> None:
        try:
            process = await self._process_factory(
                "kimi",
                "login",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            _log_event("failure", failure_kind="cli_unavailable")
            raise KimiLoginError(
                "Kimi CLI is unavailable; install it or run kimi login manually"
            ) from exc

        drains = {
            asyncio.create_task(self._discard(stream)): stream
            for stream in (process.stdout, process.stderr)
            if stream is not None
        }
        try:
            try:
                return_code = await asyncio.wait_for(
                    process.wait(), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                _log_event("timeout")
                reaped = await self._stop_process(process)
                _log_event("failure", failure_kind="timeout")
                if not reaped:
                    _log_event("failure", failure_kind="child_reap_timeout")
                    self._fatal_error = _CLEANUP_FAILURE_MESSAGE
                    raise KimiLoginError(_CLEANUP_FAILURE_MESSAGE) from exc
                raise KimiLoginError(
                    "Kimi login timed out; run kimi login manually in a terminal"
                ) from exc
            except asyncio.CancelledError:
                _log_event(
                    "cancellation",
                    outcome="shutdown" if self._closed else "no_waiters",
                )
                reaped = await self._stop_process(process)
                if self._closed:
                    if not reaped:
                        _log_event("failure", failure_kind="child_reap_timeout")
                        self._fatal_error = _CLEANUP_FAILURE_MESSAGE
                        raise KimiLoginError(
                            "Kimi login shutdown cleanup failed"
                        ) from None
                    raise KimiLoginError(
                        "Kimi login is unavailable during gateway shutdown"
                    ) from None
                if not reaped:
                    _log_event("failure", failure_kind="child_reap_timeout")
                    self._fatal_error = _CLEANUP_FAILURE_MESSAGE
                raise
            _log_event("child_reaped", outcome="exited")
            if return_code != 0:
                _log_event("failure", failure_kind="nonzero_exit")
                raise KimiLoginError(
                    "Kimi login failed; run kimi login manually in a terminal"
                )
        finally:
            drains_finished = await self._finish_drains(drains)
            if not drains_finished:
                _log_event("failure", failure_kind="pipe_drain_timeout")
                self._fatal_error = _CLEANUP_FAILURE_MESSAGE
                if self._closed:
                    raise KimiLoginError("Kimi login shutdown cleanup failed") from None
                raise KimiLoginError(_CLEANUP_FAILURE_MESSAGE) from None
        _log_event("success")

    async def _stop_process(self, process: Any) -> bool:
        if process.returncode is not None:
            _log_event("child_reaped", outcome="already_exited")
            return True
        try:
            process.terminate()
        except ProcessLookupError:
            if not await self._wait_for_exit(process):
                return False
            _log_event("child_reaped", outcome="already_exited")
            return True
        if await self._wait_for_exit(process):
            _log_event("child_reaped", outcome="terminated")
            return True
        try:
            process.kill()
        except ProcessLookupError:
            if not await self._wait_for_exit(process):
                return False
            _log_event("child_reaped", outcome="already_exited")
            return True
        if not await self._wait_for_exit(process):
            return False
        _log_event("child_reaped", outcome="killed")
        return True

    async def _wait_for_exit(self, process: Any) -> bool:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._exit_grace_seconds,
            )
        except TimeoutError:
            return False
        return True

    @staticmethod
    async def _discard(stream: Any) -> None:
        while await stream.read(_READ_CHUNK_BYTES):
            pass

    async def _finish_drains(
        self,
        drains: dict[asyncio.Task[None], Any],
    ) -> bool:
        """Bound pipe cleanup after the child is reaped.

        Production drains read ``asyncio.StreamReader`` subprocess pipes. Those
        readers either honor task cancellation or can be unblocked by closing
        their private transport. Injected test streams may expose a synchronous
        ``close()`` hook instead. Streams that ignore cancellation must support
        one of those close paths; arbitrary permanently blocking awaitables are
        outside the process-factory contract.
        """
        if not drains:
            return True
        done, pending = await asyncio.wait(
            drains.keys(),
            timeout=self._exit_grace_seconds,
        )
        forced_close = False
        if pending:
            for drain in pending:
                forced_close = self._close_drain_stream(drains[drain]) or forced_close
                drain.cancel()
            closed, pending = await asyncio.wait(
                pending,
                timeout=self._exit_grace_seconds,
            )
            done.update(closed)
        await asyncio.gather(*done, return_exceptions=True)
        return not forced_close and not pending

    @staticmethod
    def _close_drain_stream(stream: Any) -> bool:
        """Synchronously unblock a supported subprocess pipe reader."""
        close = getattr(stream, "close", None)
        if callable(close):
            close()
            return True
        transport = getattr(stream, "_transport", None)
        close_transport = getattr(transport, "close", None)
        if callable(close_transport):
            close_transport()
            return True
        return False
