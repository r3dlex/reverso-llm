"""Bounded CLI spine shared by CLI-backed provider adapters (ADR 0005).

CLI-backed provider adapters (claude, auggie) execute a locally installed
binary as a subprocess, either one-shot (``run_bounded_cli``) or streaming
line-by-line (``stream_bounded_cli``). The safety contract of that invocation
is identical everywhere and is owned HERE, exactly once:

- wall-clock bound: every invocation carries a timeout (default 300s) so a
  hung CLI can never pin a gateway worker thread or SSE connection
  indefinitely;
- redaction: stderr passes through ``redact_secret`` before any logging, so
  token material never reaches a log line;
- cause suppression: ``CalledProcessError`` carries raw stderr and argv, so
  the provider-typed error is raised ``from None`` and the cause can never
  ride a traceback into logs or error bodies;
- kill-on-abandon (streaming only): when the consumer stops iterating (for
  example a client disconnect mid-stream), the child process is killed
  instead of leaking and running its turn to completion.

Adapters contribute only their argv (plus an optional child environment) and
parse the returned stdout themselves. Before this module existed the same
contract was copy-pasted per adapter and had silently diverged: the claude
runner had no timeout and re-raised with its cause attached.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping, Sequence

from reverso.protocols.auth import redact_secret

logger = logging.getLogger(__name__)

# Shared wall-clock bound for one-shot CLI turns. A timeout surfaces as the
# provider-typed error, never as an unbounded hang.
DEFAULT_CLI_TIMEOUT_SECONDS = 300.0


def _resolve_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return str(Path(cwd).expanduser().resolve(strict=False))


def _cwd_exists(cwd: str | None) -> bool:
    return cwd is None or Path(cwd).is_dir()


class BoundedCliStreamFailure(RuntimeError):
    """A ``stream_bounded_cli`` failure with a redaction-safe message.

    ``returncode`` is set ONLY for the nonzero-exit failure mode, so callers
    can tell "the CLI exited rc!=0" apart from "missing binary" and "timed
    out" without parsing the message. The message never carries stderr or
    argv content; redacted stderr goes to the log inside the spine.
    """

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def run_bounded_cli(
    argv: Sequence[str],
    *,
    error: Callable[[str], Exception],
    cli_label: str,
    failure_message: str | None = None,
    timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> str:
    """Run ``argv`` once, bounded, and return its stdout.

    ``error`` is the provider-typed exception factory (e.g. ``AuggieError``);
    every failure mode raises through it:

    - missing binary: ``"<cli_label> not found on PATH"``
    - wall-clock exceeded: ``"<cli_label> timed out"``
    - nonzero exit: ``failure_message`` (defaults to ``"<cli_label>
      invocation failed"``) after logging the REDACTED stderr; the
      ``CalledProcessError`` cause is suppressed because it carries raw
      stderr and argv.

    ``env``, when given, replaces the child environment entirely; callers
    that need inheritance must merge ``os.environ`` themselves. ``cwd`` is
    resolved without requiring the directory to exist yet and is never logged.
    """
    message = failure_message or f"{cli_label} invocation failed"
    resolved_cwd = _resolve_cwd(cwd)
    if not _cwd_exists(resolved_cwd):
        raise error(f"{cli_label} workspace cwd not found")
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            cwd=resolved_cwd,
        )
    except FileNotFoundError as exc:
        raise error(f"{cli_label} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise error(f"{cli_label} timed out") from exc
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "%s failed (rc=%s): %s",
            cli_label,
            exc.returncode,
            redact_secret(exc.stderr or None),
        )
        # Suppress the cause: CalledProcessError carries raw stderr/argv that
        # could include token material and must not ride along in a traceback.
        raise error(message) from None
    return result.stdout


async def stream_bounded_cli(
    argv: Sequence[str],
    *,
    cli_label: str,
    timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> AsyncIterator[str]:
    """Run ``argv`` once and yield its stdout line by line, bounded.

    The streaming counterpart of ``run_bounded_cli``. The whole invocation
    shares ONE wall-clock deadline (``timeout`` from spawn): every stdout
    read is capped by the remaining budget, so a CLI that wedges mid-stream
    surfaces ``"<cli_label> timed out"`` instead of pinning the SSE
    connection forever. Lines are yielded decoded (UTF-8, replace) with
    their trailing newline intact; parsing belongs to the adapter.

    Failure modes all raise :class:`BoundedCliStreamFailure` with a
    redaction-safe message:

    - missing binary: ``"<cli_label> not found on PATH"``
    - deadline exceeded (child killed): ``"<cli_label> timed out"``
    - nonzero exit after EOF: ``"<cli_label> exited rc=<n>"`` with
      ``returncode`` set, after logging the REDACTED stderr.

    If the consumer abandons the iterator (client disconnect mid-stream),
    the child is killed rather than left running its turn to completion.
    ``env``, when given, replaces the child environment entirely and is
    never logged. ``cwd`` is resolved without requiring the directory to exist
    yet and is never logged.
    """
    resolved_cwd = _resolve_cwd(cwd)
    if not _cwd_exists(resolved_cwd):
        raise BoundedCliStreamFailure(f"{cli_label} workspace cwd not found")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
            cwd=resolved_cwd,
        )
    except FileNotFoundError as exc:
        raise BoundedCliStreamFailure(f"{cli_label} not found on PATH") from exc

    assert process.stdout is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BoundedCliStreamFailure(f"{cli_label} timed out")
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=remaining
                )
            except (asyncio.TimeoutError, TimeoutError):
                raise BoundedCliStreamFailure(f"{cli_label} timed out") from None
            if not line:
                break
            yield line.decode("utf-8", "replace")

        # 1ms floor: stdout has already closed, so the child is exiting; grant
        # a minimal wait to reap it instead of misreporting a deadline-edge
        # timeout (unlike the read loop, which checks remaining <= 0 strictly).
        remaining = max(deadline - loop.time(), 0.001)
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            raise BoundedCliStreamFailure(f"{cli_label} timed out") from None
        if returncode:
            stderr_text = await _read_stderr(process)
            logger.warning(
                "%s stream exited rc=%s: %s",
                cli_label,
                returncode,
                redact_secret(stderr_text or None),
            )
            raise BoundedCliStreamFailure(
                f"{cli_label} exited rc={returncode}", returncode=returncode
            )
    finally:
        # Reached on normal completion, on failure, AND on consumer abandon
        # (GeneratorExit): a still-running child is killed, never leaked.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await process.wait()
            except Exception:  # noqa: BLE001 - defensive cleanup
                pass


async def _read_stderr(process: asyncio.subprocess.Process) -> str:
    """Best-effort read of a finished child's stderr; never raises."""
    if process.stderr is None:
        return ""
    try:
        data = await process.stderr.read()
    except Exception:  # noqa: BLE001 - defensive cleanup
        return ""
    return data.decode("utf-8", "replace")
