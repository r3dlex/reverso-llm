"""OpenCode Go credential resolution (OCG-G3).

The subscription key is a static ``sk-opencode-`` style token; there is no OAuth
flow to drive. It reaches the process through the environment, populated at proxy
startup from the Keychain item ``reverso/OPENCODE_API_KEY``.

Two rules exist because of how the key is exposed rather than how it is used:

``OCGO_API_KEY`` is accepted as a READ-ONLY alias, resolved second. Users
arriving from the ``ocgo`` proxy already export it, and silently ignoring it
would look like a broken credential. Nothing in Reverso ever writes it, so the
alias cannot become a second source of truth.

The key is never logged, never interpolated into an error message, and is
scrubbed from the environment of every spawned CLI: no child process needs it,
and a launched agent inherits its parent environment wholesale.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = [
    "OCGO_API_KEY_ENV",
    "OPENCODE_API_KEY_ENV",
    "OPENCODE_KEYCHAIN_SERVICE",
    "OpenCodeCredentialError",
    "require_api_key",
    "resolve_api_key",
]

OPENCODE_API_KEY_ENV = "OPENCODE_API_KEY"
OCGO_API_KEY_ENV = "OCGO_API_KEY"
OPENCODE_KEYCHAIN_SERVICE = "reverso/OPENCODE_API_KEY"

# Canonical first, alias second. Order is the whole contract.
_LOOKUP_ORDER: tuple[str, ...] = (OPENCODE_API_KEY_ENV, OCGO_API_KEY_ENV)


class OpenCodeCredentialError(RuntimeError):
    """No usable OpenCode Go credential is present.

    Raised instead of returning a partial or placeholder key so the caller fails
    closed with a 503 rather than issuing an unauthenticated upstream request.
    """


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Return the OpenCode Go key, or ``None`` when none is usable.

    A variable set to blank or whitespace is treated as ABSENT rather than as a
    credential, and falls through to the next candidate: an empty export is a
    deployment mistake, and letting it mask a working alias would turn that
    mistake into a confusing 401 from upstream.
    """
    source = os.environ if env is None else env
    for name in _LOOKUP_ORDER:
        candidate = (source.get(name) or "").strip()
        if candidate:
            return candidate
    return None


def require_api_key(env: Mapping[str, str] | None = None) -> str:
    """Return the key or raise. The message never echoes any candidate value."""
    key = resolve_api_key(env)
    if key is None:
        raise OpenCodeCredentialError(
            f"no OpenCode Go credential: set {OPENCODE_API_KEY_ENV} "
            f"(or the read-only alias {OCGO_API_KEY_ENV}), or store it in the "
            f"Keychain as '{OPENCODE_KEYCHAIN_SERVICE}'"
        )
    return key
