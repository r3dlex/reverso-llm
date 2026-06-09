"""Provider auth interface for local OAuth / CLI-auth credentials (ADR 0002 11.4).

Adapters resolve credentials from the local logged-in user (Claude subscription
OAuth via the ``claudeAiOauth`` artifact, Copilot via local CLI credentials).
This module defines the resolution shape, a secret-redaction helper that the
whole gateway logs through, and a deterministic fake-auth seam for tests. It
holds NO repository secrets and never logs token material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_REDACTED = "***redacted***"
_SECRET_FIELD_HINTS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "apikey",
    "api_key",
    "accesstoken",
    "refreshtoken",
)


@dataclass
class AuthResolution:
    """The outcome of resolving provider credentials.

    ``method`` names the resolved auth path (for example ``claude_oauth`` or
    ``copilot_cli``) so the falsifiable Claude gate can assert the OAuth path
    was used rather than a metered API key. ``subscription_type`` mirrors
    ``claudeAiOauth.subscriptionType`` when present. ``authenticated`` is the
    boolean gate. ``details`` carries non-secret diagnostics only; secret
    material must never be placed here.
    """

    authenticated: bool
    method: str
    subscription_type: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class ProviderAuth(Protocol):
    """Provider credential resolver backed by local OAuth / CLI auth.

    Implementations read local credential artifacts (Keychain,
    ``~/.claude/.credentials.json``, ``~/.config/github-copilot``) and never a
    repository-stored secret. ``resolve`` is the gate; ``bearer_token`` returns
    the live token for upstream calls but its result must always be passed
    through ``redact_secret`` before logging.
    """

    def resolve(self) -> AuthResolution:
        """Resolve credentials and return a non-secret resolution summary."""
        ...

    async def bearer_token(self) -> str:
        """Return the current bearer token (auto-refreshed). Never log raw."""
        ...


def redact_secret(value: str | None, *, keep: int = 0) -> str:
    """Redact a secret string for safe logging.

    Returns a fixed placeholder, optionally preserving the last ``keep``
    characters so logs can correlate without exposing token material. Never
    returns enough of the value to be reusable.
    """
    if not value:
        return _REDACTED
    keep = max(0, min(keep, 4))
    if keep and len(value) > keep:
        return _REDACTED + value[-keep:]
    return _REDACTED


def redact_mapping(data: dict) -> dict:
    """Return a shallow copy of ``data`` with secret-looking fields redacted.

    Field names are matched case-insensitively against known secret hints so
    structured log payloads can be emitted without leaking credentials.
    """
    redacted: dict = {}
    for key, value in data.items():
        normalized = str(key).lower().replace("-", "").replace("_", "")
        if any(hint.replace("_", "") in normalized for hint in _SECRET_FIELD_HINTS):
            redacted[key] = redact_secret(value if isinstance(value, str) else None)
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value)
        else:
            redacted[key] = value
    return redacted


class FakeAuth:
    """Deterministic in-process auth seam for tests (no real credentials).

    Tests construct this with an explicit resolution and token so the parity
    harness and adapter tests can exercise authenticated and unauthenticated
    paths without touching the Keychain, the filesystem, or the network.
    """

    def __init__(
        self,
        resolution: AuthResolution,
        token: str = "fake-token",
    ) -> None:
        self._resolution = resolution
        self._token = token

    def resolve(self) -> AuthResolution:
        return self._resolution

    async def bearer_token(self) -> str:
        return self._token


def fake_auth(
    *,
    authenticated: bool = True,
    method: str = "fake",
    subscription_type: str | None = None,
    token: str = "fake-token",
) -> FakeAuth:
    """Build a deterministic FakeAuth for tests."""
    return FakeAuth(
        AuthResolution(
            authenticated=authenticated,
            method=method,
            subscription_type=subscription_type,
        ),
        token=token,
    )
