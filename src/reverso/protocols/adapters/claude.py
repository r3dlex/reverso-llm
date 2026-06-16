"""Claude provider adapter with a falsifiable subscription-OAuth gate (ADR 0002).

This adapter serves the ``/claude`` Responses path. Its authentication gate is
deliberately falsifiable (ADR 0002 D3): it reads the local ``claudeAiOauth``
artifact DIRECTLY and asserts on it, rather than inferring auth by elimination
from the presence of ``ANTHROPIC_API_KEY``. The resolved AuthResolution.method
is always the OAuth path (``claude_oauth``); the gate is designed to FAIL if it
ever resolves to a metered API key or consumes a token from the environment.

The credential artifact is the same one written by the Claude Code CLI login:
  - macOS: Keychain generic-password item, service "Claude Code-credentials".
  - Linux/headless: ~/.claude/.credentials.json (mode 0600).
Both store a JSON object whose top-level ``claudeAiOauth`` key holds
``accessToken``, ``refreshToken``, ``expiresAt``, ``scopes``,
``subscriptionType`` and ``rateLimitTier``.

Token material is NEVER logged: all diagnostics go through redact_secret /
redact_mapping. No repository secret is read or stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx

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
from reverso.protocols.auth import (
    AuthResolution,
    redact_mapping,
)
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
from reverso.proxy.profile_routing import (
    CURRENT_PROFILE_WORKSPACE,
    resolve_profile_model,
)

logger = logging.getLogger(__name__)

# The resolved OAuth method name. The falsifiable gate asserts on this exact
# value; it must never be an api-key path.
OAUTH_METHOD = "claude_oauth"

# Env vars the gate must NOT consume. Reading these to authenticate would defeat
# the subscription-OAuth requirement, so they are named here only to assert they
# are never used (the gate test checks the env is left untouched).
_FORBIDDEN_AUTH_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_OAUTH_ARTIFACT_KEY = "claudeAiOauth"
_LINUX_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# Anthropic API endpoint used ONLY for the live model listing; completions
# still go through the claude CLI subprocess. The oauth beta header lets the
# subscription bearer authenticate the request (never ANTHROPIC_API_KEY).
_ANTHROPIC_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETA = "oauth-2025-04-20"
_MODELS_TIMEOUT_SECONDS = 30.0

# Fallback listing when the live Anthropic listing is unreachable. These are
# claude CLI aliases, which the CLI always accepts regardless of model churn
# (full dated ids go stale and the CLI rejects them).
_FALLBACK_CLAUDE_MODELS = (
    "opus",
    "sonnet",
    "haiku",
)


class ClaudeAuthError(RuntimeError):
    """Raised when the Claude subscription-OAuth credential cannot be resolved."""


class ClaudeOAuthAuth:
    """Resolve Claude subscription credentials from the local OAuth artifact.

    This implements the ProviderAuth surface but does so WITHOUT auth-by-
    elimination: it reads the ``claudeAiOauth`` artifact directly and asserts the
    access token is present. It never falls back to ANTHROPIC_API_KEY or any
    other environment token; if the artifact is absent the resolution is simply
    unauthenticated.
    """

    def __init__(
        self,
        *,
        keychain_service: str = _KEYCHAIN_SERVICE,
        credentials_path: Path | None = None,
        keychain_reader: Any | None = None,
    ) -> None:
        self._keychain_service = keychain_service
        self._credentials_path = credentials_path or _LINUX_CREDENTIALS_PATH
        # Injectable for tests; defaults to the real macOS Keychain read.
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
        """Read the raw credential JSON from the Linux/headless fallback file."""
        try:
            return self._credentials_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None

    def _load_artifact(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return (claudeAiOauth dict, source) read DIRECTLY from local storage.

        Tries the Keychain first, then the credentials file. The returned dict is
        the value under the ``claudeAiOauth`` top-level key. Neither path consults
        any environment token. ``source`` is a non-secret diagnostic label.
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
                    "claude oauth artifact from %s was not valid JSON", source
                )
                continue
            artifact = parsed.get(_OAUTH_ARTIFACT_KEY)
            if isinstance(artifact, dict):
                return artifact, source
        return None, None

    def resolve(self) -> AuthResolution:
        """Resolve the OAuth credential and return a non-secret summary.

        The ``method`` is ALWAYS the OAuth path when authenticated; this adapter
        has no api-key code path. ``authenticated`` is True only when a live
        access token is present in the artifact (and, when observable, not
        expired).
        """
        artifact, source = self._load_artifact()
        if artifact is None:
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                details={"reason": "no_claude_oauth_artifact"},
            )

        access_token = artifact.get("accessToken")
        subscription_type = artifact.get("subscriptionType")
        expires_at = artifact.get("expiresAt")

        if not access_token:
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                subscription_type=subscription_type,
                details={"reason": "no_access_token", "source": source},
            )

        details: dict[str, object] = {
            "source": source,
            "scopes": artifact.get("scopes"),
            "rate_limit_tier": artifact.get("rateLimitTier"),
        }

        if _is_expired(expires_at):
            details["reason"] = "expired"
            details["expires_at"] = expires_at
            return AuthResolution(
                authenticated=False,
                method=OAUTH_METHOD,
                subscription_type=subscription_type,
                details=details,
            )

        if expires_at is not None:
            details["expires_at"] = expires_at
        return AuthResolution(
            authenticated=True,
            method=OAUTH_METHOD,
            subscription_type=subscription_type,
            details=details,
        )

    async def bearer_token(self) -> str:
        """Return the live OAuth access token. NEVER log the raw return value."""
        artifact, _ = self._load_artifact()
        if not artifact or not artifact.get("accessToken"):
            raise ClaudeAuthError("no claude oauth access token available")
        return str(artifact["accessToken"])


def _is_expired(expires_at: Any) -> bool:
    """Return True when an observable expiry has passed.

    ``expiresAt`` is the Claude Code epoch-milliseconds expiry. When it is absent
    or unparseable the expiry is not observable, so this returns False and the
    caller treats the token as live (a real upstream call would surface a 401).
    """
    if expires_at is None:
        return False
    try:
        expiry_ms = float(expires_at)
    except (TypeError, ValueError):
        return False
    return expiry_ms <= time.time() * 1000.0


class _StreamPreflightError(RuntimeError):
    """Internal marker: streaming runner failed before emitting any chunk.

    Raised by the default streaming runner when the claude CLI exits nonzero
    before the first text chunk arrives or when its first stdout line cannot
    be parsed as the documented stream-json envelope. _stream_response treats
    this as the documented fallback condition and replays the buffered path.
    Never surfaced to clients (callers swap it for the buffered runner).
    """


class ClaudeAdapter:
    """ProviderAdapter for Claude served over the local subscription OAuth login.

    Completion text is produced by invoking the locally installed ``claude`` CLI
    (the same binary the user logged in with) as a subprocess, so the adapter
    rides the subscription session rather than a metered API key. Output is
    mapped into the Responses ResponseEnvelope and SSE event shapes the
    first-party app serializes.
    """

    def __init__(
        self,
        *,
        auth: ClaudeOAuthAuth | None = None,
        store: ResponseStore | None = None,
        cli_runner: Any | None = None,
        stream_cli_runner: Callable[[str, str], AsyncIterator[str]] | None = None,
        models_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._auth = auth or ClaudeOAuthAuth()
        self._store = store or ResponseStore()
        # Injectable completion backend for tests; defaults to the claude CLI.
        self._cli_runner = cli_runner or self._run_claude_cli
        # Injectable streaming backend; defaults to the claude CLI stream-json
        # subprocess. Tests inject plain async generators.
        self._stream_cli_runner = stream_cli_runner or self._default_stream_cli_runner
        # Injectable HTTP backend for the live model listing only.
        self._models_client_factory = models_client_factory or (
            lambda: httpx.AsyncClient(timeout=_MODELS_TIMEOUT_SECONDS)
        )

    def _ensure_authenticated(self) -> AuthResolution:
        resolution = self._auth.resolve()
        if not resolution.authenticated:
            logger.warning(
                "claude auth not resolved: %s",
                redact_mapping(dict(resolution.details)),
            )
            raise ClaudeAuthError(
                str(resolution.details.get("reason", "claude_oauth_unavailable"))
            )
        # Defensive invariant: this adapter only ever uses the OAuth path.
        if resolution.method != OAUTH_METHOD:
            raise ClaudeAuthError(
                f"claude auth resolved to {resolution.method!r}, expected {OAUTH_METHOD!r}"
            )
        return resolution

    async def _default_stream_cli_runner(
        self, prompt: str, model: str
    ) -> AsyncIterator[str]:
        """Default streaming runner over `claude --output-format stream-json`.

        Runs the local claude CLI through the bounded streaming spine
        (``stream_bounded_cli``, ADR 0005), which owns the wall-clock
        deadline, kill-on-abandon, and redacted stderr logging. This runner
        contributes argv and the stream-json line parsing only: one text
        fragment is yielded per assistant text content chunk. The resolved
        OAuth token is handed to the child via its environment ONLY and is
        never logged; the parent env is otherwise inherited unchanged.

        Fallback semantics (mirrored in _stream_response): any spine failure
        or unparseable stream-json BEFORE the first fragment raises
        _StreamPreflightError so the caller can switch to the buffered path.
        Once any fragment has been yielded, errors propagate unwrapped so the
        gateway's mid-stream contract (response.failed + [DONE]) takes over,
        EXCEPT a nonzero exit after emitted text, which is treated as benign
        EOF (long-standing parity: the emitted text stands as the turn).
        """
        token = _resolve_token_sync(self._auth)
        child_env = dict(os.environ)
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        argv = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            model,
            "--",
            prompt,
        ]
        emitted = False
        try:
            async for line in stream_bounded_cli(
                argv,
                cli_label="claude CLI",
                env=child_env,
                cwd=CURRENT_PROFILE_WORKSPACE.get(),
            ):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    if not emitted:
                        raise _StreamPreflightError(
                            "claude stream-json first line was not JSON"
                        ) from exc
                    raise
                if not isinstance(event, dict):
                    if not emitted:
                        raise _StreamPreflightError(
                            "claude stream-json first event was not an object"
                        )
                    continue
                fragment = _extract_assistant_text(event)
                if fragment:
                    emitted = True
                    yield fragment
        except BoundedCliStreamFailure as exc:
            if not emitted:
                if exc.returncode is not None:
                    raise _StreamPreflightError(
                        f"claude stream CLI exited rc={exc.returncode} "
                        "before first chunk"
                    ) from None
                raise _StreamPreflightError(str(exc)) from None
            if exc.returncode is not None:
                return
            raise

    def _run_claude_cli(self, prompt: str, model: str) -> str:
        """Run the local `claude` CLI for a single-shot completion.

        Uses the existing CLI login session (subscription OAuth). The resolved
        token is passed via the process environment for the child only; it is
        never logged. Bounding, redaction, and cause suppression live in the
        shared CLI spine. Returns the assistant text.
        """
        token = _resolve_token_sync(self._auth)
        child_env = dict(os.environ)
        # Hand the child the live subscription token; redact before any logging.
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        stdout = run_bounded_cli(
            ["claude", "--print", "--model", model, "--", prompt],
            error=ClaudeAuthError,
            cli_label="claude CLI",
            env=child_env,
            cwd=CURRENT_PROFILE_WORKSPACE.get(),
        )
        return stdout.strip()

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``."""
        self._ensure_authenticated()
        prompt = build_prompt(request)
        # Resolve GPT-level Codex profile names (e.g. gpt-5.5) to concrete
        # Claude model ids, matching the legacy ProfileRoutingMiddleware that
        # the first-party /claude path bypasses (same compensation as deepseek).
        cli_model = resolve_profile_model("claude", request.model or "")
        # The CLI runner is a blocking subprocess; offload it so a single Claude
        # call cannot stall the gateway's shared event loop for its full run.
        text = await asyncio.to_thread(self._cli_runner, prompt, cli_model)
        envelope = buffered_envelope(request, prompt=prompt, text=text)
        self._store.put_response(envelope, record_input_items(request))
        return envelope

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        """Yield Responses SSE events for ``request`` (stream=True)."""
        return self._stream_response(request)

    async def _stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        self._ensure_authenticated()
        prompt = build_prompt(request)
        cli_model = resolve_profile_model("claude", request.model or "")

        # Try the incremental streaming runner first. Per the B2 contract the
        # ONLY conditions that may silently fall back to the buffered CLI are
        # (a) the streaming subprocess exits nonzero before yielding any chunk,
        # or (b) the first chunk cannot be parsed. The default runner signals
        # both via _StreamPreflightError; any other pre-first-chunk failure
        # (auth, no CLI) collapses into the same fallback path here so the
        # client still gets a buffered response. Once a delta has been emitted
        # we MUST NOT fall back, so we keep the fallback window strictly
        # before the first yield.
        stream = self._stream_cli_runner(prompt, cli_model)
        first_chunk: str | None = None
        if hasattr(stream, "__anext__"):
            try:
                first_chunk = await stream.__anext__()  # type: ignore[union-attr]
            except StopAsyncIteration:
                first_chunk = None
            except _StreamPreflightError as exc:
                logger.info(
                    "claude streaming preflight failed; falling back to buffered (%s)",
                    type(exc).__name__,
                )
                first_chunk = None
            except Exception as exc:  # noqa: BLE001 - any pre-stream failure folds into fallback
                logger.warning(
                    "claude streaming runner failed before first chunk: %s",
                    type(exc).__name__,
                )
                first_chunk = None
        else:
            first_chunk = None

        if first_chunk is None:
            # Fallback: buffered CLI path. Identical shape to create_response.
            text = await asyncio.to_thread(self._cli_runner, prompt, cli_model)
            envelope = buffered_envelope(request, prompt=prompt, text=text)
            async for event in replay_turn(
                envelope, store=self._store, input_items=record_input_items(request)
            ):
                yield event
            return

        # Incremental streaming path (ADR 0004): the adapter contributes only
        # its chunk iterator (the preflight-pulled first chunk re-prefixed onto
        # the runner's stream) and a finalize callable; replay_incremental owns
        # canonical event emission, the finalize-time store write, and the
        # chunk-to-delta mapping (the parity suite's _collapse_repeated_deltas
        # absorbs the chunking difference). After the first delta any iterator
        # failure propagates unwrapped so the gateway's mid-stream contract
        # (response.failed event + [DONE]) takes over verbatim.
        response_id = new_response_id()
        message_id = new_message_id()

        async def chunks() -> AsyncIterator[dict[str, Any]]:
            yield {"text": first_chunk}
            async for chunk in stream:
                yield {"text": chunk}

        def finalize(
            *,
            full_text: str,
            full_reasoning: str | None,
            usage: dict[str, Any] | None,
            tool_calls: list[dict[str, Any]],
        ) -> ResponseEnvelope:
            return ResponseEnvelope(
                id=response_id,
                model=request.model,
                output=[message_item(message_id, full_text)],
                status="completed",
                usage=estimate_usage(prompt, full_text),
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

    async def list_models(self) -> ModelList:
        """Return the live Anthropic model listing for ``/v1/models``.

        Fetched from the Anthropic API with the resolved subscription OAuth
        bearer plus the oauth beta header; this adapter never consumes
        ANTHROPIC_API_KEY. Falls back to the always-valid CLI aliases when the
        token or upstream is unavailable, so the endpoint never 502s for a
        listing. The token itself is never logged.
        """
        created = int(time.time())
        try:
            token = await self._auth.bearer_token()
            async with self._models_client_factory() as client:
                response = await client.get(
                    f"{_ANTHROPIC_API_BASE}/v1/models",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-version": _ANTHROPIC_VERSION,
                        "anthropic-beta": _OAUTH_BETA,
                    },
                )
            if 200 <= response.status_code < 300:
                payload = response.json()
                data = [
                    {
                        "id": model["id"],
                        "object": "model",
                        "created": created,
                        "owned_by": "anthropic",
                    }
                    for model in payload.get("data", [])
                    if isinstance(model, dict) and model.get("id")
                ]
                if data:
                    return ModelList(data=data)
            logger.warning(
                "anthropic model listing returned %s; serving CLI alias fallback",
                response.status_code,
            )
        except (ClaudeAuthError, httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "anthropic model listing unavailable (%s); serving CLI alias fallback",
                type(exc).__name__,
            )
        data = [
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "anthropic",
            }
            for model_id in _FALLBACK_CLAUDE_MODELS
        ]
        return ModelList(data=data)

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        """Return a previously created response by id."""
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise ClaudeAuthError(f"unknown response_id {response_id!r}")
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        """Return the input items recorded for a prior response id."""
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items


def _resolve_token_sync(auth: ClaudeOAuthAuth) -> str:
    """Resolve the bearer token without an event loop (CLI runs in a thread)."""
    artifact, _ = auth._load_artifact()
    if not artifact or not artifact.get("accessToken"):
        raise ClaudeAuthError("no claude oauth access token available")
    return str(artifact["accessToken"])


def _extract_assistant_text(event: dict[str, Any]) -> str:
    """Return the assistant text fragment carried by one stream-json event.

    Per A3 evidence in .omc/research/auggie-streaming.md the claude CLI emits
    one JSON object per line. Assistant text fragments live under
    ``{"type":"assistant","message":{...,"content":[{"type":"text","text":...}]}}``;
    thinking parts, system lifecycle events, rate_limit_event, and the terminal
    result event are intentionally ignored so the gateway never streams
    reasoning or metadata as user-visible deltas.
    """
    if event.get("type") != "assistant":
        return ""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)
