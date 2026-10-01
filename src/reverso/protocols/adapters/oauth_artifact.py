"""Shared local OAuth credential-artifact reading.

One deep module behind the ProviderAuth seam (ADR 0002 11.4): the local
credential artifact is read keychain first and the credentials file second,
and the token bundle is asserted live and unexpired. ClaudeOAuthAuth and
CodexOAuthAuth are thin subclasses; KimiOAuthAuth reuses the file reader and
keeps its own refresh logic.

Invariants enforced here, once:
  - local storage only: no code path consults any environment token;
  - keychain first, then the credentials file;
  - token material is never logged: diagnostics carry non-secret labels only.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from reverso.protocols.auth import AuthResolution

logger = logging.getLogger(__name__)


class ArtifactState(Enum):
    """Outcome of one local artifact read (absent, malformed, or loaded)."""

    ABSENT = "absent"
    MALFORMED = "malformed"
    LOADED = "loaded"


def is_expired(expires_at: Any) -> bool:
    """Return True when an observable expiry (epoch ms) has passed.

    When ``expires_at`` is None (or unparseable) the expiry is not observable,
    so this returns False and the caller treats the token as live; a real
    upstream call would surface the failure.
    """
    if expires_at is None:
        return False
    try:
        expiry_ms = float(expires_at)
    except (TypeError, ValueError):
        return False
    return expiry_ms <= time.time() * 1000.0


def read_artifact_file(path: Path) -> tuple[ArtifactState, dict[str, Any] | None]:
    """Read and parse one local credentials file into the shared tri-state.

    Returns ABSENT for a missing file, MALFORMED for an unreadable-shape or
    non-dict JSON body, and LOADED with the parsed dict otherwise. Other OS
    errors (permission, I/O) propagate so callers keep their own error policy:
    kimi maps them to its secret-free KimiError, and the shared auth class
    treats them as an absent source.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ArtifactState.ABSENT, None
    except UnicodeDecodeError:
        return ArtifactState.MALFORMED, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ArtifactState.MALFORMED, None
    if not isinstance(payload, dict):
        return ArtifactState.MALFORMED, None
    return ArtifactState.LOADED, payload


def read_keychain(service: str) -> str | None:
    """Read the raw credential JSON from the macOS Keychain via `security`."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


@dataclass(frozen=True)
class TokenOutcome:
    """Provider token extraction outcome (non-secret).

    ``token`` is the extracted access token (falsy when absent, which the gate
    reports as ``no_access_token``). ``reason`` overrides the default missing
    reason for a provider-specific intermediate guard (codex's missing
    ``tokens`` object). ``subscription_type`` mirrors the provider's
    subscription field when the bundle supports one.
    """

    token: Any = None
    reason: str | None = None
    subscription_type: str | None = None


class LocalOAuthArtifactAuth:
    """Resolve subscription credentials from a local OAuth artifact.

    One deep module behind the ProviderAuth seam (ADR 0002 11.4): reads the
    macOS Keychain generic-password item first, then the local credentials
    file, parses the JSON artifact, extracts the provider token bundle via
    ``artifact_key``, and asserts a live unexpired access token. It never
    consults any environment token and never logs token material.

    Provider differences (JSON key names, expiry field, summary fields) are
    supplied as small extractor callables so the security invariants (local
    storage only, keychain-first ordering, expiry enforcement, secret-free
    diagnostics) are enforced here, once.
    """

    def __init__(
        self,
        *,
        method: str,
        error: type[Exception],
        label: str,
        missing_reason: str,
        credentials_path: Path,
        artifact_key: Callable[[dict[str, Any]], dict[str, Any] | None],
        token_of: Callable[[dict[str, Any]], TokenOutcome],
        expiry_of: Callable[[dict[str, Any]], Any],
        details_of: Callable[[dict[str, Any]], dict[str, object]] | None = None,
        keychain_service: str | None = None,
        keychain_reader: Callable[[], str | None] | None = None,
        bearer_missing_message: str = "no oauth access token available",
    ) -> None:
        self._method = method
        self._error = error
        self._label = label
        self._missing_reason = missing_reason
        self._credentials_path = credentials_path
        self._artifact_key = artifact_key
        self._token_of = token_of
        self._expiry_of = expiry_of
        self._details_of = details_of or (lambda _artifact: {})
        self._keychain_service = keychain_service
        self._keychain_reader = keychain_reader or self._read_keychain
        self._bearer_missing_message = bearer_missing_message

    def _read_keychain(self) -> str | None:
        """Read the keychain source; None when no service is configured."""
        if not self._keychain_service:
            return None
        return read_keychain(self._keychain_service)

    def _read_credentials_file(self) -> str | None:
        """Read the credentials-file source; None when it cannot be read."""
        try:
            return self._credentials_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None

    def _artifact_sources(self) -> Iterator[tuple[str, Callable[[], str | None]]]:
        """Yield (source label, raw reader) pairs, keychain first, file second."""
        if self._keychain_reader is not None or self._keychain_service:
            yield "keychain", self._keychain_reader
        yield "credentials_file", self._read_credentials_file

    def _load_artifact(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return (token bundle, source) read DIRECTLY from local storage.

        Tries the keychain first, then the credentials file. Neither path
        consults any environment token. Malformed sources are skipped with a
        warning that carries the source label only, never artifact content.
        ``source`` is a non-secret diagnostic label.
        """
        for source, raw_reader in self._artifact_sources():
            raw = raw_reader()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "%s oauth artifact from %s was not valid JSON",
                    self._label,
                    source,
                )
                continue
            if not isinstance(parsed, dict):
                # A valid-JSON non-dict artifact (e.g. a list) carries no
                # extractable bundle; skip it instead of crashing extractors.
                continue
            bundle = self._artifact_key(parsed)
            if isinstance(bundle, dict):
                return bundle, source
        return None, None

    def resolve(self) -> AuthResolution:
        """Resolve the OAuth credential and return a non-secret summary.

        The ``method`` is ALWAYS the OAuth path; there is no api-key code path.
        ``authenticated`` is True only when a live access token is present in
        the artifact (and, when observable, not expired). Diagnostics carry
        non-secret labels only.
        """
        artifact, source = self._load_artifact()
        if artifact is None:
            return AuthResolution(
                authenticated=False,
                method=self._method,
                details={"reason": self._missing_reason},
            )

        outcome = self._token_of(artifact)
        if outcome.reason is not None or not outcome.token:
            return AuthResolution(
                authenticated=False,
                method=self._method,
                subscription_type=outcome.subscription_type,
                details={
                    "reason": outcome.reason or "no_access_token",
                    "source": source,
                },
            )

        expires_at = self._expiry_of(artifact)
        details: dict[str, object] = {"source": source, **self._details_of(artifact)}
        if is_expired(expires_at):
            details["reason"] = "expired"
            details["expires_at"] = expires_at
            return AuthResolution(
                authenticated=False,
                method=self._method,
                subscription_type=outcome.subscription_type,
                details=details,
            )

        if expires_at is not None:
            details["expires_at"] = expires_at
        return AuthResolution(
            authenticated=True,
            method=self._method,
            subscription_type=outcome.subscription_type,
            details=details,
        )

    async def bearer_token(self) -> str:
        """Return the live OAuth access token. NEVER log the raw return value."""
        artifact, _ = self._load_artifact()
        if not artifact:
            raise self._error(self._bearer_missing_message)
        outcome = self._token_of(artifact)
        if not outcome.token:
            raise self._error(self._bearer_missing_message)
        return str(outcome.token)
