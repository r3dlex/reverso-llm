"""Falsifiable no-hidden-execution guarantee for the Auggie and DeepSeek adapters.

This is the genuinely missing G004 safety test (test-spec: Auggie "Provider-native
tool tests" and DeepSeek tool behavior). It proves that handling a single
create_response turn does NOT secretly execute extra tools, spawn hidden
processes, or make hidden network calls:

- AuggieAdapter drives exactly one CLI backend invocation per turn and spawns no
  other subprocess. Two independent counters back this: the injected
  ``cli_runner`` seam counts backend calls, and a monkeypatched
  ``subprocess.run`` spy asserts the REAL subprocess is never touched while the
  injected runner stands in (so the real spawn count must be 0). A tool/function
  call style payload still yields exactly one backend call (no auto-execution).
- DeepSeekAdapter performs exactly one upstream HTTP POST per turn, counted via
  the injected httpx MockTransport handler. Even when the upstream body carries
  ``tool_calls``, those are surfaced as ``function_call`` output items and NO
  second/follow-up HTTP call is made (no hidden tool-execution loop).

Every assertion is falsifiable: introducing a hidden second subprocess, a hidden
network call, or auto tool execution flips the counter and fails the test. No
real ``auggie`` binary, OAuth session, or DeepSeek endpoint is touched.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.auggie import AuggieAdapter, _parse_completion_output
from reverso.protocols.adapters.deepseek import DeepSeekAdapter

AUGGIE_SESSION_ENV = "AUGMENT_SESSION_AUTH"
DEEPSEEK_API_KEY_SENTINEL = "sk-DEEPSEEKsentinelKEY-no-hidden-exec-1a2b3c4d"


@pytest.fixture(autouse=True)
def _auggie_session_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a resolvable Auggie OAuth session via env (no real token)."""
    monkeypatch.setenv(AUGGIE_SESSION_ENV, "fake-session-not-a-real-token")


def _mock_client_factory(handler):
    """Build a DeepSeek client_factory backed by an httpx.MockTransport.

    No real network call is ever made; the handler counts and answers requests.
    """
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


def _deepseek_chat_body(text: str = "ok", **extra) -> dict:
    message = {"role": "assistant", "content": text}
    message.update(extra)
    return {
        "id": "chatcmpl-fake",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# --------------------------------------------------------------------------- #
# Auggie: exactly one CLI backend call, zero real subprocess spawns per turn.  #
# --------------------------------------------------------------------------- #


async def test_auggie_turn_invokes_cli_backend_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One create_response turn drives the CLI backend exactly once.

    Independently spy on the real subprocess.run: with cli_runner injected, the
    real subprocess must NEVER be spawned (spawn count == 0). A hidden second
    process spawn would flip either counter and fail.
    """
    runner_calls = {"n": 0}
    subprocess_calls = {"n": 0}

    def _real_subprocess_spy(*args, **kwargs):  # pragma: no cover - must not run
        subprocess_calls["n"] += 1
        raise AssertionError("real subprocess.run must not be spawned this turn")

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _real_subprocess_spy
    )

    def _spy_runner(prompt: str, model: str) -> str:
        runner_calls["n"] += 1
        return "Single completion only."

    adapter = AuggieAdapter(cli_runner=_spy_runner)
    request = ResponsesRequest(model="auggie-default", input="do work")

    envelope = await adapter.create_response(request)

    assert runner_calls["n"] == 1, "exactly one CLI backend call per turn"
    assert subprocess_calls["n"] == 0, "no real subprocess spawned with injected runner"
    assert envelope.output[0]["content"][0]["text"] == "Single completion only."


async def test_auggie_tool_style_payload_does_not_auto_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool/function-call style backend payload triggers no extra execution.

    The runner surfaces a tool_calls payload as text; the adapter must NOT loop
    back to execute the tool, so the backend is still called exactly once and the
    real subprocess is never spawned.
    """
    runner_calls = {"n": 0}

    def _real_subprocess_spy(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("real subprocess.run must not be spawned this turn")

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _real_subprocess_spy
    )

    tool_payload = json.dumps(
        {
            "response": "I would call read_file.",
            "tool_calls": [{"name": "read_file", "arguments": {"path": "/etc/passwd"}}],
        }
    )

    def _spy_runner(prompt: str, model: str) -> str:
        runner_calls["n"] += 1
        return _parse_completion_output(tool_payload)

    adapter = AuggieAdapter(cli_runner=_spy_runner)
    request = ResponsesRequest(
        model="auggie-default",
        input="please read a file",
        tools=[{"type": "function", "name": "read_file"}],
    )

    envelope = await adapter.create_response(request)

    assert runner_calls["n"] == 1, "tool-style payload must not trigger a second call"
    assert envelope.output[0]["content"][0]["text"] == "I would call read_file."


async def test_auggie_stream_turn_invokes_cli_backend_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draining a streaming turn drives the CLI backend exactly once.

    The stream is backed by a single completion; replaying the SSE event sequence
    must not re-invoke the backend or spawn any real subprocess.
    """
    runner_calls = {"n": 0}

    def _real_subprocess_spy(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("real subprocess.run must not be spawned this turn")

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _real_subprocess_spy
    )

    def _spy_runner(prompt: str, model: str) -> str:
        runner_calls["n"] += 1
        return "Streamed once."

    adapter = AuggieAdapter(cli_runner=_spy_runner)
    request = ResponsesRequest(model="auggie-default", input="hi", stream=True)

    events = [event async for event in adapter.stream_response(request)]

    assert events, "stream must yield events"
    assert runner_calls["n"] == 1, "streaming a turn must call the backend once"


# --------------------------------------------------------------------------- #
# DeepSeek: exactly one upstream POST per turn, even with tool_calls present.   #
# --------------------------------------------------------------------------- #


async def test_deepseek_turn_makes_exactly_one_upstream_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One create_response turn makes exactly one upstream chat POST."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY_SENTINEL)
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200, json=_deepseek_chat_body(text="single answer"))

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})

    await adapter.create_response(request)

    assert len(posts) == 1, "exactly one upstream POST per turn"
    assert posts[0].endswith("/chat/completions")


async def test_deepseek_tool_calls_surfaced_with_no_followup_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tool_calls are surfaced as function_call items with NO second HTTP call.

    A hidden tool-execution loop would issue a second POST; the counter must stay
    at exactly one. The function_call output item proves the calls are surfaced,
    not executed.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY_SENTINEL)
    posts: list[str] = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(
            200, json=_deepseek_chat_body(text="", tool_calls=tool_calls)
        )

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "weather in Paris?"}
    )

    envelope = await adapter.create_response(request)

    function_calls = [o for o in envelope.output if o["type"] == "function_call"]
    assert function_calls, "tool_calls must surface as function_call output items"
    assert function_calls[0]["name"] == "get_weather"
    assert function_calls[0]["arguments"] == '{"city":"Paris"}'
    # Falsifiable: a hidden auto-execution loop would make a second POST.
    assert len(posts) == 1, "tool_calls must not trigger a follow-up upstream call"


async def test_deepseek_stream_turn_makes_exactly_one_upstream_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draining a streaming turn makes exactly one upstream chat POST."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY_SENTINEL)
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(str(request.url))
        return httpx.Response(200, json=_deepseek_chat_body(text="streamed once"))

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    request = ResponsesRequest.from_payload(
        {"model": "deepseek-chat", "input": "hi", "stream": True}
    )

    events = [event async for event in adapter.stream_response(request)]

    assert events, "stream must yield events"
    assert len(posts) == 1, "streaming a turn must make exactly one upstream POST"
