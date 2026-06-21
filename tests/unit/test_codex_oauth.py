"""Falsifiable ChatGPT-subscription OAuth gate for the Codex auth layer (ADR 0007).

STORY G002 ships the auth resolver only (``CodexOAuthAuth``). These tests assert
the gate reads the ``~/.codex/auth.json`` artifact DIRECTLY and fails CLOSED on a
missing, token-less, or expired artifact, that it resolves the OAuth method (never
a metered api-key path), and that token material never leaks into the resolution
or logs. All fixtures are SYNTHETIC: the JWT is hand-built with a fake payload and
a non-secret signature segment; no real credential is ever read.

The no-divergence coupling test (valid artifact + failing CLI session -> structured
Anthropic error) requires the CodexAdapter and is DEFERRED to G003 (see the plan
.omc/plans/ralplan-codex-anthropic-oauth.md, Section 4 Unit / pre-mortem 1).
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import time

from reverso.protocols.adapters.codex import (
    OAUTH_METHOD,
    CodexOAuthAuth,
)

# SYNTHETIC secret material. These never appear in any real credential store;
# the leak tests assert these literals are absent from resolution details / logs.
_FAKE_ACCESS_SECRET = "codex-access-SECRET-7f3a9c1e2b"
_FAKE_REFRESH_SECRET = "codex-refresh-SECRET-9d8e7f6a5b"
_FAKE_ID_SECRET = "codex-id-SECRET-1a2b3c4d5e"


def _jwt(exp_seconds: int) -> str:
    """Build a SYNTHETIC 3-segment JWT carrying an ``exp`` claim (epoch seconds).

    The signature segment is a fixed non-secret marker; the gate decodes the
    payload only and never verifies the signature (pre-flight validity check).
    """

    def _seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "RS256", "typ": "JWT"})
    payload = _seg({"exp": exp_seconds, "sub": "synthetic-subject", "iss": "test"})
    return f"{header}.{payload}.synthetic-signature-not-secret"


def _artifact(
    *,
    exp_seconds: int | None,
    access_token: str | None = None,
    include_tokens: bool = True,
) -> str:
    """Build a SYNTHETIC ~/.codex/auth.json body matching the spike shape."""
    if access_token is None and exp_seconds is not None:
        access_token = _jwt(exp_seconds)
    body: dict = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-06-21T00:00:00Z",
    }
    if include_tokens:
        body["tokens"] = {
            "id_token": _FAKE_ID_SECRET,
            "access_token": access_token,
            "refresh_token": _FAKE_REFRESH_SECRET,
            "account_id": "acct-synthetic-123",
        }
    return json.dumps(body)


def _future_seconds() -> int:
    return int(time.time() + 3600)


def _past_seconds() -> int:
    return int(time.time() - 3600)


def test_resolves_oauth_path_from_file() -> None:
    """A valid auth.json resolves the OAuth path with authenticated=True."""
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(exp_seconds=_future_seconds()),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    # Falsifiability: the method must NOT be a metered api-key path.
    assert resolution.method not in {"openai", "api_key", "api-key"}
    assert resolution.details.get("source") == "keychain"
    assert resolution.subscription_type == "chatgpt"


def test_resolves_oauth_path_from_credentials_file(tmp_path) -> None:
    """The credentials-file source resolves the same OAuth path directly."""
    cred_file = tmp_path / "auth.json"
    cred_file.write_text(_artifact(exp_seconds=_future_seconds()), encoding="utf-8")

    auth = CodexOAuthAuth(credentials_path=cred_file, keychain_reader=lambda: None)
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("source") == "credentials_file"


def test_missing_artifact_fails_closed(tmp_path) -> None:
    """No artifact anywhere -> unauthenticated with a non-secret reason."""
    auth = CodexOAuthAuth(
        credentials_path=tmp_path / "does-not-exist.json",
        keychain_reader=lambda: None,
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "no_codex_oauth_artifact"


def test_no_tokens_object_fails_closed() -> None:
    """An artifact without a tokens object fails closed."""
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(exp_seconds=None, include_tokens=False),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "no_codex_oauth_tokens"


def test_no_access_token_fails_closed() -> None:
    """A tokens object with an empty access_token fails closed."""
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(exp_seconds=None, access_token=""),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "no_access_token"


def test_expired_artifact_fails_closed() -> None:
    """An access_token whose JWT exp is in the past fails closed."""
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(exp_seconds=_past_seconds()),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "expired"


def test_unparseable_jwt_treated_as_live() -> None:
    """A non-JWT access token is unobservable for expiry, so the gate passes.

    A real CLI turn would surface the failure; the gate must not block on an
    expiry it cannot observe (mirrors the claude _is_expired contract).
    """
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(
            exp_seconds=None, access_token="not-a-jwt-opaque-token"
        ),
    )
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("expires_at") is None


def test_token_material_never_appears_in_resolution() -> None:
    """No synthetic secret leaks into the AuthResolution (details/method/etc.)."""
    auth = CodexOAuthAuth(
        credentials_path=None,
        keychain_reader=lambda: _artifact(exp_seconds=_future_seconds()),
    )
    resolution = auth.resolve()

    serialized = json.dumps(
        {
            "authenticated": resolution.authenticated,
            "method": resolution.method,
            "subscription_type": resolution.subscription_type,
            "details": resolution.details,
        }
    )
    for secret in (_FAKE_ACCESS_SECRET, _FAKE_REFRESH_SECRET, _FAKE_ID_SECRET):
        assert secret not in serialized
    # The JWT access token itself must not be echoed back either.
    assert "synthetic-signature-not-secret" not in serialized


def test_token_material_never_logged_on_unresolved_gate(caplog, tmp_path) -> None:
    """An unparseable artifact logs a warning that carries no token material."""
    leaking_blob = json.dumps({"tokens": {"access_token": _FAKE_ACCESS_SECRET}})[:-1]

    auth = CodexOAuthAuth(
        credentials_path=tmp_path / "does-not-exist.json",
        keychain_reader=lambda: leaking_blob,  # invalid JSON triggers the warning
    )
    with caplog.at_level(logging.WARNING):
        resolution = auth.resolve()

    assert resolution.authenticated is False
    for record in caplog.records:
        assert _FAKE_ACCESS_SECRET not in record.getMessage()


def test_jwt_exp_non_finite_does_not_crash() -> None:
    """Adversarial JWT exp values (inf, huge int, NaN-ish) never raise out of resolve().

    A structurally-valid JWT whose ``exp`` claim cannot be converted to a finite
    int must be treated as unobservable (expiry not determinable, gate passes) so
    resolve() ALWAYS returns a structured AuthResolution and NEVER propagates an
    OverflowError or any other ArithmeticError. Mirrors the MAJOR code-review fix.
    """
    import base64 as _b64
    import json as _json

    def _crafted_jwt(exp_value: object) -> str:
        header = _b64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_raw = _json.dumps({"exp": exp_value, "sub": "synthetic"}).encode()
        payload = _b64.urlsafe_b64encode(payload_raw).rstrip(b"=").decode()
        return f"{header}.{payload}.synthetic-sig"

    adversarial_cases = {
        "float_overflow_1e400": 1e400,  # serializes as Infinity -> non-finite
        "huge_int": 10**309,  # beyond float range -> OverflowError on int()
        "negative_huge": -(10**309),  # same, negative direction
    }

    for label, exp_value in adversarial_cases.items():
        try:
            token = _crafted_jwt(exp_value)
        except (ValueError, OverflowError):
            # json.dumps may itself refuse Infinity/NaN; that's fine -- the test
            # objective is that no exception escapes resolve(). If we can't even
            # build the JWT, use a raw non-finite string in the payload instead.
            payload_raw = f'{{"exp": {exp_value!r}, "sub": "synthetic"}}'.encode()
            payload = _b64.urlsafe_b64encode(payload_raw).rstrip(b"=").decode()
            header = _b64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
            token = f"{header}.{payload}.synthetic-sig"

        artifact = _json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": token,
                    "refresh_token": "r",
                    "id_token": "i",
                    "account_id": "a",
                },
                "last_refresh": "2026-06-21T00:00:00Z",
            }
        )

        auth = CodexOAuthAuth(
            credentials_path=None,
            keychain_reader=lambda a=artifact: a,
        )
        # Must NOT raise; must return a structured AuthResolution.
        try:
            resolution = auth.resolve()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"resolve() raised {type(exc).__name__} for exp={label!r}: {exc}"
            ) from exc

        assert isinstance(
            resolution.authenticated, bool
        ), f"resolve() returned non-bool authenticated for exp={label!r}"
        assert resolution.method == OAUTH_METHOD, f"wrong method for exp={label!r}"
        # Non-finite exp is unobservable; gate treats token as live (expires_at=None).
        assert resolution.details.get("expires_at") is None, (
            f"non-finite exp should yield expires_at=None, got "
            f"{resolution.details.get('expires_at')!r} for exp={label!r}"
        )


def test_non_dict_artifact_fails_closed(tmp_path) -> None:
    """A top-level JSON list (valid JSON, non-dict) artifact fails closed.

    json.loads succeeds but the result is not a dict; _load_artifact must skip
    it, fall through to no further source, and resolve() must fail closed.
    credentials_path points to a non-existent file so no real artifact can
    rescue the resolution.
    """
    auth = CodexOAuthAuth(
        credentials_path=tmp_path / "does-not-exist.json",
        keychain_reader=lambda: '[{"access_token": "sneaky"}]',
    )
    resolution = auth.resolve()

    assert resolution.authenticated is False
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("reason") == "no_codex_oauth_artifact"


def test_keychain_invalid_falls_through_to_credentials_file(tmp_path) -> None:
    """An invalid (non-JSON) keychain result falls through to the credentials file.

    Exercises the _load_artifact source-iteration loop: keychain returns garbage,
    logs a warning, then the credentials-file source resolves successfully.
    """
    cred_file = tmp_path / "auth.json"
    cred_file.write_text(_artifact(exp_seconds=_future_seconds()), encoding="utf-8")

    auth = CodexOAuthAuth(
        credentials_path=cred_file,
        keychain_reader=lambda: "not-valid-json!!!",
    )
    resolution = auth.resolve()

    assert resolution.authenticated is True
    assert resolution.method == OAUTH_METHOD
    assert resolution.details.get("source") == "credentials_file"


def test_import_does_not_pull_legacy_app_or_litellm() -> None:
    """Importing codex.py must not import reverso.proxy.app or litellm.

    Mirrors test_litellm_quarantine.py: checked in a fresh subprocess so a prior
    in-process import by an unrelated test cannot mask a real static-import edge.
    """
    code = (
        "import sys, importlib;"
        "importlib.import_module('reverso.protocols.adapters.codex');"
        "leaked_app = 'reverso.proxy.app' in sys.modules;"
        "leaked_litellm = any("
        "m == 'litellm' or m.startswith('litellm.') for m in sys.modules);"
        "print('proxy_app=' + ('LEAKED' if leaked_app else 'CLEAN'));"
        "print('litellm=' + ('LEAKED' if leaked_litellm else 'CLEAN'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"subprocess import failed: rc={result.returncode}\n{result.stderr}"
    out = result.stdout.strip()
    assert (
        "proxy_app=CLEAN" in out
    ), f"codex.py must NOT import reverso.proxy.app; subprocess reported: {out!r}"
    assert (
        "litellm=CLEAN" in out
    ), f"codex.py must NOT import any litellm module; subprocess reported: {out!r}"
