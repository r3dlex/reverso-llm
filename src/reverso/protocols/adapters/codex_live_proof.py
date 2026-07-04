"""Opt-in live Codex OAuth/PKCE proof harness.

Default behavior is fail-closed: no auth probing, subprocess execution, or network
call happens unless the relevant live-proof environment variable is explicitly set.
Reports intentionally contain only public shape metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
import asyncio
import inspect
import json
import os
import subprocess

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.auth import ProviderAuth, redact_mapping
from reverso.protocols.adapters.codex_direct import (
    CodexDirectUpstream,
    experimental_http_codex_direct_adapter,
)

LIVE_PROOF_ENV = "REVERSO_CODEX_LIVE_PROOF"
OFFICIAL_LIVE_PROOF_ENV = "REVERSO_CODEX_OFFICIAL_LIVE_PROOF"
DIRECT_LIVE_PROOF_ENV = "REVERSO_CODEX_DIRECT_LIVE_PROOF"
_DEFAULT_PROMPT = "Reply with exactly: reverso codex live proof ok"


class CodexLiveProofSkipped(RuntimeError):
    """Live proof did not run because explicit local opt-in was absent."""


@dataclass(frozen=True)
class LiveProofReport:
    """Secret-free live proof report."""

    lane: str
    status: str
    auth_authenticated: bool | None = None
    auth_method: str | None = None
    auth_source: str | None = None
    token_present: bool | None = None
    model: str | None = None
    response_shape_keys: list[str] = field(default_factory=list)
    usage_present: bool | None = None
    rate_limit_present: bool | None = None
    error_type: str | None = None
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "status": self.status,
            "auth_authenticated": self.auth_authenticated,
            "auth_method": self.auth_method,
            "auth_source": self.auth_source,
            "token_present": self.token_present,
            "model": self.model,
            "response_shape_keys": list(self.response_shape_keys),
            "usage_present": self.usage_present,
            "rate_limit_present": self.rate_limit_present,
            "error_type": self.error_type,
            "reason": self.reason,
        }


def _enabled(env: Mapping[str, str], key: str) -> bool:
    return env.get(key) == "1"


def require_live_opt_in(lane: str, env: Mapping[str, str] | None = None) -> None:
    """Fail closed unless the lane-specific opt-in is present."""

    env = os.environ if env is None else env
    if lane == "official":
        if _enabled(env, LIVE_PROOF_ENV) or _enabled(env, OFFICIAL_LIVE_PROOF_ENV):
            return
        raise CodexLiveProofSkipped(
            f"set {OFFICIAL_LIVE_PROOF_ENV}=1 or {LIVE_PROOF_ENV}=1 on a trusted local machine"
        )
    if lane == "direct":
        if _enabled(env, DIRECT_LIVE_PROOF_ENV):
            return
        raise CodexLiveProofSkipped(
            f"set {DIRECT_LIVE_PROOF_ENV}=1 on a trusted local machine"
        )
    raise ValueError(f"unknown live proof lane: {lane}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def auth_readiness(
    auth: ProviderAuth, *, probe_token: bool = False
) -> dict[str, Any]:
    """Return non-secret auth readiness metadata.

    `probe_token` returns only token presence, never token content.
    """

    resolution = auth.resolve()
    details = redact_mapping(dict(resolution.details))
    token_present = None
    if probe_token and resolution.authenticated:
        try:
            token = await _maybe_await(auth.bearer_token())
            token_present = bool(token)
        except Exception:  # noqa: BLE001 - report shape only.
            token_present = False
    return {
        "auth_authenticated": resolution.authenticated,
        "auth_method": resolution.method,
        "auth_source": _public_source(details),
        "token_present": token_present,
    }


def _public_source(details: Mapping[str, Any]) -> str | None:
    source = details.get("source") or details.get("reason")
    if isinstance(source, str):
        return source
    return None


def _shape_keys(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return sorted(str(key) for key in payload.keys())
    return []


def _report_error(
    lane: str, exc: BaseException, auth_meta: dict[str, Any] | None = None
) -> LiveProofReport:
    return LiveProofReport(
        lane=lane,
        status="failed",
        error_type=type(exc).__name__,
        reason=str(exc)[:160],
        **(auth_meta or {}),
    )


async def run_direct_live_proof(
    auth: ProviderAuth,
    *,
    env: Mapping[str, str] | None = None,
    upstream: CodexDirectUpstream | None = None,
    prompt: str = _DEFAULT_PROMPT,
) -> LiveProofReport:
    """Run the private direct backend proof only after explicit opt-in."""

    require_live_opt_in("direct", env)
    auth_meta = await auth_readiness(auth, probe_token=True)
    try:
        adapter = (
            experimental_http_codex_direct_adapter(auth) if upstream is None else None
        )
        if upstream is not None:
            from reverso.protocols.adapters.codex_direct import CodexDirectAdapter

            adapter = CodexDirectAdapter(auth=auth, upstream=upstream)
        assert adapter is not None
        envelope = await adapter.create_response(
            ResponsesRequest(model="gpt-5.5", input=prompt, stream=False)
        )
        raw = envelope.raw if isinstance(envelope.raw, dict) else {}
        return LiveProofReport(
            lane="direct",
            status="passed",
            model=envelope.model,
            response_shape_keys=_shape_keys(raw),
            usage_present=isinstance(envelope.usage, dict),
            rate_limit_present="rate_limits" in raw or "rate_limit" in raw,
            **auth_meta,
        )
    except Exception as exc:  # noqa: BLE001 - secret-free public report.
        return _report_error("direct", exc, auth_meta)


def run_official_cli_live_proof(
    *,
    env: Mapping[str, str] | None = None,
    prompt: str = _DEFAULT_PROMPT,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> LiveProofReport:
    """Run the official Codex CLI lane only after explicit opt-in."""

    require_live_opt_in("official", env)
    runner = runner or subprocess.run
    try:
        result = runner(
            ["codex", "exec", "--json", "--model", "gpt-5.5", prompt],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return _report_error("official", exc)
    parsed = _parse_codex_json_lines(result.stdout)
    status = "passed" if result.returncode == 0 else "failed"
    return LiveProofReport(
        lane="official",
        status=status,
        model=_first_string(parsed, "model"),
        response_shape_keys=sorted({key for item in parsed for key in item.keys()}),
        usage_present=any(isinstance(item.get("usage"), dict) for item in parsed),
        rate_limit_present=any("rate_limits" in item for item in parsed),
        error_type=None if result.returncode == 0 else "CodexCliNonZero",
        reason=None
        if result.returncode == 0
        else f"codex CLI exited {result.returncode}",
    )


def _parse_codex_json_lines(stdout: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def _first_string(rows: list[dict[str, Any]], key: str) -> str | None:
    for row in rows:
        value = row.get(key)
        if isinstance(value, str):
            return value
    return None


def run_direct_live_proof_sync(auth: ProviderAuth, **kwargs: Any) -> LiveProofReport:
    return asyncio.run(run_direct_live_proof(auth, **kwargs))
