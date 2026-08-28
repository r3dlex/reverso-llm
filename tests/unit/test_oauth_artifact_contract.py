"""Shared OAuth artifact auth contract (reverso-oauth-auth-dedup).

Single-homes the invariants that three adapters previously each asserted by
hand (``ClaudeOAuthAuth``, ``CodexOAuthAuth``, ``KimiOAuthAuth``): the local
credential artifact is read keychain first and the credentials file second and
never from the environment; an expired artifact fails closed; a malformed or
non-dict artifact never authenticates or crashes; and token material never
reaches the resolution summary, a raised error, or a log record.

``ClaudeOAuthAuth`` and ``CodexOAuthAuth`` are thin subclasses of the shared
``LocalOAuthArtifactAuth``, so the never-env-fallback, keychain-first, and
non-dict guards are proven once against the shared implementation and hold for
both adapters. ``KimiOAuthAuth`` reuses the shared ``read_artifact_file``
reader and layers a deliberate, documented ``KIMI_BEARER_TOKEN`` fallback for
non-CLI deployments on top; its legs below pin that exception explicitly (so it
cannot silently regress) and prove the shared-reader routing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from reverso.protocols.adapters.claude import ClaudeOAuthAuth
from reverso.protocols.adapters.codex import CodexOAuthAuth
from reverso.protocols.adapters.kimi import KimiError, KimiOAuthAuth
from reverso.protocols.adapters.oauth_artifact import (
    ArtifactState,
    LocalOAuthArtifactAuth,
    read_artifact_file,
)

# SYNTHETIC secret material, one distinct sentinel per adapter so a cross-adapter
# leak is detectable. These never appear in any real credential store.
_ACCESS_SECRETS = {
    "claude": "claude-access-SECRET-7f3a9c1e2b",
    "codex": "codex-access-SECRET-9d8e7f6a5b",
    "kimi": "kimi-access-SECRET-1a2b3c4d5e",
}
_REFRESH_SECRETS = {
    "claude": "claude-refresh-SECRET-2b3c4d5e6f",
    "codex": "codex-refresh-SECRET-3c4d5e6f7a",
    "kimi": "kimi-refresh-SECRET-4d5e6f7a8b",
}

# The env vars each adapter must NOT consume for artifact authentication, per
# the module-level _FORBIDDEN_AUTH_ENV declarations in claude.py and codex.py.
_FORBIDDEN_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"),
    # kimi has no forbidden-env declaration: KIMI_BEARER_TOKEN is a documented,
    # deliberate bearer fallback for non-CLI deployments (pinned below), not a
    # metered key the artifact gate must reject.
    "kimi": (),
}

_MISSING_REASON = {
    "claude": "no_claude_oauth_artifact",
    "codex": "no_codex_oauth_artifact",
}


def _future_ms() -> int:
    return int((time.time() + 3600) * 1000)


def _past_ms() -> int:
    return int((time.time() - 3600) * 1000)


def _future_s() -> int:
    return int(time.time() + 3600)


def _past_s() -> int:
    return int(time.time() - 3600)


def _codex_jwt(exp_seconds: int | None) -> str:
    """Build a SYNTHETIC 3-segment JWT carrying an ``exp`` claim (epoch s)."""

    def _seg(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "RS256", "typ": "JWT"})
    payload = _seg({"exp": exp_seconds, "sub": "synthetic-subject"})
    return f"{header}.{payload}.synthetic-signature-not-secret"


def _claude_artifact(expires_at: int | None) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": _ACCESS_SECRETS["claude"],
                "refreshToken": _REFRESH_SECRETS["claude"],
                "expiresAt": expires_at,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "default",
            }
        }
    )


def _codex_artifact(*, exp_seconds: int | None) -> str:
    body: dict[str, Any] = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-06-21T00:00:00Z",
        "tokens": {
            "id_token": "codex-id-SECRET-synthetic",
            "access_token": (
                _codex_jwt(exp_seconds) if exp_seconds is not None else "opaque"
            ),
            "refresh_token": _REFRESH_SECRETS["codex"],
            "account_id": "acct-synthetic-123",
        },
    }
    return json.dumps(body)


def _kimi_artifact(*, access_token: str, expires_at: float | None) -> str:
    """Build a kimi artifact with NO refresh token so no refresh flow runs."""
    body: dict[str, Any] = {"access_token": access_token}
    if expires_at is not None:
        body["expires_at"] = expires_at
    return json.dumps(body)


def _build_claude(
    credentials_path: Path | None, keychain_raw: str | None
) -> ClaudeOAuthAuth:
    return ClaudeOAuthAuth(
        credentials_path=credentials_path,
        keychain_reader=(lambda: keychain_raw),
    )


def _build_codex(
    credentials_path: Path | None, keychain_raw: str | None
) -> CodexOAuthAuth:
    return CodexOAuthAuth(
        credentials_path=credentials_path,
        keychain_reader=(lambda: keychain_raw),
    )


def _build_kimi(
    credentials_path: Path | None, keychain_raw: str | None
) -> KimiOAuthAuth:
    return KimiOAuthAuth(credentials_path=credentials_path)


_SPECS: dict[str, Any] = {
    "claude": _build_claude,
    "codex": _build_codex,
    "kimi": _build_kimi,
}


def test_adapters_delegate_to_shared_implementation() -> None:
    """claude/codex auth objects are thin subclasses of the shared class."""
    for build in (_build_claude, _build_codex):
        auth = build(None, None)
        assert isinstance(auth, LocalOAuthArtifactAuth)


def test_env_only_token_never_authenticates(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A token present ONLY in the env must never authenticate the gate.

    For claude and codex the invariant is enforced once, inside the shared
    LocalOAuthArtifactAuth: no code path consults any environment token, so with
    the adapter's forbidden env vars set and NO artifact anywhere, resolve()
    stays unauthenticated on the OAuth method (never an api-key method).

    Kimi is pinned separately (test_kimi_bearer_env_fallback_is_documented): its
    KIMI_BEARER_TOKEN is a deliberate, documented fallback for non-CLI
    deployments, so the never-env invariant applies to its artifact reader, not
    its resolution policy.
    """
    for name in ("claude", "codex"):
        for var in _FORBIDDEN_ENV[name]:
            monkeypatch.setenv(var, f"{name}-env-SECRET-should-be-ignored")

        auth = _SPECS[name](tmp_path / "missing-credentials.json", None)
        resolution = auth.resolve()

        assert resolution.authenticated is False
        assert resolution.method in ("claude_oauth", "codex_oauth")
        assert resolution.method not in {"anthropic", "openai", "api_key", "api-key"}
        assert resolution.details.get("reason") == _MISSING_REASON[name]


def test_kimi_bearer_env_fallback_is_documented(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Kimi's KIMI_BEARER_TOKEN fallback is deliberate and stays non-secret.

    This is the deliberate exception to the never-env-fallback invariant: the
    env var carries the OAuth bearer for non-CLI deployments (not a metered API
    key), so it remains pinned here. With NO artifact and NO login coordinator,
    the env bearer is the only available credential and is returned as-is.
    """
    sentinel = "kimi-env-bearer-SECRET-documented-fallback"
    monkeypatch.setenv("KIMI_BEARER_TOKEN", sentinel)

    auth = _build_kimi(tmp_path / "missing-credentials.json", None)
    token = asyncio.run(auth.resolve_existing_bearer_token())

    assert token == sentinel


def test_expired_artifact_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An expired artifact must not authenticate, for every adapter at once."""
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    artifacts = {
        "claude": _claude_artifact(expires_at=_past_ms()),
        "codex": _codex_artifact(exp_seconds=_past_s()),
        "kimi": _kimi_artifact(
            access_token=_ACCESS_SECRETS["kimi"], expires_at=float(_past_s())
        ),
    }

    for name, raw in artifacts.items():
        cred_file = tmp_path / f"{name}-credentials.json"
        cred_file.write_text(raw, encoding="utf-8")
        auth = _SPECS[name](cred_file, None)

        if name == "kimi":
            with pytest.raises(KimiError):
                asyncio.run(auth.resolve_existing_bearer_token())
            continue

        resolution = auth.resolve()
        assert resolution.authenticated is False, name
        assert resolution.details.get("reason") == "expired", name


def test_keychain_precedes_credentials_file(tmp_path) -> None:
    """Both sources present: the keychain bundle is the one that authenticates."""
    file_token = "file-source-token-NOT-the-resolved-one"
    artifacts = {
        "claude": (
            _claude_artifact(expires_at=_future_ms()),
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": file_token,
                        "refreshToken": "r",
                        "expiresAt": _future_ms(),
                    }
                }
            ),
        ),
        "codex": (
            _codex_artifact(exp_seconds=_future_s()),
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": file_token,
                        "refresh_token": "r",
                        "account_id": "a",
                    },
                }
            ),
        ),
    }

    for name, (keychain_raw, file_raw) in artifacts.items():
        cred_file = tmp_path / f"{name}-auth.json"
        cred_file.write_text(file_raw, encoding="utf-8")
        # A valid keychain raw shadows the file source with a DIFFERENT token.
        auth = _SPECS[name](cred_file, keychain_raw)

        resolution = auth.resolve()
        assert resolution.authenticated is True, name
        assert resolution.details.get("source") == "keychain", name
        token = asyncio.run(auth.bearer_token())
        assert file_token not in token, name


def test_invalid_keychain_falls_through_to_credentials_file(tmp_path) -> None:
    """Garbage on the keychain source is skipped; the file source resolves."""
    for name in ("claude", "codex"):
        cred_file = tmp_path / f"{name}-auth.json"
        cred_file.write_text(
            _claude_artifact(expires_at=_future_ms())
            if name == "claude"
            else _codex_artifact(exp_seconds=_future_s()),
            encoding="utf-8",
        )
        auth = _SPECS[name](cred_file, "not-valid-json!!!")
        resolution = auth.resolve()
        assert resolution.authenticated is True, name
        assert resolution.details.get("source") == "credentials_file", name


def test_non_dict_artifact_never_authenticates_or_crashes(tmp_path) -> None:
    """A valid-JSON non-dict artifact is skipped, never crashes, never auths.

    The claude copy of the reader lacked the isinstance-dict guard codex had:
    a JSON list on the keychain source raised AttributeError out of resolve().
    The shared reader guards both adapters against it.
    """
    for name in ("claude", "codex"):
        sneaky = json.dumps([{"accessToken": "sneaky-SECRET-list-token"}])
        auth = _SPECS[name](tmp_path / "missing-credentials.json", sneaky)

        resolution = auth.resolve()
        assert resolution.authenticated is False, name
        assert resolution.details.get("reason") == _MISSING_REASON[name], name


def test_token_material_never_reaches_redaction_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
) -> None:
    """No token material in resolutions, raised errors, or any log record."""
    monkeypatch.delenv("KIMI_BEARER_TOKEN", raising=False)
    with caplog.at_level(logging.DEBUG):
        # claude + codex: a full resolution carries no secret material.
        artifacts = {
            "claude": _claude_artifact(expires_at=_future_ms()),
            "codex": _codex_artifact(exp_seconds=_future_s()),
        }
        for name, raw in artifacts.items():
            cred_file = tmp_path / f"{name}-credentials.json"
            cred_file.write_text(raw, encoding="utf-8")
            resolution = _SPECS[name](cred_file, None).resolve()
            serialized = json.dumps(
                {
                    "authenticated": resolution.authenticated,
                    "method": resolution.method,
                    "subscription_type": resolution.subscription_type,
                    "details": resolution.details,
                }
            )
            for secret in (_ACCESS_SECRETS[name], _REFRESH_SECRETS[name]):
                assert secret not in serialized, name

        # A malformed keychain artifact logs a warning that carries no material.
        for name in ("claude", "codex"):
            leaking_raw = json.dumps(
                {"claudeAiOauth": {"accessToken": _ACCESS_SECRETS[name]}}
            )[:-1]
            auth = _SPECS[name](tmp_path / "missing.json", leaking_raw)
            resolution = auth.resolve()
            assert resolution.authenticated is False, name

        # kimi: the raised error never carries artifact material.
        kimi_file = tmp_path / "kimi-credentials.json"
        kimi_file.write_text(
            _kimi_artifact(
                access_token=_ACCESS_SECRETS["kimi"], expires_at=float(_past_s())
            ),
            encoding="utf-8",
        )
        kimi_auth = _build_kimi(kimi_file, None)
        try:
            asyncio.run(kimi_auth.resolve_existing_bearer_token())
        except KimiError as exc:
            assert _ACCESS_SECRETS["kimi"] not in str(exc)

    for record in caplog.records:
        message = record.getMessage()
        for secret in (*_ACCESS_SECRETS.values(), *_REFRESH_SECRETS.values()):
            assert secret not in message


def test_kimi_reads_artifact_through_shared_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """KimiOAuthAuth routes its artifact read through the shared reader.

    Pins the single-homing itself: kimi must not carry a private copy of the
    local artifact reading; its reads go through the shared read_artifact_file.
    """
    calls: list[Path] = []

    def _spy(path: Path) -> tuple[ArtifactState, dict[str, Any] | None]:
        calls.append(path)
        payload = {
            "access_token": _ACCESS_SECRETS["kimi"],
            "expires_at": float(_future_s()),
        }
        return ArtifactState.LOADED, payload

    monkeypatch.setattr("reverso.protocols.adapters.kimi.read_artifact_file", _spy)
    auth = _build_kimi(tmp_path / "any.json", None)
    token = asyncio.run(auth.resolve_existing_bearer_token())

    assert calls, "kimi must read its artifact through the shared reader"
    assert token == _ACCESS_SECRETS["kimi"]


def test_shared_reader_file_contract(tmp_path) -> None:
    """The shared file reader exposes the absent/malformed/loaded tri-state."""
    state, payload = read_artifact_file(tmp_path / "does-not-exist.json")
    assert state is ArtifactState.ABSENT and payload is None

    malformed_file = tmp_path / "malformed.json"
    malformed_file.write_text("{not json", encoding="utf-8")
    state, payload = read_artifact_file(malformed_file)
    assert state is ArtifactState.MALFORMED and payload is None

    non_dict_file = tmp_path / "non-dict.json"
    non_dict_file.write_text("[1, 2, 3]", encoding="utf-8")
    state, payload = read_artifact_file(non_dict_file)
    assert state is ArtifactState.MALFORMED and payload is None

    loaded_file = tmp_path / "loaded.json"
    loaded_file.write_text('{"access_token": "t"}', encoding="utf-8")
    state, payload = read_artifact_file(loaded_file)
    assert state is ArtifactState.LOADED and payload == {"access_token": "t"}


def test_shared_reader_propagates_unexpected_os_errors(tmp_path) -> None:
    """Read failures that are not absence (e.g. a directory) propagate."""
    directory_path = tmp_path / "a-directory"
    directory_path.mkdir()
    with pytest.raises(OSError):
        read_artifact_file(directory_path)
