"""Gateway-owned supervision for the official ``kimi login`` command."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

_LOGIN_TIMEOUT_SECONDS = 15 * 60 + 30
_PROCESS_EXIT_GRACE_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024

ProcessFactory = Callable[..., Awaitable[Any]]


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
        self._closed = False

    async def ensure_authenticated(self) -> None:
        """Compatibility name for awaiting the shared official login."""
        await self.login()

    async def login(self) -> None:
        """Await the shared official login attempt."""
        async with self._lock:
            if self._closed:
                raise KimiLoginError(
                    "Kimi login is unavailable during gateway shutdown"
                )
            task = self._task
            if task is None:
                task = asyncio.create_task(self._run_login())
                self._task = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._task is task:
                        self._task = None

    async def close(self) -> None:
        """Stop accepting login work and reap any active child."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            if self._task is task:
                self._task = None

    async def _run_login(self) -> None:
        try:
            process = await self._process_factory(
                "kimi",
                "login",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise KimiLoginError(
                "Kimi CLI is unavailable; install it or run kimi login manually"
            ) from exc

        drains = [
            asyncio.create_task(self._discard(stream))
            for stream in (process.stdout, process.stderr)
            if stream is not None
        ]
        try:
            try:
                return_code = await asyncio.wait_for(
                    process.wait(), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                await self._stop_process(process)
                raise KimiLoginError(
                    "Kimi login timed out; run kimi login manually in a terminal"
                ) from exc
            except asyncio.CancelledError:
                await self._stop_process(process)
                raise
            if return_code != 0:
                raise KimiLoginError(
                    "Kimi login failed; run kimi login manually in a terminal"
                )
        finally:
            await self._finish_drains(drains)

    async def _stop_process(self, process: Any) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._exit_grace_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _discard(stream: Any) -> None:
        while await stream.read(_READ_CHUNK_BYTES):
            pass

    async def _finish_drains(self, drains: list[asyncio.Task[None]]) -> None:
        """Bound pipe cleanup after the child is reaped."""
        for drain in drains:
            drain.cancel()
        if drains:
            await asyncio.wait(drains, timeout=self._exit_grace_seconds)
