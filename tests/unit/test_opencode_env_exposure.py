"""OCG-G3: how the OpenCode Go key enters and leaves the process.

The key's blast radius is the environment, so these tests pin the two boundaries
that matter: where it is allowed IN (Keychain -> proxy env, canonical name only)
and where it must be kept OUT (every spawned CLI, which inherits its parent
environment wholesale and needs no OpenCode credential).
"""

from __future__ import annotations

from reverso.claude_code_sync import LAUNCHER_SCRUB_ENV_KEYS
from reverso.protocols.adapters.opencode.credentials import (
    OCGO_API_KEY_ENV,
    OPENCODE_API_KEY_ENV,
    OPENCODE_KEYCHAIN_SERVICE,
)
from reverso.proxy.main import _KEYCHAIN_KEYS, _inject_keychain_secrets


def test_keychain_maps_the_canonical_variable() -> None:
    assert _KEYCHAIN_KEYS[OPENCODE_API_KEY_ENV] == OPENCODE_KEYCHAIN_SERVICE


def test_the_alias_is_never_a_keychain_target() -> None:
    """Read-only means read-only: nothing may populate the alias."""
    assert OCGO_API_KEY_ENV not in _KEYCHAIN_KEYS


def test_both_names_are_scrubbed_from_spawned_clis() -> None:
    assert OPENCODE_API_KEY_ENV in LAUNCHER_SCRUB_ENV_KEYS
    assert OCGO_API_KEY_ENV in LAUNCHER_SCRUB_ENV_KEYS


def test_preset_key_short_circuits_the_keychain_read(monkeypatch) -> None:
    """CI and tests inject directly; the Keychain must not be consulted."""
    calls: list[str] = []
    monkeypatch.setattr(
        "reverso.proxy.main._load_keychain_secret",
        lambda service: calls.append(service) or "sk-from-keychain",
    )
    monkeypatch.setenv(OPENCODE_API_KEY_ENV, "sk-preset")
    _inject_keychain_secrets()
    assert OPENCODE_KEYCHAIN_SERVICE not in calls


def test_a_preset_alias_suppresses_the_missing_key_warning(monkeypatch, capsys) -> None:
    """An ocgo user who exported only the alias is configured, not broken."""
    monkeypatch.setattr(
        "reverso.proxy.main._load_keychain_secret", lambda service: None
    )
    monkeypatch.delenv(OPENCODE_API_KEY_ENV, raising=False)
    monkeypatch.setenv(OCGO_API_KEY_ENV, "sk-alias")
    _inject_keychain_secrets()
    assert OPENCODE_API_KEY_ENV not in capsys.readouterr().err


def test_a_genuinely_absent_key_still_warns(monkeypatch, capsys) -> None:
    """The suppression above must not silence a real misconfiguration."""
    monkeypatch.setattr(
        "reverso.proxy.main._load_keychain_secret", lambda service: None
    )
    monkeypatch.delenv(OPENCODE_API_KEY_ENV, raising=False)
    monkeypatch.delenv(OCGO_API_KEY_ENV, raising=False)
    _inject_keychain_secrets()
    assert OPENCODE_API_KEY_ENV in capsys.readouterr().err


def test_the_alias_is_not_promoted_into_the_canonical_variable(monkeypatch) -> None:
    """Suppressing the warning must not mean writing the canonical name."""
    monkeypatch.setattr(
        "reverso.proxy.main._load_keychain_secret", lambda service: None
    )
    monkeypatch.delenv(OPENCODE_API_KEY_ENV, raising=False)
    monkeypatch.setenv(OCGO_API_KEY_ENV, "sk-alias")
    _inject_keychain_secrets()
    import os

    assert OPENCODE_API_KEY_ENV not in os.environ
