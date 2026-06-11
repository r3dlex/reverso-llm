"""Bounded CLI spine shared by CLI-backed provider adapters (ADR 0005).

CLI-backed provider adapters (claude, auggie) execute a locally installed
binary as a one-shot subprocess. The safety contract of that invocation is
identical everywhere and is owned HERE, exactly once:

- wall-clock bound: every invocation carries a timeout (default 300s) so a
  hung CLI can never pin a gateway worker thread indefinitely;
- redaction: stderr passes through ``redact_secret`` before any logging, so
  token material never reaches a log line;
- cause suppression: ``CalledProcessError`` carries raw stderr and argv, so
  the provider-typed error is raised ``from None`` and the cause can never
  ride a traceback into logs or error bodies.

Adapters contribute only their argv (plus an optional child environment) and
parse the returned stdout themselves. Before this module existed the same
contract was copy-pasted per adapter and had silently diverged: the claude
runner had no timeout and re-raised with its cause attached.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable, Mapping, Sequence

from reverso.protocols.auth import redact_secret

logger = logging.getLogger(__name__)

# Shared wall-clock bound for one-shot CLI turns. A timeout surfaces as the
# provider-typed error, never as an unbounded hang.
DEFAULT_CLI_TIMEOUT_SECONDS = 300.0


def run_bounded_cli(
    argv: Sequence[str],
    *,
    error: Callable[[str], Exception],
    cli_label: str,
    failure_message: str | None = None,
    timeout: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
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
    that need inheritance must merge ``os.environ`` themselves. The value is
    never logged.
    """
    message = failure_message or f"{cli_label} invocation failed"
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
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
