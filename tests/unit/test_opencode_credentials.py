"""OCG-G3: OpenCode Go credential resolution.

The key is resolved from the environment only. ``OPENCODE_API_KEY`` is the
canonical variable; ``OCGO_API_KEY`` is a read-only alias resolved SECOND so an
``ocgo`` user's existing export keeps working. Nothing here ever writes the
alias, and no code path logs the key.
"""

from __future__ import annotations

import pytest

from reverso.protocols.adapters.opencode.credentials import (
    OCGO_API_KEY_ENV,
    OPENCODE_API_KEY_ENV,
    OpenCodeCredentialError,
    require_api_key,
    resolve_api_key,
)


def test_canonical_variable_resolves() -> None:
    assert resolve_api_key({OPENCODE_API_KEY_ENV: "sk-canonical"}) == "sk-canonical"


def test_alias_resolves_when_canonical_absent() -> None:
    assert resolve_api_key({OCGO_API_KEY_ENV: "sk-alias"}) == "sk-alias"


def test_canonical_wins_over_alias() -> None:
    env = {OPENCODE_API_KEY_ENV: "sk-canonical", OCGO_API_KEY_ENV: "sk-alias"}
    assert resolve_api_key(env) == "sk-canonical"


def test_absent_key_resolves_to_none() -> None:
    assert resolve_api_key({}) is None


def test_blank_and_whitespace_are_not_credentials() -> None:
    assert resolve_api_key({OPENCODE_API_KEY_ENV: ""}) is None
    assert resolve_api_key({OPENCODE_API_KEY_ENV: "   "}) is None


def test_blank_canonical_falls_through_to_alias() -> None:
    """A blank canonical export must not mask a usable alias."""
    env = {OPENCODE_API_KEY_ENV: "", OCGO_API_KEY_ENV: "sk-alias"}
    assert resolve_api_key(env) == "sk-alias"


def test_surrounding_whitespace_is_stripped() -> None:
    assert resolve_api_key({OPENCODE_API_KEY_ENV: " sk-padded\n"}) == "sk-padded"


def test_require_api_key_raises_when_absent() -> None:
    with pytest.raises(OpenCodeCredentialError) as excinfo:
        require_api_key({})
    assert OPENCODE_API_KEY_ENV in str(excinfo.value)


def test_require_api_key_error_never_contains_a_key() -> None:
    """The raise path must not echo a partial credential."""
    with pytest.raises(OpenCodeCredentialError) as excinfo:
        require_api_key({OPENCODE_API_KEY_ENV: "   "})
    assert "sk-" not in str(excinfo.value)
