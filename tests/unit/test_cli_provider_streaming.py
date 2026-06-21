"""Tests for custom CLI provider streaming contracts (anthropic_cli only)."""

from __future__ import annotations

import pytest

from reverso.proxy import anthropic_cli_provider


@pytest.mark.asyncio
async def test_anthropic_astreaming_is_async_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        anthropic_cli_provider,
        "_run_turn_stream",
        lambda prompt, model_flag, workspace, timeout: iter(["O", "K"]),
    )

    stream = anthropic_cli_provider.anthropic_cli.astreaming(
        model="custom/claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
    )

    chunks = [chunk async for chunk in stream]
    assert [chunk["text"] for chunk in chunks[:-1]] == ["O", "K"]
    assert chunks[-1]["is_finished"] is True


@pytest.mark.asyncio
async def test_anthropic_astreaming_preserves_profile_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

    captured = {}

    def fake_run_turn_stream(prompt, model_flag, workspace, timeout):
        captured["workspace"] = workspace
        return iter(["O", "K"])

    monkeypatch.setattr(
        anthropic_cli_provider, "_run_turn_stream", fake_run_turn_stream
    )
    token = CURRENT_PROFILE_WORKSPACE.set("/workspaces/example-repo")
    try:
        stream = anthropic_cli_provider.anthropic_cli.astreaming(
            model="custom/claude-opus-4-8",
            messages=[{"role": "user", "content": "hello"}],
        )
        chunks = [chunk async for chunk in stream]
    finally:
        CURRENT_PROFILE_WORKSPACE.reset(token)

    assert [chunk["text"] for chunk in chunks[:-1]] == ["O", "K"]
    assert captured["workspace"] == "/workspaces/example-repo"


def test_anthropic_streaming_uses_daemon_deltas_and_strips_think(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_cli_provider, "daemon_available", lambda sock: True)
    monkeypatch.setattr(
        anthropic_cli_provider,
        "stream_daemon",
        lambda *args, **kwargs: iter(
            [
                {"type": "delta", "delta": "<think>hidden"},
                {"type": "delta", "delta": "</think>Hello"},
                {"type": "delta", "delta": " there"},
                {
                    "type": "completed",
                    "assistant_text": "Hello there",
                    "session_id": "sid",
                    "observations": [],
                },
            ]
        ),
    )

    chunks = list(
        anthropic_cli_provider.anthropic_cli.streaming(
            model="custom/claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert [chunk["text"] for chunk in chunks[:-1]] == ["Hello", " there"]
    assert chunks[-1]["is_finished"] is True


def test_anthropic_daemon_http_status_error_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    request = httpx.Request("POST", "http://daemon/session/turn")
    response = httpx.Response(500, request=request)

    monkeypatch.setattr(anthropic_cli_provider, "daemon_available", lambda sock: True)
    monkeypatch.setattr(
        anthropic_cli_provider,
        "call_daemon",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.HTTPStatusError("boom", request=request, response=response)
        ),
    )
    monkeypatch.setattr(
        anthropic_cli_provider,
        "_invoke_claude",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not fallback")
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        anthropic_cli_provider._run_turn("hello", "claude-sonnet-4-6", None, 5)


def test_anthropic_daemon_turn_uses_request_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_call_daemon(sock, workspace, provider, user_message, model, timeout):
        captured.update(
            {
                "workspace": workspace,
                "provider": provider,
                "user_message": user_message,
                "model": model,
                "timeout": timeout,
            }
        )
        return {"assistant_text": "OK", "session_id": "sid", "observations": []}

    monkeypatch.setattr(anthropic_cli_provider, "daemon_available", lambda sock: True)
    monkeypatch.setattr(anthropic_cli_provider, "call_daemon", fake_call_daemon)

    text, session_id, observations, warnings = anthropic_cli_provider._run_turn(
        "hello",
        "claude-opus-4-8",
        "/Users/andreburgstahler/Ws/Rib",
        5,
    )

    assert text == "OK"
    assert session_id == "sid"
    assert observations == []
    assert warnings == []
    assert captured == {
        "workspace": "/Users/andreburgstahler/Ws/Rib",
        "provider": "anthropic",
        "user_message": "hello",
        "model": "claude-opus-4-8",
        "timeout": 5.0,
    }


def test_anthropic_completion_reads_profile_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

    captured = {}

    def fake_run_turn(prompt, model_flag, workspace, timeout):
        captured.update(
            {
                "prompt": prompt,
                "model_flag": model_flag,
                "workspace": workspace,
                "timeout": timeout,
            }
        )
        return "OK", "sid", [], []

    monkeypatch.setattr(anthropic_cli_provider, "_run_turn", fake_run_turn)
    token = CURRENT_PROFILE_WORKSPACE.set("/Users/andreburgstahler/Ws/Rib")
    try:
        response = anthropic_cli_provider.anthropic_cli.completion(
            model="custom/claude-opus-4-8",
            messages=[{"role": "user", "content": "hello"}],
            timeout=5,
        )
    finally:
        CURRENT_PROFILE_WORKSPACE.reset(token)

    assert response.choices[0].message.content == "OK"
    assert captured == {
        "prompt": "hello",
        "model_flag": "claude-opus-4-8",
        "workspace": "/Users/andreburgstahler/Ws/Rib",
        "timeout": 5,
    }


def test_anthropic_stateless_fallback_uses_request_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    captured = {}

    def fake_invoke(prompt, model_flag, workspace=None, timeout=300):
        captured.update(
            {
                "prompt": prompt,
                "model_flag": model_flag,
                "workspace": workspace,
                "timeout": timeout,
            }
        )
        return {"result": "OK", "session_id": "fallback-sid"}

    monkeypatch.setattr(anthropic_cli_provider, "daemon_available", lambda sock: True)
    monkeypatch.setattr(
        anthropic_cli_provider,
        "call_daemon",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("no daemon")),
    )
    monkeypatch.setattr(anthropic_cli_provider, "_invoke_claude", fake_invoke)

    text, session_id, observations, warnings = anthropic_cli_provider._run_turn(
        "hello",
        "claude-opus-4-8",
        "/Users/andreburgstahler/Ws/Rib",
        5,
    )

    assert text == "OK"
    assert session_id == "fallback-sid"
    assert observations == []
    assert warnings and warnings[0].startswith("daemon_unavailable:")
    assert captured == {
        "prompt": "hello",
        "model_flag": "claude-opus-4-8",
        "workspace": "/Users/andreburgstahler/Ws/Rib",
        "timeout": 5,
    }
