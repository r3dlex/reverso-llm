"""Auggie provider adapter backed by the local @augmentcode/auggie CLI (ADR 0003).

There is NO Python auggie-sdk: Auggie ships as the npm CLI ``@augmentcode/auggie``
(binary ``auggie`` on PATH). This adapter therefore uses the bounded subprocess
spine (precedent ``src/reverso/protocols/adapters/claude.py``), shelling to the
locally installed ``auggie`` binary so it rides the user's OAuth session rather
than any repository-stored secret.

Indexing posture (see ``.omc/research/auggie-indexing-spike.md``): ``--print``
auto-indexes whatever ``--workspace-root`` resolves to and there is no global
per-invocation hard-disable. The adapter therefore defaults the workspace root to
an ephemeral empty sandbox dir (never the caller's workspace) and surfaces the
literal caveat ``hard-disable unproven`` in model metadata. The read-only posture
uses ``--ask`` (retrieval and non-editing tools only); the adapter never executes
tool calls.

Auth comes from the local OAuth session (``~/.augment/session.json`` or env
``AUGMENT_SESSION_AUTH``); never a repository secret. Diagnostics go through
redact_secret / redact_mapping so no token material is ever logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
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
from reverso.protocols.auth import redact_mapping, redact_secret
from reverso.protocols.store import ResponseStore

logger = logging.getLogger(__name__)

# Falsifiable indexing caveat: the spike could not prove a global per-invocation
# hard-disable, so model metadata carries this EXACT literal. The weaker word
# "disabled" must never be substituted (a downstream test asserts this literal).
INDEXING_CAVEAT = "hard-disable unproven"

# Local OAuth session artifact and env fallback. Existence only is checked; the
# token contents are never read or logged.
_SESSION_PATH = Path.home() / ".augment" / "session.json"
_SESSION_ENV = "AUGMENT_SESSION_AUTH"

# Subprocess wall-clock bound; a timeout surfaces a bounded AuggieError.
_CLI_TIMEOUT_SECONDS = 300.0


class AuggieError(RuntimeError):
    """Raised when the Auggie CLI is unavailable, unauthenticated, or fails."""


def _session_present() -> bool:
    """Return True when a local OAuth session exists (no token is read)."""
    if os.environ.get(_SESSION_ENV):
        return True
    return _SESSION_PATH.exists()


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


def _build_prompt(request: ResponsesRequest) -> str:
    """Combine instructions and input into a single prompt string."""
    text = _input_to_text(request.input)
    if request.instructions:
        return f"{request.instructions}\n\n{text}" if text else request.instructions
    return text


def _message_item(item_id: str, text: str) -> dict[str, Any]:
    """Build a completed Responses assistant message output item."""
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _build_completion_argv(prompt: str, model: str, workspace_root: str) -> list[str]:
    """Build the one-shot ``auggie`` completion argv.

    ``--print`` is one-shot, ``--output-format json`` gives parseable output, and
    ``--ask`` keeps a read-only posture (retrieval and non-editing tools only).
    ``--workspace-root`` is an ephemeral sandbox so indexing never touches the
    caller's workspace. This builder is pure so tests can assert the argv without
    launching anything.
    """
    return [
        "auggie",
        "--print",
        "--quiet",
        "--output-format",
        "json",
        "--ask",
        "-m",
        model,
        "--workspace-root",
        workspace_root,
        "--",
        prompt,
    ]


def _parse_completion_output(stdout: str) -> str:
    """Extract the assistant text from ``auggie --print --output-format json``."""
    text = stdout.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the raw text when the payload is not JSON.
        return text
    if isinstance(parsed, dict):
        for key in ("response", "text", "output", "content", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
        return text
    return text


def _normalize_models(payload: Any) -> list[dict[str, Any]]:
    """Map ``auggie model list --json`` output to OpenAI-shaped model dicts.

    Each model carries the falsifiable ``indexing`` caveat literal so downstream
    metadata cannot silently claim indexing is hard-disabled.
    """
    if isinstance(payload, dict):
        raw_models = payload.get("models") or payload.get("data") or []
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []
    created = int(time.time())
    normalized: list[dict[str, Any]] = []
    for model in raw_models:
        if isinstance(model, dict):
            model_id = model.get("id") or model.get("name")
        elif isinstance(model, str):
            model_id = model
        else:
            continue
        if not model_id:
            continue
        normalized.append(
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": "augmentcode",
                "indexing": INDEXING_CAVEAT,
            }
        )
    return normalized


class AuggieAdapter:
    """ProviderAdapter for Auggie served over the local CLI OAuth session.

    Completion text is produced by invoking the locally installed ``auggie`` CLI
    as a subprocess (no Python SDK exists), so the adapter rides the user's OAuth
    session. Output is mapped into the Responses ResponseEnvelope and SSE shapes
    the first-party app serializes. The default workspace root is an ephemeral
    sandbox so ``--print`` indexing never touches the caller's workspace.
    """

    def __init__(
        self,
        *,
        store: ResponseStore | None = None,
        cli_runner: Any | None = None,
        models_runner: Any | None = None,
    ) -> None:
        self._store = store or ResponseStore()
        # Injectable completion backend for tests; defaults to the auggie CLI.
        self._cli_runner = cli_runner or self._run_auggie_cli
        # Injectable model-listing backend; defaults to `auggie model list --json`.
        self._models_runner = models_runner or self._run_auggie_models

    def _ensure_authenticated(self) -> None:
        if not _session_present():
            logger.warning(
                "auggie auth not resolved: %s",
                redact_mapping({"reason": "no_augment_session"}),
            )
            raise AuggieError("auggie oauth session unavailable")

    def _run_auggie_cli(self, prompt: str, model: str) -> str:
        """Run the local ``auggie`` CLI for a single-shot completion.

        Uses an ephemeral sandbox workspace root so indexing never touches the
        caller's workspace; the sandbox is removed after the call. Returns the
        assistant text. Never logs token material.
        """
        workspace_root = tempfile.mkdtemp(prefix="reverso-auggie-")
        argv = _build_completion_argv(prompt, model, workspace_root)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise AuggieError("auggie CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise AuggieError("auggie CLI timed out") from exc
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "auggie CLI failed (rc=%s): %s",
                exc.returncode,
                redact_secret(exc.stderr or None),
            )
            # Suppress the cause: CalledProcessError carries raw stderr/argv that
            # could include token material and must not ride along in a traceback.
            raise AuggieError("auggie CLI invocation failed") from None
        finally:
            shutil.rmtree(workspace_root, ignore_errors=True)
        return _parse_completion_output(result.stdout)

    def _run_auggie_models(self) -> Any:
        """Run ``auggie model list --json`` and return the parsed payload."""
        try:
            result = subprocess.run(
                ["auggie", "model", "list", "--json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise AuggieError("auggie CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise AuggieError("auggie CLI timed out") from exc
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "auggie model list failed (rc=%s): %s",
                exc.returncode,
                redact_secret(exc.stderr or None),
            )
            # Suppress the cause (raw stderr/argv) from any downstream traceback.
            raise AuggieError("auggie model list failed") from None
        try:
            return json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise AuggieError("auggie model list returned invalid JSON") from exc

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``."""
        self._ensure_authenticated()
        prompt = _build_prompt(request)
        # The CLI runner is a blocking subprocess; offload it so a single Auggie
        # call cannot stall the gateway's shared event loop for its full timeout.
        text = await asyncio.to_thread(self._cli_runner, prompt, request.model)
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
        # Offload the blocking CLI subprocess (see create_response) before any
        # event is emitted, so the shared event loop stays responsive.
        text = await asyncio.to_thread(self._cli_runner, prompt, request.model)
        response_id = _new_response_id()
        message_id = _new_message_id()
        completed_item = _message_item(message_id, text)
        usage = _estimate_usage(prompt, text)

        # Store before emitting events: the CLI turn is already complete, so a
        # client disconnect mid-stream must not lose the response for later
        # previous_response_id chaining or get_response/input_items lookups.
        envelope = ResponseEnvelope(
            id=response_id,
            model=request.model,
            output=[completed_item],
            status="completed",
            usage=usage,
            previous_response_id=request.previous_response_id,
        )
        self._store.put_response(envelope, _record_input_items(request))

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
            "usage": usage,
        }
        yield _event("response.completed", {"response": completed_response})

    async def list_models(self) -> ModelList:
        """Return the Auggie model listing for ``/v1/models``.

        Each entry carries the ``indexing`` caveat literal ``hard-disable
        unproven`` so the falsifiable indexing claim travels with the metadata.
        """
        payload = self._models_runner()
        data = _normalize_models(payload)
        return ModelList(data=data, models=list(data))

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        """Return a previously created response by id."""
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise AuggieError(f"unknown response_id {response_id!r}")
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        """Return the input items recorded for a prior response id."""
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items


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
