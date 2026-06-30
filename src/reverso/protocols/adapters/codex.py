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
    The legacy CLI provider spawned ``codex exec`` with NO ``env=``
    argument, relying on the CLI's
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

import asyncio
import base64
import binascii
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, NamedTuple

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.adapters.cli_spine import (
    BoundedCliStreamFailure,
    run_bounded_cli,
    stream_bounded_cli,
)
from reverso.protocols.adapters.codex_rollout import read_rate_limits
from reverso.protocols.adapters import codex_usage_store
from reverso.protocols.auth import (
    AuthResolution,
    redact_mapping,
)
from reverso.protocols.model_exposure import codex_catalog_context_window
from reverso.protocols.replay import (
    buffered_envelope,
    build_prompt,
    estimate_usage,
    message_item,
    new_message_id,
    new_response_id,
    record_input_items,
    replay_incremental,
    replay_turn,
)
from reverso.protocols.store import ResponseStore
from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

logger = logging.getLogger(__name__)


class _CodexStreamTerminal(NamedTuple):
    """Terminal metadata emitted by the default Codex stream runner."""

    usage: dict[str, Any] | None
    thread_id: str | None


CodexCliResult = str | tuple[str, dict[str, Any] | None, str | None]
CodexCliRunner = Callable[[str, str], CodexCliResult]
CodexStreamChunk = str | _CodexStreamTerminal
CodexStreamRunner = Callable[[str, str], AsyncIterator[CodexStreamChunk]]

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

# The five gpt model ids served first-party on the Anthropic surface (PRD), each
# mapped to the ``codex exec --model`` flag. Codex accepts the gpt id directly as
# its model flag, so the mapping is identity here; it is kept explicit so the
# served set is the single source of truth for list_models and the --model flag.
_CODEX_MODEL_FLAGS: dict[str, str] = {
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-4.1": "gpt-4.1",
}

# Default codex --model flag when the request model is not one of the five served
# ids; the frontier model is the safe default so an unknown id still resolves.
_DEFAULT_CODEX_MODEL_FLAG = "gpt-5.5"


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
            # expires_at is epoch MILLISECONDS (JWT exp is seconds, multiplied by 1000).
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


def _codex_model_flag(model: str | None) -> str:
    """Map a requested gpt id to its ``codex exec --model`` flag.

    The five served ids map to themselves (Codex takes the gpt id directly as its
    model flag); an unknown id falls back to the frontier default so a stray id
    never produces an empty flag.
    """
    return _CODEX_MODEL_FLAGS.get(model or "", _DEFAULT_CODEX_MODEL_FLAG)


def _agent_message_text(event: dict[str, Any]) -> str:
    """Return the assistant text carried by one codex --json event, or "".

    The Codex ``--json`` grammar (B2, mirrored from
    ``reverso.daemon.parsers.codex_cli``) emits assistant text at
    ``item.completed`` with ``item.type == "agent_message"``. A
    ``command_execution`` item is a shell OBSERVATION, not Responses
    function-call output, so it returns "" here: the adapter is TEXT-ONLY and
    never fabricates a tool_use / function_call output item (pre-mortem 3).
    ``thread.started`` and ``turn.completed`` carry no assistant text.
    """
    if event.get("type") != "item.completed":
        return ""
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "agent_message":
        return ""
    text = item.get("text")
    return text if isinstance(text, str) else ""


def _is_turn_complete(event: dict[str, Any]) -> bool:
    """Return True for the terminal ``turn.completed`` event."""
    return event.get("type") == "turn.completed"


def _parse_codex_lines(
    stdout: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Parse a buffered ``codex exec --json`` stdout.

    Returns ``(text, usage, thread_id)`` where:
    - ``text``      - newline-joined agent_message texts (same as before)
    - ``usage``     - the ``turn.completed.usage`` dict (4 keys) or None
    - ``thread_id`` - from the ``thread.started`` event, or None

    Lines that are blank or not JSON are skipped (lenient daemon grammar).
    """
    parts: list[str] = []
    usage: dict[str, Any] | None = None
    thread_id: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        # Capture thread_id from the thread.started event.
        if event.get("type") == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str):
                thread_id = tid
        text = _agent_message_text(event)
        if text:
            parts.append(text)
        if _is_turn_complete(event):
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
            break
    return "\n".join(parts), usage, thread_id


class CodexAdapter:
    """ProviderAdapter for gpt models served over the local ChatGPT subscription.

    Completion text is produced by invoking the locally installed ``codex`` CLI
    (``codex exec ... --json``) as a subprocess through the bounded CLI spine
    (ADR 0005), so the adapter rides the user's ``codex login`` session rather
    than a metered OpenAI API key. Output is mapped into the internal Responses
    ``ResponseEnvelope`` / ``SSEEvent`` shapes; the M1 Anthropic surface converts
    those into Anthropic-native bodies and events.

    PRE-FLIGHT GATE: every turn first calls ``CodexOAuthAuth.resolve()`` and FAILS
    CLOSED with ``CodexAuthError`` if the subscription artifact is missing or
    expired. Per design point A3 (validate-only, the only buildable option proven
    by the G002.0 spike) NO token is injected into the child env: ``codex exec``
    exposes no injection variable, so the CLI authenticates the turn from its own
    login session. The gate is therefore a pre-flight validity check; the
    no-divergence coupling test asserts a valid gate with a FAILING ``codex exec``
    surfaces a STRUCTURED error (never a false-green).

    TEXT-ONLY ceiling (pre-mortem 3): Codex emits ``command_execution``
    observations, not Responses function-call output, so this adapter never emits
    a structured tool_use / function_call output item.
    """

    def __init__(
        self,
        *,
        auth: CodexOAuthAuth | None = None,
        store: ResponseStore | None = None,
        cli_runner: CodexCliRunner | None = None,
        stream_cli_runner: CodexStreamRunner | None = None,
    ) -> None:
        self._auth = auth or CodexOAuthAuth()
        self._store = store or ResponseStore()
        # Injectable completion backend for tests; defaults to the codex CLI. The
        # runner returns the assistant text for a single buffered turn.
        self._cli_runner = cli_runner or self._run_codex_cli
        # Injectable streaming backend; defaults to the codex CLI stream over the
        # bounded spine, yielding one fragment per agent_message. Tests inject
        # plain async generators.
        self._stream_cli_runner = stream_cli_runner or self._default_stream_cli_runner

    def _ensure_authenticated(self) -> AuthResolution:
        resolution = self._auth.resolve()
        if not resolution.authenticated:
            logger.warning(
                "codex auth not resolved: %s",
                redact_mapping(dict(resolution.details)),
            )
            raise CodexAuthError(
                str(resolution.details.get("reason", "codex_oauth_unavailable"))
            )
        # Defensive invariant: this adapter only ever uses the OAuth path.
        if resolution.method != OAUTH_METHOD:
            raise CodexAuthError(
                f"codex auth resolved to {resolution.method!r}, "
                f"expected {OAUTH_METHOD!r}"
            )
        return resolution

    def _run_codex_cli(
        self, prompt: str, model_flag: str
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """Run ``codex exec`` once for a single-shot completion.

        Returns ``(text, usage, thread_id)`` - the parsed assistant text, the
        real ``turn.completed.usage`` dict (4 keys) or None, and the
        ``thread.started`` thread_id or None.

        Uses the existing CLI login session (ChatGPT subscription OAuth, A3): NO
        ``env=`` is passed, so the spine inherits the parent env and the CLI
        authenticates from its own session; no reverso bearer is injected. The
        legacy ``-s workspace-write`` grant is deliberately DROPPED (read-only
        sandbox default) since this serves a text turn, not a file-mutating task.
        Bounding, redaction, and cause suppression live in the shared spine.
        """
        stdout = run_bounded_cli(
            [
                "codex",
                "exec",
                prompt,
                "--json",
                "--model",
                model_flag,
                "--skip-git-repo-check",
            ],
            error=CodexAuthError,
            cli_label="codex CLI",
            cwd=CURRENT_PROFILE_WORKSPACE.get(),
        )
        return _parse_codex_lines(stdout)

    async def _default_stream_cli_runner(
        self, prompt: str, model_flag: str
    ) -> AsyncIterator[CodexStreamChunk]:
        """Default streaming runner over ``codex exec --json``.

        Drives the bounded streaming spine (ADR 0005), which owns the deadline,
        kill-on-abandon, and redacted stderr logging. Codex buffers a full turn
        (assistant text arrives only at ``item.completed`` (agent_message)), so
        this yields one fragment per agent_message rather than token deltas; the
        canonical SSE shape is produced by ``replay_incremental`` above. No token
        is injected into the child env (A3); the parent env is inherited.

        Usage and thread_id are surfaced via a private terminal object yielded
        AFTER all text fragments. This avoids instance-state mutation, so
        concurrent streaming turns on the shared adapter cannot tear each
        other's data. The ``chunks()`` closure in ``_stream_response`` captures
        the values into turn-local nonlocal cells before ``finalize`` reads them.
        """
        _thread_id: str | None = None
        _usage: dict[str, Any] | None = None
        async for line in stream_bounded_cli(
            [
                "codex",
                "exec",
                prompt,
                "--json",
                "--model",
                model_flag,
                "--skip-git-repo-check",
            ],
            cli_label="codex CLI",
            cwd=CURRENT_PROFILE_WORKSPACE.get(),
        ):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str):
                    _thread_id = tid
            fragment = _agent_message_text(event)
            if fragment:
                yield fragment
            if _is_turn_complete(event):
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    _usage = raw_usage
                break
        # Yield terminal metadata so _stream_response can capture usage +
        # thread_id into turn-local cells without touching instance state.
        yield _CodexStreamTerminal(usage=_usage, thread_id=_thread_id)

    def _build_usage(
        self,
        stream_usage: dict[str, Any] | None,
        prompt: str,
        full_text: str,
    ) -> dict[str, Any]:
        """Return the envelope usage dict.

        Prefers ``stream_usage`` (real 4-key values from ``turn.completed``).
        Falls back to ``estimate_usage`` ONLY when stream_usage is absent,
        keeping ``estimate_usage`` unchanged and shared by non-codex adapters
        (INV-3).
        """
        if stream_usage is not None:
            input_tokens = int(stream_usage.get("input_tokens") or 0)
            cached = int(stream_usage.get("cached_input_tokens") or 0)
            output_tokens = int(stream_usage.get("output_tokens") or 0)
            reasoning = int(stream_usage.get("reasoning_output_tokens") or 0)
            return {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning,
                "total_tokens": input_tokens + output_tokens + reasoning,
            }
        return estimate_usage(prompt, full_text)

    async def _update_usage_store(
        self,
        model_id: str,
        stream_usage: dict[str, Any] | None,
        thread_id: str | None,
    ) -> None:
        """Update the in-process usage store after a completed codex turn.

        Reads the rollout file for rate_limits via ``asyncio.to_thread`` so
        the rglob+stat+file-read stays off the ASGI event loop (fix #3).
        A missing rollout keeps the last stored rate_limits value (keep-last
        semantics - does NOT clear to None).  Tokens and context are always
        updated from ``stream_usage`` when present.
        """
        if stream_usage is None:
            return
        window = codex_catalog_context_window(model_id)
        current_rl = codex_usage_store.get_rate_limits()
        new_rl = await asyncio.to_thread(read_rate_limits, thread_id)
        # Keep-last: when rollout gives nothing, preserve existing rate_limits.
        rate_limits = new_rl if new_rl is not None else current_rl
        snapshot = codex_usage_store.build_snapshot(
            model_id=model_id,
            stream_usage=stream_usage,
            window_tokens=window,
            rate_limits=rate_limits,
        )
        codex_usage_store.update(snapshot)

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``.

        A turn that exits 0 with no ``agent_message`` event yields an envelope
        with empty text; this is a VALID empty completion, not an error.
        """
        self._ensure_authenticated()
        prompt = build_prompt(request)
        model_flag = _codex_model_flag(request.model)
        # The CLI runner is a blocking subprocess; offload it so a single Codex
        # call cannot stall the gateway's shared event loop for its full run.
        result = await asyncio.to_thread(self._cli_runner, prompt, model_flag)
        # Support both the legacy str return (injected test runners that return
        # bare str) and the new tuple return (real _run_codex_cli).
        if isinstance(result, tuple):
            text, stream_usage, thread_id = result
        else:
            text, stream_usage, thread_id = result, None, None  # type: ignore[unreachable]
        usage = self._build_usage(stream_usage, prompt, text)
        envelope = ResponseEnvelope(
            id=new_response_id(),
            model=request.model,
            output=[message_item(new_message_id(), text)],
            status="completed",
            usage=usage,
            previous_response_id=request.previous_response_id,
        )
        self._store.put_response(envelope, record_input_items(request))
        await self._update_usage_store(request.model or "", stream_usage, thread_id)
        return envelope

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        """Yield Responses SSE events for ``request`` (stream=True)."""
        return self._stream_response(request)

    async def _stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        self._ensure_authenticated()
        prompt = build_prompt(request)
        model_flag = _codex_model_flag(request.model)

        # Codex buffers a full turn: text arrives only at item.completed
        # (agent_message), not as token deltas.  Two distinct pre-first-chunk
        # outcomes require different handling:
        #
        #   StopAsyncIteration (clean empty stream): the process exited 0 with no
        #   agent_message.  This is a VALID empty completion -- do NOT re-invoke
        #   _cli_runner.  A second spawn would double latency and subscription
        #   usage for what is simply a turn with no output.  Build the empty
        #   envelope directly and replay it.
        #
        #   BoundedCliStreamFailure / any other exception (actual failure): the
        #   process failed before emitting any output.  Fall back to the buffered
        #   _cli_runner so the caller receives a structured error (CodexAuthError)
        #   rather than a truncated stream.  This is the only case where the
        #   buffered runner is invoked.
        stream = self._stream_cli_runner(prompt, model_flag)
        first_chunk: CodexStreamChunk | None = None
        _stream_failed = False
        if hasattr(stream, "__anext__"):
            try:
                first_chunk = await stream.__anext__()  # type: ignore[union-attr]
            except StopAsyncIteration:
                # Clean empty turn: build empty completion directly (no re-spawn).
                first_chunk = None
                _stream_failed = False
            except BoundedCliStreamFailure as exc:
                logger.info(
                    "codex streaming failed before first chunk; "
                    "falling back to buffered (rc=%s)",
                    exc.returncode,
                )
                first_chunk = None
                _stream_failed = True
            except Exception as exc:  # noqa: BLE001 - any pre-stream failure folds into fallback
                logger.warning(
                    "codex streaming runner failed before first chunk: %s",
                    type(exc).__name__,
                )
                first_chunk = None
                _stream_failed = True

        if first_chunk is None and _stream_failed:
            # Failure fallback: re-invoke the buffered runner.  A failing codex
            # exec raises CodexAuthError here (no false-green); the gateway
            # renders it as a structured Anthropic error.
            result = await asyncio.to_thread(self._cli_runner, prompt, model_flag)
            if isinstance(result, tuple):
                text, stream_usage, thread_id = result
            else:
                text, stream_usage, thread_id = result, None, None  # type: ignore[unreachable]
            fb_usage = self._build_usage(stream_usage, prompt, text)
            envelope = ResponseEnvelope(
                id=new_response_id(),
                model=request.model,
                output=[message_item(new_message_id(), text)],
                status="completed",
                usage=fb_usage,
                previous_response_id=request.previous_response_id,
            )
            async for event in replay_turn(
                envelope, store=self._store, input_items=record_input_items(request)
            ):
                yield event
            await self._update_usage_store(request.model or "", stream_usage, thread_id)
            return

        if first_chunk is None:
            # Clean empty turn: replay an empty envelope directly, no re-spawn.
            # No real stream_usage available for an empty turn; _update_usage_store
            # is a no-op when stream_usage is None, but called for symmetry (#7).
            envelope = buffered_envelope(request, prompt=prompt, text="")
            async for event in replay_turn(
                envelope, store=self._store, input_items=record_input_items(request)
            ):
                yield event
            await self._update_usage_store(request.model or "", None, None)
            return

        # Incremental path: replay_incremental owns the canonical SSE sequence
        # and the finalize-time store write. After the first fragment any iterator
        # failure propagates unwrapped so the gateway's mid-stream contract takes
        # over verbatim.
        #
        # Turn-local cells for usage + thread_id. No instance-state mutation:
        # concurrent turns on the shared adapter cannot tear each other's data.
        # The chunks() async generator intercepts the terminal metadata object
        # that _default_stream_cli_runner yields after all text fragments and
        # captures the values into these nonlocal cells before forwarding
        # nothing to replay_incremental.
        response_id = new_response_id()
        message_id = new_message_id()
        _turn_usage: dict[str, Any] | None = None
        _turn_thread_id: str | None = None

        async def chunks() -> AsyncIterator[dict[str, Any]]:
            nonlocal _turn_usage, _turn_thread_id
            # first_chunk was already awaited; forward it unless it is metadata.
            if isinstance(first_chunk, _CodexStreamTerminal):
                _turn_usage = first_chunk.usage
                _turn_thread_id = first_chunk.thread_id
                return
            yield {"text": first_chunk}
            async for fragment in stream:
                if isinstance(fragment, _CodexStreamTerminal):
                    _turn_usage = fragment.usage
                    _turn_thread_id = fragment.thread_id
                    return
                yield {"text": fragment}

        def finalize(
            *,
            full_text: str,
            full_reasoning: str | None,
            usage: dict[str, Any] | None,
            tool_calls: list[dict[str, Any]],
        ) -> ResponseEnvelope:
            # _turn_usage and _turn_thread_id were captured into turn-local
            # nonlocal cells by chunks() above. No instance state, no race.
            envelope_usage = self._build_usage(_turn_usage, prompt, full_text)
            return ResponseEnvelope(
                id=response_id,
                model=request.model,
                output=[message_item(message_id, full_text)],
                status="completed",
                usage=envelope_usage,
                previous_response_id=request.previous_response_id,
            )

        async for event in replay_incremental(
            chunks(),
            response_id=response_id,
            message_id=message_id,
            model=request.model,
            store=self._store,
            input_items=record_input_items(request),
            finalize=finalize,
        ):
            yield event

        # Store update after replay_incremental completes (store-before-drain
        # relaxed per ADR 0004; usage store follows the same relaxed contract).
        await self._update_usage_store(
            request.model or "", _turn_usage, _turn_thread_id
        )

    async def list_models(self) -> ModelList:
        """Return the five served gpt ids as a ``/v1/models`` listing.

        No live upstream call is required: the served set is static data
        (``_CODEX_MODEL_FLAGS``), the single source of truth for routing.
        """
        created = int(time.time())
        data = [
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "openai",
            }
            for model_id in _CODEX_MODEL_FLAGS
        ]
        return ModelList(data=data, models=list(data))

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        """Return a previously created response by id."""
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise CodexAuthError(f"unknown response_id {response_id!r}")
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        """Return the input items recorded for a prior response id."""
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items
