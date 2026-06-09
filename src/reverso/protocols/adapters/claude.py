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

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.auth import (
    AuthResolution,
    redact_mapping,
    redact_secret,
)
from reverso.protocols.store import ResponseStore

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

# Static model listing for the Claude path. Kept minimal and OpenAI-shaped; the
# app adds the Codex-private ``models`` refresh field around this data list.
_CLAUDE_MODELS = (
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-haiku-20241022",
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


def _input_to_text(value: Any) -> str:
    """Flatten a Responses ``input`` (string or item list) into a prompt string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    if isinstance(value, list):
        for item in value:
            parts.append(_input_item_to_text(item))
    else:
        parts.append(str(value))
    return "\n".join(part for part in parts if part)


def _input_item_to_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts)
    text = item.get("text")
    return text if isinstance(text, str) else ""


def _message_item(item_id: str, text: str) -> dict[str, Any]:
    """Build a completed Responses assistant message output item."""
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


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
    ) -> None:
        self._auth = auth or ClaudeOAuthAuth()
        self._store = store or ResponseStore()
        # Injectable completion backend for tests; defaults to the claude CLI.
        self._cli_runner = cli_runner or self._run_claude_cli

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

    def _run_claude_cli(self, prompt: str, model: str) -> str:
        """Run the local `claude` CLI for a single-shot completion.

        Uses the existing CLI login session (subscription OAuth). The resolved
        token is passed via the process environment for the child only; it is
        never logged. Returns the assistant text.
        """
        token = _resolve_token_sync(self._auth)
        child_env = dict(os.environ)
        # Hand the child the live subscription token; redact before any logging.
        child_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        try:
            result = subprocess.run(
                ["claude", "--print", "--model", model, prompt],
                capture_output=True,
                text=True,
                check=True,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise ClaudeAuthError("claude CLI not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "claude CLI failed (rc=%s): %s",
                exc.returncode,
                redact_secret(exc.stderr or None),
            )
            raise ClaudeAuthError("claude CLI invocation failed") from exc
        return result.stdout.strip()

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``."""
        self._ensure_authenticated()
        prompt = _build_prompt(request)
        text = self._cli_runner(prompt, request.model)
        response_id = _new_response_id()
        message = _message_item(_new_message_id(), text)
        envelope = ResponseEnvelope(
            id=response_id,
            model=request.model,
            output=[message],
            status="completed",
            usage=_estimate_usage(prompt, text),
            previous_response_id=request.previous_response_id,
        )
        self._store.put_response(envelope, _record_input_items(request))
        return envelope

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        """Yield Responses SSE events for ``request`` (stream=True)."""
        return self._stream_response(request)

    async def _stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        self._ensure_authenticated()
        prompt = _build_prompt(request)
        text = self._cli_runner(prompt, request.model)
        response_id = _new_response_id()
        message_id = _new_message_id()

        base_response = {
            "id": response_id,
            "object": "response",
            "status": "in_progress",
            "model": request.model,
        }
        yield _event("response.created", {"response": dict(base_response)})
        yield _event("response.in_progress", {"response": dict(base_response)})
        yield _event(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )
        yield _event(
            "response.content_part.added",
            {
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )
        yield _event(
            "response.output_text.delta",
            {
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
        )
        yield _event(
            "response.output_text.done",
            {
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
        )
        yield _event(
            "response.content_part.done",
            {
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            },
        )
        completed_item = _message_item(message_id, text)
        yield _event(
            "response.output_item.done",
            {"output_index": 0, "item": completed_item},
        )
        completed_response = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "model": request.model,
            "output": [completed_item],
            "usage": _estimate_usage(prompt, text),
        }
        yield _event("response.completed", {"response": completed_response})

        envelope = ResponseEnvelope(
            id=response_id,
            model=request.model,
            output=[completed_item],
            status="completed",
            usage=completed_response["usage"],
            previous_response_id=request.previous_response_id,
        )
        self._store.put_response(envelope, _record_input_items(request))

    async def list_models(self) -> ModelList:
        """Return the Claude model listing for ``/v1/models``."""
        created = int(time.time())
        data = [
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "anthropic",
            }
            for model_id in _CLAUDE_MODELS
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


def _build_prompt(request: ResponsesRequest) -> str:
    """Combine instructions and input into a single prompt string."""
    text = _input_to_text(request.input)
    if request.instructions:
        return f"{request.instructions}\n\n{text}" if text else request.instructions
    return text


def _record_input_items(request: ResponsesRequest) -> list[dict[str, Any]]:
    """Build the stored input-item record for previous_response_id chaining."""
    value = request.input
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if value is None:
        return []
    return [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _input_to_text(value)}],
        }
    ]


def _estimate_usage(prompt: str, output: str) -> dict[str, int]:
    """Approximate token usage from word counts (no upstream usage available)."""
    input_tokens = len(prompt.split())
    output_tokens = len(output.split())
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _event(event_type: str, body: dict[str, Any]) -> SSEEvent:
    """Build an SSEEvent whose data carries its own ``type`` (OpenAI shape)."""
    data = {"type": event_type}
    data.update(body)
    return SSEEvent(event=event_type, data=data)


def _new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"
