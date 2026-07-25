from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import pytest

from reverso.protocols.kimi_login import KimiLoginCoordinator, KimiLoginError


class _BlockingPipe:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, size: int) -> bytes:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return b""


class _CancellationResistantPipe(_BlockingPipe):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def read(self, size: int) -> bytes:
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return b""

    def close(self) -> None:
        self.closed.set()
        self.release.set()


class _Process:
    def __init__(
        self,
        *,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
        terminate_lookup_error: bool = False,
        kill_lookup_error: bool = False,
    ) -> None:
        self.stdout = _BlockingPipe()
        self.stderr = _BlockingPipe()
        self.returncode: int | None = None
        self.release = asyncio.Event()
        self.ignore_terminate = ignore_terminate
        self.ignore_kill = ignore_kill
        self.terminate_lookup_error = terminate_lookup_error
        self.kill_lookup_error = kill_lookup_error
        self.terminated = asyncio.Event()
        self.killed = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        await self.release.wait()
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.terminated.set()
        if self.terminate_lookup_error:
            self.release.set()
            raise ProcessLookupError
        if not self.ignore_terminate:
            self.release.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.killed.set()
        if self.kill_lookup_error:
            self.release.set()
            raise ProcessLookupError
        if not self.ignore_kill:
            self.release.set()


def _spawned_coordinator(
    process: _Process,
    *,
    timeout_seconds: float = 10,
    exit_grace_seconds: float = 0.01,
) -> tuple[KimiLoginCoordinator, asyncio.Event, list[tuple[Any, ...]]]:
    spawned = asyncio.Event()
    calls: list[tuple[Any, ...]] = []

    async def spawn(*args: Any, **kwargs: Any) -> _Process:
        calls.append(args)
        spawned.set()
        return process

    return (
        KimiLoginCoordinator(
            process_factory=spawn,
            timeout_seconds=timeout_seconds,
            exit_grace_seconds=exit_grace_seconds,
        ),
        spawned,
        calls,
    )


@pytest.mark.asyncio
async def test_one_cancelled_waiter_preserves_shared_login() -> None:
    process = _Process()
    coordinator, spawned, calls = _spawned_coordinator(process)
    first = asyncio.create_task(coordinator.login())
    second = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert process.terminate_calls == 0
    assert not second.done()
    assert calls == [("kimi", "login")]

    process.release.set()
    await asyncio.wait_for(second, timeout=0.1)


@pytest.mark.asyncio
async def test_last_waiter_cancellation_reaps_child_and_drains() -> None:
    process = _Process(ignore_terminate=True)
    coordinator, spawned, calls = _spawned_coordinator(process)
    waiter = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    await asyncio.wait_for(process.stdout.started.wait(), timeout=0.1)
    await asyncio.wait_for(process.stderr.started.wait(), timeout=0.1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls >= 2
    assert process.returncode == 0
    assert process.stdout.cancelled.is_set()
    assert process.stderr.cancelled.is_set()
    assert coordinator._task is None

    await coordinator.login()
    assert calls == [("kimi", "login"), ("kimi", "login")]


@pytest.mark.asyncio
async def test_arrival_during_last_waiter_cleanup_starts_fresh_login() -> None:
    first_process = _Process(ignore_terminate=True)
    second_process = _Process()
    processes = iter([first_process, second_process])
    calls: list[tuple[Any, ...]] = []
    second_spawned = asyncio.Event()

    async def spawn(*args: Any, **kwargs: Any) -> _Process:
        calls.append(args)
        process = next(processes)
        if process is second_process:
            second_spawned.set()
        return process

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        timeout_seconds=10,
        exit_grace_seconds=0.05,
    )
    abandoned = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(first_process.stdout.started.wait(), timeout=0.1)

    abandoned.cancel()
    await asyncio.wait_for(first_process.terminated.wait(), timeout=0.1)
    replacement = asyncio.create_task(coordinator.login())
    await asyncio.sleep(0)

    assert calls == [("kimi", "login")]
    assert not replacement.done()

    await asyncio.wait_for(second_spawned.wait(), timeout=0.2)
    second_process.release.set()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    await asyncio.wait_for(replacement, timeout=0.1)

    assert calls == [("kimi", "login"), ("kimi", "login")]
    assert first_process.kill_calls == 1
    assert first_process.returncode == 0


@pytest.mark.asyncio
async def test_timeout_fans_out_and_reaps_once() -> None:
    process = _Process(ignore_terminate=True)
    coordinator, spawned, calls = _spawned_coordinator(
        process,
        timeout_seconds=0.01,
    )
    waiters = [
        asyncio.create_task(coordinator.login()),
        asyncio.create_task(coordinator.login()),
    ]
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, KimiLoginError) for result in results)
    assert {str(result) for result in results} == {
        "Kimi login timed out; run kimi login manually in a terminal"
    }
    assert calls == [("kimi", "login")]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == 0
    assert process.stdout.cancelled.is_set()
    assert process.stderr.cancelled.is_set()
    assert coordinator._task is None
    assert coordinator._waiters == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "expected_outcome"),
    [
        (_Process(terminate_lookup_error=True), "already_exited"),
        (
            _Process(ignore_terminate=True, kill_lookup_error=True),
            "already_exited",
        ),
    ],
)
async def test_process_lookup_race_is_confirmed_reaped(
    process: _Process,
    expected_outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, _spawned, _calls = _spawned_coordinator(process)

    with caplog.at_level(logging.INFO):
        assert await coordinator._stop_process(process)

    assert process.returncode == 0
    assert process.wait_calls >= 1
    assert expected_outcome in {
        getattr(record, "outcome", None) for record in caplog.records
    }


@pytest.mark.asyncio
async def test_failed_post_kill_reap_is_fatal_and_blocks_new_login() -> None:
    process = _Process(ignore_terminate=True, ignore_kill=True)
    coordinator, spawned, calls = _spawned_coordinator(
        process,
        timeout_seconds=0.01,
        exit_grace_seconds=0.01,
    )
    waiter = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)

    with pytest.raises(KimiLoginError, match="cleanup failed"):
        await asyncio.wait_for(waiter, timeout=0.1)
    with pytest.raises(KimiLoginError, match="cleanup failed"):
        await coordinator.login()
    with pytest.raises(KimiLoginError, match="cleanup failed"):
        await coordinator.close()

    assert calls == [("kimi", "login")]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode is None
    assert coordinator._task is None


@pytest.mark.asyncio
async def test_cancellation_resistant_drains_fail_instead_of_reporting_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    process = _Process()
    stdout = _CancellationResistantPipe()
    stderr = _CancellationResistantPipe()
    process.stdout = stdout
    process.stderr = stderr
    process.release.set()
    coordinator, _spawned, calls = _spawned_coordinator(
        process,
        exit_grace_seconds=0.01,
    )

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(KimiLoginError, match="cleanup failed"),
    ):
        await asyncio.wait_for(coordinator.login(), timeout=0.1)
    with pytest.raises(KimiLoginError, match="cleanup failed"):
        await coordinator.login()

    events = {getattr(record, "event", None) for record in caplog.records}
    assert "kimi_login.success" not in events
    assert "pipe_drain_timeout" in {
        getattr(record, "failure_kind", None) for record in caplog.records
    }
    assert calls == [("kimi", "login")]
    assert stdout.cancelled.is_set()
    assert stderr.cancelled.is_set()
    assert stdout.closed.is_set()
    assert stderr.closed.is_set()
    assert not any(
        task.get_coro().__qualname__.endswith("KimiLoginCoordinator._discard")
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_shutdown_rejects_new_waiters_and_fails_existing_waiters() -> None:
    process = _Process(ignore_terminate=True)
    coordinator, spawned, _calls = _spawned_coordinator(process)
    waiters = [
        asyncio.create_task(coordinator.login()),
        asyncio.create_task(coordinator.login()),
    ]
    await asyncio.wait_for(spawned.wait(), timeout=0.1)
    await asyncio.sleep(0)

    await coordinator.close()
    await coordinator.close()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, KimiLoginError) for result in results)
    assert {str(result) for result in results} == {
        "Kimi login is unavailable during gateway shutdown"
    }
    with pytest.raises(KimiLoginError, match="gateway shutdown"):
        await coordinator.login()
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == 0
    assert process.stdout.cancelled.is_set()
    assert process.stderr.cancelled.is_set()
    assert coordinator._task is None


@pytest.mark.asyncio
async def test_close_cleanup_survives_cancelled_caller_and_is_idempotent() -> None:
    process = _Process(ignore_terminate=True)
    coordinator, spawned, _calls = _spawned_coordinator(
        process,
        exit_grace_seconds=0.05,
    )
    waiter = asyncio.create_task(coordinator.login())
    await asyncio.wait_for(spawned.wait(), timeout=0.1)

    first_close = asyncio.create_task(coordinator.close())
    await asyncio.wait_for(process.terminated.wait(), timeout=0.1)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    await asyncio.wait_for(coordinator.close(), timeout=0.2)
    result = await asyncio.gather(waiter, return_exceptions=True)

    assert isinstance(result[0], KimiLoginError)
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_large_child_output_is_drained_without_logging_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_output = "device-code-material"
    payload_bytes = 256 * 1024

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        script = (
            "import os;"
            f"payload={sensitive_output!r}.encode()*"
            f"(({payload_bytes}//len({sensitive_output!r}))+1);"
            "os.write(1,payload);os.write(2,payload)"
        )
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    coordinator = KimiLoginCoordinator(
        process_factory=spawn,
        timeout_seconds=2,
        exit_grace_seconds=0.1,
    )

    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(coordinator.login(), timeout=3)

    assert sensitive_output not in caplog.text
    assert payload_bytes > 64 * 1024
    assert {getattr(record, "event", None) for record in caplog.records} >= {
        "kimi_login.start",
        "kimi_login.success",
        "kimi_login.child_reaped",
    }


@pytest.mark.asyncio
async def test_lifecycle_events_are_classified_and_secret_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    process = _Process(ignore_terminate=True)
    coordinator, spawned, _calls = _spawned_coordinator(process)

    with caplog.at_level(logging.INFO):
        waiters = [
            asyncio.create_task(coordinator.login()),
            asyncio.create_task(coordinator.login()),
        ]
        await asyncio.wait_for(spawned.wait(), timeout=0.1)
        await coordinator.close()
        await asyncio.gather(*waiters, return_exceptions=True)

    events = {getattr(record, "event", None) for record in caplog.records}
    assert {
        "kimi_login.start",
        "kimi_login.join",
        "kimi_login.cancellation",
        "kimi_login.shutdown_cleanup",
        "kimi_login.child_reaped",
    } <= events
    assert "token" not in caplog.text.lower()
    assert "device" not in caplog.text.lower()
    assert "auth" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_timeout_event_is_classified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    process = _Process()
    coordinator, spawned, _calls = _spawned_coordinator(
        process,
        timeout_seconds=0.01,
    )

    with caplog.at_level(logging.INFO):
        waiter = asyncio.create_task(coordinator.login())
        await asyncio.wait_for(spawned.wait(), timeout=0.1)
        with pytest.raises(KimiLoginError, match="timed out"):
            await waiter

    assert "kimi_login.timeout" in {
        getattr(record, "event", None) for record in caplog.records
    }
    assert "timeout" in {
        getattr(record, "failure_kind", None) for record in caplog.records
    }
