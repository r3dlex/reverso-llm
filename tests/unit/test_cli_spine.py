"""Unit tests for the bounded CLI spine (ADR 0005).

The spine is the single owner of the one-shot subprocess safety contract for
CLI-backed provider adapters: wall-clock bound, stderr redaction before
logging, and cause suppression on nonzero exit. The full error-mode matrix is
tested HERE, once, through the spine's public interface; adapter tests only
verify their argv and stdout parsing.
"""

from __future__ import annotations

import sys

import pytest

from reverso.protocols.adapters.cli_spine import (
    DEFAULT_CLI_TIMEOUT_SECONDS,
    run_bounded_cli,
)


class _SpineError(RuntimeError):
    """Provider-typed stand-in for AuggieError / ClaudeAuthError."""


def test_success_returns_stdout() -> None:
    stdout = run_bounded_cli(
        [sys.executable, "-c", "print('spine-ok')"],
        error=_SpineError,
        cli_label="fake CLI",
    )
    assert stdout == "spine-ok\n"


def test_env_replaces_child_environment() -> None:
    stdout = run_bounded_cli(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('SPINE_TEST_VAR', 'missing'))",
        ],
        error=_SpineError,
        cli_label="fake CLI",
        env={"SPINE_TEST_VAR": "hello"},
    )
    assert stdout == "hello\n"


def test_missing_binary_raises_provider_error() -> None:
    with pytest.raises(_SpineError) as excinfo:
        run_bounded_cli(
            ["reverso-definitely-missing-binary"],
            error=_SpineError,
            cli_label="fake CLI",
        )
    assert str(excinfo.value) == "fake CLI not found on PATH"


def test_timeout_is_bounded_and_raises_provider_error() -> None:
    with pytest.raises(_SpineError) as excinfo:
        run_bounded_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            error=_SpineError,
            cli_label="fake CLI",
            timeout=0.2,
        )
    assert str(excinfo.value) == "fake CLI timed out"


def test_default_timeout_is_the_shared_bound() -> None:
    # The bound itself is part of the interface: no caller may run unbounded.
    assert DEFAULT_CLI_TIMEOUT_SECONDS == 300.0


def test_nonzero_exit_suppresses_cause_and_never_leaks_stderr() -> None:
    secret = "token=SECRET-spine-leak-1234567890"
    with pytest.raises(_SpineError) as excinfo:
        run_bounded_cli(
            [
                sys.executable,
                "-c",
                f"import sys; print({secret!r}, file=sys.stderr); sys.exit(3)",
            ],
            error=_SpineError,
            cli_label="fake CLI",
        )
    assert str(excinfo.value) == "fake CLI invocation failed"
    # The CalledProcessError cause (raw stderr/argv) must never ride a
    # traceback: cause suppressed, context display suppressed.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "SECRET" not in str(excinfo.value)


def test_nonzero_exit_uses_custom_failure_message() -> None:
    with pytest.raises(_SpineError) as excinfo:
        run_bounded_cli(
            [sys.executable, "-c", "import sys; sys.exit(2)"],
            error=_SpineError,
            cli_label="fake CLI",
            failure_message="fake model list failed",
        )
    assert str(excinfo.value) == "fake model list failed"


def test_nonzero_exit_logs_redacted_stderr_only(caplog) -> None:
    secret = "token=SECRET-spine-log-1234567890"
    with caplog.at_level("WARNING", logger="reverso.protocols.adapters.cli_spine"):
        with pytest.raises(_SpineError):
            run_bounded_cli(
                [
                    sys.executable,
                    "-c",
                    f"import sys; print({secret!r}, file=sys.stderr); sys.exit(3)",
                ],
                error=_SpineError,
                cli_label="fake CLI",
            )
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "fake CLI failed (rc=3)" in joined
    assert "SECRET-spine-log" not in joined
