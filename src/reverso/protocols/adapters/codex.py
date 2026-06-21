"""Codex provider auth with a falsifiable ChatGPT-subscription OAuth gate (ADR 0007).

This module hosts the Codex backend that serves gpt-* models on the inbound
Anthropic Messages surface (ADR 0006, ADR 0007). Milestone 2 / STORY G002 ships
the authentication layer only: ``CodexOAuthAuth``. The ``CodexAdapter`` that USES
this resolver is G003 and is intentionally NOT in this module yet; this module is
importable standalone and free of any legacy-app or litellm import.

G002.0 spike findings (recorded inline per the plan; verified on this machine
2026-06-21):
  - Artifact location: ``~/.codex/auth.json`` exists (mode 0600), written by
    ``codex login``. No macOS Keychain generic-password entry was found for any
    plausible service name (codex / openai / chatgpt / Codex CLI), so the file is
    the sole local credential store on this machine. ``~/.config/codex/`` does not
    exist (the CLI uses ``CODEX_HOME``, defaulting to ``~/.codex``).
  - Artifact shape (FIELD NAMES only, never values):
      ``auth_mode`` (str), ``OPENAI_API_KEY`` (null when on the subscription),
      ``tokens`` (object: ``id_token``, ``access_token``, ``refresh_token``,
      ``account_id``), ``last_refresh`` (ISO-8601 str).
    There is NO top-level numeric expiry field. The ``access_token`` is a 3-segment
    JWT whose payload carries a standard ``exp`` claim (epoch SECONDS); that is the
    observable expiry used by the gate (decoded locally, signature NOT verified, as
    this is a pre-flight validity check, not an authorization decision).
  - Injection env var: ``codex exec --help`` exposes NO token / oauth / bearer
    injection environment variable analogous to ``CLAUDE_CODE_OAUTH_TOKEN``. The
    ``--with-access-token`` flag belongs to ``codex login`` (it writes the stored
    session from stdin), and ``--remote-auth-token-env`` belongs to the remote-app
    login subcommand, NOT to ``codex exec``. The legacy provider corroborates this:
    ``openai_cli_provider._invoke_codex`` spawns ``codex exec`` with NO ``env=``
    argument (``src/reverso/proxy/openai_cli_provider.py``), relying on the CLI's
    own stored session.
  - Conclusion: A3 validate-only is the DEFAULT (and the only buildable option on
    this machine). ``CodexOAuthAuth`` reads and VALIDATES the subscription artifact
    for the pre-flight gate and fails closed on missing / no-token / expired; it
    does NOT inject a bearer into the child env, because ``codex exec`` honors no
    such variable. The Codex CLI authenticates the actual turn from its own
    ``codex login`` session. There is therefore no ``bearer_token`` injection path
    here (A1/A2 remain an OPTIONAL future upgrade gated on a positive injection
    finding). The residual gate/turn divergence risk is bounded by the no-divergence
    coupling test, which requires the ``CodexAdapter`` and is deferred to G003.

Token material is NEVER logged: all diagnostics go through redact_mapping. No
repository secret is read or stored; synthetic fixtures only in tests.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from reverso.protocols.auth import AuthResolution

logger = logging.getLogger(__name__)

# The resolved OAuth method name. The falsifiable gate asserts on this exact
# value; it must never be an api-key path.
OAUTH_METHOD = "codex_oauth"

# Env vars the gate must NOT consume. Reading these to authenticate would defeat
# the ChatGPT-subscription requirement, so they are named here only to assert
# they are never used (the gate test checks the env is left untouched). The
# OPENAI_API_KEY field inside the artifact is likewise never treated as auth.
_FORBIDDEN_AUTH_ENV = ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")

# macOS Keychain service name probed by the spike. No entry was found on this
# machine; kept as a future-proof seam for the optional A1/A2 upgrade. The file
# at ~/.codex/auth.json is the authoritative store today.
_KEYCHAIN_SERVICE = "Codex CLI"
_CODEX_CREDENTIALS_PATH = Path.home() / ".codex" / "auth.json"

# Top-level artifact keys (G002.0 spike). The token bundle lives under "tokens";
# there is no top-level numeric expiry, so expiry is read from the access_token
# JWT "exp" claim.
_TOKENS_KEY = "tokens"
_ACCESS_TOKEN_FIELD = "access_token"
_ACCOUNT_ID_FIELD = "account_id"
_AUTH_MODE_FIELD = "auth_mode"


class CodexAuthError(RuntimeError):
    """Raised when the Codex ChatGPT-subscription OAuth credential cannot resolve."""


class CodexOAuthAuth:
    """Resolve Codex ChatGPT-subscription credentials from the local OAuth artifact.

    Mirrors ``ClaudeOAuthAuth`` but is VALIDATE-ONLY (design point A3, the default
    proven by the G002.0 spike): it reads the ``~/.codex/auth.json`` artifact (and,
    as a future-proof seam, a macOS Keychain item) DIRECTLY and asserts the access
    token is present and not expired. It never falls back to OPENAI_API_KEY or any
    environment token; if the artifact is absent the resolution is simply
    unauthenticated.

    There is deliberately NO ``bearer_token`` method: ``codex exec`` exposes no
    token-injection env var, so the resolved token is never handed to the child.
    The CLI authenticates the turn from its own ``codex login`` session. This keeps
    the gate a falsifiable pre-flight check that asserts the subscription OAuth path
    and never a metered API key.
    """

    def __init__(
        self,
        *,
        keychain_service: str = _KEYCHAIN_SERVICE,
        credentials_path: Path | None = None,
        keychain_reader: Any | None = None,
    ) -> None:
        self._keychain_service = keychain_service
        self._credentials_path = credentials_path or _CODEX_CREDENTIALS_PATH
        # Injectable for tests; defaults to the real macOS Keychain read. The
        # Keychain path returns None on this machine (no entry found) but is kept
        # so the optional A1/A2 upgrade can reuse the same source layering.
        self._keychain_reader = keychain_reader or self._read_keychain

    def _read_keychain(self) -> str | None:
        """Read the raw credential JSON from the macOS Keychain via `security`."""
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    self._keychain_service,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        return result.stdout.strip() or None

    def _read_credentials_file(self) -> str | None:
        """Read the raw credential JSON from ~/.codex/auth.json."""
        try:
            return self._credentials_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None

    def _load_artifact(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return (auth.json dict, source) read DIRECTLY from local storage.

        Tries the Keychain first (future-proof seam), then the credentials file.
        The returned dict is the WHOLE parsed ``auth.json`` object (the token
        bundle lives under its ``tokens`` key). Neither path consults any
        environment token. ``source`` is a non-secret diagnostic label.
        """
        for source, raw in (
            ("keychain", self._keychain_reader()),
            ("credentials_file", self._read_credentials_file()),
        ):
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "codex oauth artifact from %s was not valid JSON", source
                )
                continue
            if isinstance(parsed, dict):
                return parsed, source
        return None, None

    def resolve(self) -> AuthResolution:
        """Resolve the OAuth credential and return a non-secret summary.

        The ``method`` is ALWAYS the OAuth path; this resolver has no api-key code
        path. ``authenticated`` is True only when an access token is present in the
        artifact and (when observable via the JWT ``exp`` claim) not expired.
        """
        artifact, source = self._load_artifact()
        if artifact is None:
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                details={"reason": "no_codex_oauth_artifact"},
            )

        tokens = artifact.get(_TOKENS_KEY)
        if not isinstance(tokens, dict):
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                details={"reason": "no_codex_oauth_tokens", "source": source},
            )

        access_token = tokens.get(_ACCESS_TOKEN_FIELD)
        auth_mode = artifact.get(_AUTH_MODE_FIELD)

        if not access_token:
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                subscription_type=auth_mode,
                details={"reason": "no_access_token", "source": source},
            )

        expires_at = _jwt_exp_ms(access_token)
        details: dict[str, object] = {
            "source": source,
            "account_id": tokens.get(_ACCOUNT_ID_FIELD),
            "auth_mode": auth_mode,
        }

        if _is_expired(expires_at):
            details["reason"] = "expired"
            details["expires_at"] = expires_at
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                subscription_type=auth_mode,
                details=details,
            )

        if expires_at is not None:
            details["expires_at"] = expires_at
        return AuthResolution(
            authenticated=True,
            method=OAUTH_METHOD,
            subscription_type=auth_mode,
            details=details,
        )


def _jwt_exp_ms(access_token: Any) -> int | None:
    """Return the JWT ``exp`` claim in epoch MILLISECONDS, or None if unobservable.

    The Codex ``access_token`` is a 3-segment JWT (G002.0 spike). The payload
    carries a standard ``exp`` claim in epoch SECONDS. The signature is NOT
    verified: this is a local pre-flight validity check, not an authorization
    decision. Any malformed token yields None (expiry not observable), so the
    caller treats the token as live and a real CLI turn would surface the failure.
    """
    if not isinstance(access_token, str):
        return None
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    try:
        ms = float(exp) * 1000.0
        # A non-finite exp (inf, nan from a crafted JWT) is not observable as a
        # real expiry; treat it as live so the caller propagates to the real CLI.
        if not (ms > float("-inf") and ms < float("inf")):
            return None
        return int(ms)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: e.g. exp=1e400 -> float('inf') -> int(inf) raises
        # OverflowError (an ArithmeticError, NOT a ValueError). Non-finite/
        # overflowing exp is treated as unobservable, not a gate failure.
        return None


def _is_expired(expires_at: Any) -> bool:
    """Return True when an observable expiry (epoch ms) has passed.

    When ``expires_at`` is None the expiry is not observable, so this returns
    False and the caller treats the token as live (a real upstream call would
    surface the failure).
    """
    if expires_at is None:
        return False
    try:
        expiry_ms = float(expires_at)
    except (TypeError, ValueError):
        return False
    return expiry_ms <= time.time() * 1000.0
