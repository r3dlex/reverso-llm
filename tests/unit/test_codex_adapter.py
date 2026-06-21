"""Falsifiable unit tests for the first-party CodexAdapter (STORY G003, ADR 0007).

These tests prove the adapter core WITHOUT spawning a real ``codex`` process: the
CLI spine is replaced through the injectable ``cli_runner`` / ``stream_cli_runner``
seams, and the OAuth gate is driven through the ``CodexOAuthAuth`` constructor seam
with SYNTHETIC artifacts. They assert:

  - non-streaming: a canned codex ``--json`` turn yields a valid ResponseEnvelope
    with the assistant text and usage;
  - streaming: stream_response emits the canonical internal SSE sequence whose
    concatenated deltas equal the assistant text;
  - OAuth fail-closed: a missing artifact surfaces a structured CodexAuthError and
    NEVER spawns codex;
  - NO-DIVERGENCE COUPLING (the C1 / G002-deferred test): a VALID artifact but a
    FAILING codex exec surfaces a STRUCTURED error from BOTH create_response and
    stream_response (no false-green, no unhandled exception);
  - text-only ceiling: a turn carrying a ``command_execution`` observation yields
    text only, with NO structured tool_use / function_call output item;
  - list_models returns exactly the five served gpt ids.

All fixtures are SYNTHETIC; no real ChatGPT login is read.
"""

from __future__ import annotations

import base64
import json
import time
from typing import AsyncIterator

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.cli_spine import BoundedCliStreamFailure
from reverso.protocols.adapters.codex import (
    _DEFAULT_CODEX_MODEL_FLAG,
    _codex_model_flag,
    CodexAdapter,
    CodexAuthError,
    CodexOAuthAuth,
    _parse_codex_lines,
)

# --- synthetic OAuth artifact helpers (mirrors test_codex_oauth.py) ---------


def _jwt(exp_seconds: int) -> str:
    def _seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "RS256", "typ": "JWT"})
    payload = _seg({"exp": exp_seconds, "sub": "synthetic", "iss": "test"})
    return f"{header}.{payload}.synthetic-signature-not-secret"


def _valid_artifact() -> str:
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": "2026-06-21T00:00:00Z",
            "tokens": {
                "id_token": "i",
                "access_token": _jwt(int(time.time() + 3600)),
                "refresh_token": "r",
                "account_id": "acct-synthetic-123",
            },
        }
    )


def _valid_auth() -> CodexOAuthAuth:
    """A CodexOAuthAuth whose gate resolves authenticated=True from a synthetic file."""
    return CodexOAuthAuth(credentials_path=None, keychain_reader=_valid_artifact)


def _missing_auth(tmp_path) -> CodexOAuthAuth:
    """A CodexOAuthAuth whose gate fails closed (no artifact anywhere)."""
    return CodexOAuthAuth(
        credentials_path=tmp_path / "does-not-exist.json",
        keychain_reader=lambda: None,
    )


# --- canned codex exec --json turns -----------------------------------------

_AGENT_TEXT = "Hello from Codex."

_CANNED_TURN_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "thread-abc"}),
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _AGENT_TEXT},
        }
    ),
    json.dumps({"type": "turn.completed"}),
]
_CANNED_TURN_STDOUT = "\n".join(_CANNED_TURN_LINES) + "\n"

# A turn that includes a command_execution observation BEFORE the agent_message;
# the adapter must treat the observation as non-emitting (text-only ceiling).
_CANNED_TOOL_TURN_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "thread-tool"}),
    json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "total 0",
                "exit_code": 0,
            },
        }
    ),
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _AGENT_TEXT},
        }
    ),
    json.dumps({"type": "turn.completed"}),
]
_CANNED_TOOL_TURN_STDOUT = "\n".join(_CANNED_TOOL_TURN_LINES) + "\n"


def _fake_buffered_runner(stdout: str):
    """Build a cli_runner that parses canned codex --json stdout into text.

    Routes through the real ``_parse_codex_lines`` grammar so the adapter's
    buffered parse path is exercised, not bypassed.
    """

    def _runner(prompt: str, model_flag: str) -> str:
        return _parse_codex_lines(stdout)

    return _runner


def _fake_stream_runner(lines: list[str]):
    """Build a stream_cli_runner yielding agent_message fragments per the grammar.

    Mirrors the default streaming runner: one fragment per agent_message,
    skipping command_execution observations, terminating on turn.completed.
    """

    async def _runner(prompt: str, model_flag: str) -> AsyncIterator[str]:
        for raw in lines:
            event = json.loads(raw)
            if event.get("type") == "turn.completed":
                return
            item = event.get("item") if event.get("type") == "item.completed" else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    yield text

    return _runner


def _make_request(*, stream: bool = False) -> ResponsesRequest:
    return ResponsesRequest(model="gpt-5.5", input="Say hi.", stream=stream)


async def _collect(stream) -> list:
    return [event async for event in stream]


# --- non-streaming ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_response_returns_envelope_with_text_and_usage() -> None:
    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_fake_buffered_runner(_CANNED_TURN_STDOUT),
    )
    envelope = await adapter.create_response(_make_request())

    assert envelope.status == "completed"
    assert envelope.model == "gpt-5.5"
    # The single message output item carries the assistant text.
    assert len(envelope.output) == 1
    item = envelope.output[0]
    assert item["type"] == "message"
    assert item["content"][0]["text"] == _AGENT_TEXT
    # Usage is populated (word-count estimate).
    assert envelope.usage is not None
    assert envelope.usage["output_tokens"] >= 1
    # Stored for previous_response_id chaining / get_response.
    fetched = await adapter.get_response(envelope.id)
    assert fetched.id == envelope.id


# --- streaming --------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_response_emits_canonical_sequence_text_matches() -> None:
    adapter = CodexAdapter(
        auth=_valid_auth(),
        stream_cli_runner=_fake_stream_runner(_CANNED_TURN_LINES),
    )
    events = await _collect(adapter.stream_response(_make_request(stream=True)))
    names = [event.event for event in events]

    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    assert names[-1] == "response.completed"
    # Concatenated text deltas equal the assistant text.
    deltas = [
        event.data["delta"]
        for event in events
        if event.event == "response.output_text.delta"
    ]
    assert "".join(deltas) == _AGENT_TEXT
    # The terminal completed body carries the message item.
    completed = events[-1].data["response"]
    assert completed["output"][0]["content"][0]["text"] == _AGENT_TEXT


# --- OAuth gate fail-closed -------------------------------------------------


@pytest.mark.asyncio
async def test_create_response_fails_closed_without_artifact(tmp_path) -> None:
    spawned = False

    def _tripwire(prompt: str, model_flag: str) -> str:
        nonlocal spawned
        spawned = True
        return ""

    adapter = CodexAdapter(auth=_missing_auth(tmp_path), cli_runner=_tripwire)
    with pytest.raises(CodexAuthError):
        await adapter.create_response(_make_request())
    assert spawned is False, "codex must NOT be spawned when the gate fails closed"


@pytest.mark.asyncio
async def test_stream_response_fails_closed_without_artifact(tmp_path) -> None:
    spawned = False

    async def _tripwire(prompt: str, model_flag: str) -> AsyncIterator[str]:
        nonlocal spawned
        spawned = True
        if False:  # pragma: no cover - never yields
            yield ""

    adapter = CodexAdapter(auth=_missing_auth(tmp_path), stream_cli_runner=_tripwire)
    with pytest.raises(CodexAuthError):
        await _collect(adapter.stream_response(_make_request(stream=True)))
    assert spawned is False, "codex must NOT be spawned when the gate fails closed"


# --- NO-DIVERGENCE COUPLING TEST (C1 / G002-deferred) -----------------------


@pytest.mark.asyncio
async def test_valid_gate_failing_codex_surfaces_structured_error_nonstreaming() -> (
    None
):
    """Valid OAuth gate + failing codex exec -> structured error, no false-green."""

    def _failing_runner(prompt: str, model_flag: str) -> str:
        # The real spine raises the provider-typed error (CodexAuthError) on a
        # nonzero exit; emulate that contract here.
        raise CodexAuthError("codex CLI invocation failed")

    adapter = CodexAdapter(auth=_valid_auth(), cli_runner=_failing_runner)
    with pytest.raises(CodexAuthError):
        await adapter.create_response(_make_request())


@pytest.mark.asyncio
async def test_valid_gate_failing_codex_surfaces_structured_error_streaming() -> None:
    """Valid gate + failing stream AND failing buffered fallback -> structured error.

    The stream runner fails before any fragment, which folds into the buffered
    fallback per the documented window; the buffered runner ALSO fails, so the
    turn must surface a STRUCTURED CodexAuthError (no false-green, no unhandled
    exception). This is the streaming half of the no-divergence coupling test.
    """

    async def _failing_stream(prompt: str, model_flag: str) -> AsyncIterator[str]:
        raise BoundedCliStreamFailure("codex CLI exited rc=1", returncode=1)
        if False:  # pragma: no cover
            yield ""

    def _failing_buffered(prompt: str, model_flag: str) -> str:
        raise CodexAuthError("codex CLI invocation failed")

    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_failing_buffered,
        stream_cli_runner=_failing_stream,
    )
    with pytest.raises(CodexAuthError):
        await _collect(adapter.stream_response(_make_request(stream=True)))


# --- text-only ceiling (pre-mortem 3) ---------------------------------------


@pytest.mark.asyncio
async def test_command_execution_observation_yields_text_only_nonstreaming() -> None:
    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_fake_buffered_runner(_CANNED_TOOL_TURN_STDOUT),
    )
    envelope = await adapter.create_response(_make_request())

    # Exactly one output item, of type message; NO function_call / tool_use item.
    assert len(envelope.output) == 1
    assert envelope.output[0]["type"] == "message"
    assert all(item["type"] != "function_call" for item in envelope.output)
    assert envelope.output[0]["content"][0]["text"] == _AGENT_TEXT


@pytest.mark.asyncio
async def test_command_execution_observation_yields_text_only_streaming() -> None:
    adapter = CodexAdapter(
        auth=_valid_auth(),
        stream_cli_runner=_fake_stream_runner(_CANNED_TOOL_TURN_LINES),
    )
    events = await _collect(adapter.stream_response(_make_request(stream=True)))
    names = [event.event for event in events]

    # No function_call argument events leak from the command_execution observation.
    assert "response.function_call_arguments.delta" not in names
    assert "response.function_call_arguments.done" not in names
    deltas = [
        event.data["delta"]
        for event in events
        if event.event == "response.output_text.delta"
    ]
    assert "".join(deltas) == _AGENT_TEXT
    completed = events[-1].data["response"]
    assert all(item["type"] != "function_call" for item in completed["output"])


# --- list_models ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_returns_exactly_the_five_gpt_ids() -> None:
    adapter = CodexAdapter(auth=_valid_auth())
    listing = await adapter.list_models()

    ids = [model["id"] for model in listing.data]
    assert ids == [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "gpt-4.1",
    ]


# --- MINOR-2(a): empty-turn contract (no agent_message, exit 0) -------------


_EMPTY_TURN_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "thread-empty"}),
    json.dumps({"type": "turn.completed"}),
]
_EMPTY_TURN_STDOUT = "\n".join(_EMPTY_TURN_LINES) + "\n"


@pytest.mark.asyncio
async def test_empty_turn_create_response_returns_valid_empty_envelope() -> None:
    """A turn with no agent_message yields an empty completion, not an error.

    Pins the MINOR-1 contract: zero-output turns are valid empty completions.
    """
    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_fake_buffered_runner(_EMPTY_TURN_STDOUT),
    )
    envelope = await adapter.create_response(_make_request())

    assert envelope.status == "completed"
    assert len(envelope.output) == 1
    assert envelope.output[0]["content"][0]["text"] == ""
    assert envelope.usage is not None
    assert envelope.usage["output_tokens"] == 0


@pytest.mark.asyncio
async def test_empty_turn_stream_response_no_double_spawn() -> None:
    """A clean zero-fragment stream builds an empty envelope without re-invoking cli_runner.

    The buffered _cli_runner tripwire must NEVER fire: the empty stream is a
    valid completion and must not trigger a second codex invocation (MINOR-4).
    """
    spawned = False

    def _tripwire(prompt: str, model_flag: str) -> str:
        nonlocal spawned
        spawned = True
        return "should-not-be-called"

    async def _empty_stream(prompt: str, model_flag: str) -> AsyncIterator[str]:
        # Yields nothing; terminates cleanly (StopAsyncIteration).
        return
        yield  # make this an async generator  # noqa: unreachable

    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_tripwire,
        stream_cli_runner=_empty_stream,
    )
    events = await _collect(adapter.stream_response(_make_request(stream=True)))

    assert spawned is False, "_cli_runner must NOT be called for a clean empty stream"
    names = [event.event for event in events]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    completed = events[-1].data["response"]
    assert completed["output"][0]["content"][0]["text"] == ""


# --- MINOR-2(b): two-agent_message turn aggregation -------------------------

_TWO_MSG_TEXT_A = "First part."
_TWO_MSG_TEXT_B = "Second part."
_TWO_MSG_EXPECTED = f"{_TWO_MSG_TEXT_A}\n{_TWO_MSG_TEXT_B}"

_TWO_MSG_TURN_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "thread-two"}),
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _TWO_MSG_TEXT_A},
        }
    ),
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _TWO_MSG_TEXT_B},
        }
    ),
    json.dumps({"type": "turn.completed"}),
]
_TWO_MSG_TURN_STDOUT = "\n".join(_TWO_MSG_TURN_LINES) + "\n"


@pytest.mark.asyncio
async def test_two_agent_messages_aggregated_buffered() -> None:
    """Two agent_message events are newline-joined in the buffered (create_response) path."""
    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_fake_buffered_runner(_TWO_MSG_TURN_STDOUT),
    )
    envelope = await adapter.create_response(_make_request())

    assert envelope.output[0]["content"][0]["text"] == _TWO_MSG_EXPECTED


@pytest.mark.asyncio
async def test_two_agent_messages_aggregated_streaming() -> None:
    """Two agent_message fragments each become a separate delta; the terminal
    response.completed body carries the replay_incremental-accumulated full text
    (fragments concatenated without separator, matching replay_incremental's
    "".join contract, not the buffered path's newline-join).
    """
    adapter = CodexAdapter(
        auth=_valid_auth(),
        stream_cli_runner=_fake_stream_runner(_TWO_MSG_TURN_LINES),
    )
    events = await _collect(adapter.stream_response(_make_request(stream=True)))

    deltas = [
        event.data["delta"]
        for event in events
        if event.event == "response.output_text.delta"
    ]
    # Two separate deltas, one per agent_message fragment.
    assert deltas == [_TWO_MSG_TEXT_A, _TWO_MSG_TEXT_B]
    # replay_incremental accumulates with "".join (no separator between deltas).
    accumulated = "".join(deltas)
    completed = events[-1].data["response"]
    assert completed["output"][0]["content"][0]["text"] == accumulated


# --- MINOR-2(c): garbled turn resilience ------------------------------------

_GARBLED_TURN_STDOUT = (
    "\n".join(
        [
            "{not json",
            "",
            json.dumps({"type": "thread.started", "thread_id": "thread-garbled"}),
            '["a", "list", "not", "a", "dict"]',
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": _AGENT_TEXT},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    + "\n"
)


@pytest.mark.asyncio
async def test_garbled_lines_do_not_crash_and_yield_clean_text() -> None:
    """Interleaved non-JSON, blank, and non-dict lines are skipped; no crash."""
    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_fake_buffered_runner(_GARBLED_TURN_STDOUT),
    )
    envelope = await adapter.create_response(_make_request())

    assert envelope.status == "completed"
    assert envelope.output[0]["content"][0]["text"] == _AGENT_TEXT


# --- MINOR-3: _codex_model_flag parametrized --------------------------------


@pytest.mark.parametrize(
    "model_id",
    ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "gpt-4.1"],
)
def test_codex_model_flag_identity_for_known_ids(model_id: str) -> None:
    """Each of the five served gpt ids maps to itself as the --model flag."""
    assert _codex_model_flag(model_id) == model_id


def test_codex_model_flag_none_returns_default() -> None:
    assert _codex_model_flag(None) == _DEFAULT_CODEX_MODEL_FLAG


def test_codex_model_flag_bogus_returns_default() -> None:
    assert _codex_model_flag("bogus-model-xyz") == _DEFAULT_CODEX_MODEL_FLAG
