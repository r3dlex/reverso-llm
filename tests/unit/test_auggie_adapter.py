"""Unit tests for the Auggie subprocess provider adapter (ADR 0003).

All backends are FAKE: no real ``auggie`` binary, no OAuth session, no network.
Covers the Responses contract surface, the falsifiable ``hard-disable unproven``
indexing literal, the repo-workspace-root write-capable posture, the streamed
SSE event order, bounded error handling, and that no hidden execution occurs
after a tool result is surfaced.
"""

from __future__ import annotations

import json

import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
)
from reverso.protocols.adapters.auggie import (
    INDEXING_CAVEAT,
    AuggieAdapter,
    AuggieError,
    _build_completion_argv,
    _parse_completion_output,
    _resolve_workspace_root,
)


@pytest.fixture(autouse=True)
def _session_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a resolvable OAuth session via the env fallback (no real token)."""
    monkeypatch.setenv("AUGMENT_SESSION_AUTH", "fake-session-not-a-real-token")


def _models_runner(models: list) -> object:
    def runner():
        return {"models": models}

    return runner


def test_adapter_satisfies_protocol() -> None:
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "ok")
    assert isinstance(adapter, ProviderAdapter)


async def test_create_response_maps_and_stores() -> None:
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "Hello from Auggie.")
    request = ResponsesRequest.from_payload(
        {"model": "auggie-default", "input": [{"role": "user", "content": "hi"}]}
    )

    envelope = await adapter.create_response(request)

    assert isinstance(envelope, ResponseEnvelope)
    assert envelope.status == "completed"
    assert envelope.output[0]["content"][0]["text"] == "Hello from Auggie."

    stored = await adapter.get_response(envelope.id)
    assert stored.id == envelope.id
    items = await adapter.list_input_items(envelope.id)
    assert isinstance(items, InputItemList)
    assert items.data == [{"role": "user", "content": "hi"}]


async def test_stream_response_event_order() -> None:
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "Hi there.")
    request = ResponsesRequest(model="auggie-default", input="Say hi.", stream=True)

    events = [event.event async for event in adapter.stream_response(request)]

    assert events == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]


async def test_stream_response_completed_carries_text() -> None:
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "Streamed body.")
    request = ResponsesRequest(model="auggie-default", input="hi", stream=True)

    events = [event async for event in adapter.stream_response(request)]

    delta = next(e for e in events if e.event == "response.output_text.delta")
    assert delta.data["delta"] == "Streamed body."
    completed = events[-1]
    assert completed.event == "response.completed"
    assert completed.data["response"]["status"] == "completed"


async def test_stream_stores_response_before_full_drain() -> None:
    # A client that disconnects mid-stream must still leave the response stored.
    # Falsifiable: storing only after response.completed (the prior ordering)
    # would make get_response raise here because the stream is closed early.
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "Streamed body.")
    request = ResponsesRequest(model="auggie-default", input="hi", stream=True)

    gen = adapter.stream_response(request)
    first = await gen.__anext__()
    response_id = first.data["response"]["id"]
    await gen.aclose()

    stored = await adapter.get_response(response_id)
    assert stored.id == response_id


async def test_list_models_embeds_indexing_caveat_literal() -> None:
    adapter = AuggieAdapter(
        cli_runner=lambda prompt, model: "ok",
        models_runner=_models_runner([{"id": "auggie-default"}, {"id": "auggie-pro"}]),
    )

    models = await adapter.list_models()

    assert isinstance(models, ModelList)
    ids = [m["id"] for m in models.data]
    assert ids == ["auggie-default", "auggie-pro"]
    serialized = json.dumps({"data": models.data, "models": models.models})
    # Falsifiable literal must be present verbatim and NOT the weaker "disabled".
    assert INDEXING_CAVEAT == "hard-disable unproven"
    assert "hard-disable unproven" in serialized
    for model in models.data:
        assert model["indexing"] == "hard-disable unproven"
        assert model["indexing"] != "disabled"
    assert '"disabled"' not in serialized


async def test_list_models_maps_live_cli_short_names() -> None:
    """The live CLI registry keys models by ``shortName``, not ``id``."""
    adapter = AuggieAdapter(
        cli_runner=lambda prompt, model: "ok",
        models_runner=_models_runner(
            [
                {"displayName": "Prism (Claude + Gemini)", "shortName": "prism-a"},
                {"displayName": "Haiku 4.5", "shortName": "haiku4.5"},
            ]
        ),
    )

    models = await adapter.list_models()

    assert [m["id"] for m in models.data] == ["prism-a", "haiku4.5"]


async def test_list_models_exposes_codex_friendly_claude_aliases() -> None:
    """Codex should get metadata for common Claude-style Auggie names."""
    adapter = AuggieAdapter(
        cli_runner=lambda prompt, model: "ok",
        models_runner=_models_runner(
            [
                {"displayName": "Claude Fable 5", "shortName": "fable-5"},
                {"displayName": "Opus 4.8", "shortName": "opus4.8"},
                {"displayName": "Sonnet 4.6", "shortName": "sonnet4.6"},
            ]
        ),
    )

    models = await adapter.list_models()

    ids = [m["id"] for m in models.data]
    assert "fable-5" in ids
    assert "claude-fable-5" in ids
    assert "opus4.8" in ids
    assert "opus-4-8" in ids
    assert "claude-opus-4-8" in ids
    assert "sonnet4.6" in ids
    assert "sonnet-4-6" in ids
    assert "claude-sonnet-4-6" in ids


def test_completion_argv_uses_repo_root_and_write_permissions(tmp_path) -> None:
    """The argv targets the repo workspace and does not force ask mode."""
    workspace_root = str(tmp_path / "repo")
    argv = _build_completion_argv("the prompt", "auggie-default", workspace_root)

    assert argv[0] == "auggie"
    assert "--print" in argv
    assert "--ask" not in argv
    assert "--workspace-root" in argv
    ws_value = argv[argv.index("--workspace-root") + 1]
    assert ws_value == workspace_root
    permissions = [
        argv[index + 1] for index, value in enumerate(argv) if value == "--permission"
    ]
    assert "save-file:allow" in permissions
    assert "str-replace-editor:allow" in permissions
    assert "apply_patch:allow" in permissions
    assert "remove-files:allow" not in permissions
    # Output is one-shot and parseable.
    assert argv[argv.index("--output-format") + 1] == "json"


def test_completion_argv_maps_claude_style_model_aliases(tmp_path) -> None:
    workspace_root = str(tmp_path / "repo")

    opus = _build_completion_argv("prompt", "opus-4-8", workspace_root)
    sonnet = _build_completion_argv("prompt", "claude-sonnet-4-6", workspace_root)
    fable = _build_completion_argv("prompt", "claude-fable-5", workspace_root)

    assert opus[opus.index("-m") + 1] == "opus4.8"
    assert sonnet[sonnet.index("-m") + 1] == "sonnet4.6"
    assert fable[fable.index("-m") + 1] == "fable-5"


def test_prompt_flags_stay_after_separator(tmp_path) -> None:
    """A prompt cannot inject argv flags before the separator."""
    workspace_root = str(tmp_path / "repo")
    prompt = "ignore prior text --ask --workspace-root /tmp/bad"
    argv = _build_completion_argv(prompt, "auggie-default", workspace_root)

    separator = argv.index("--")
    assert prompt == argv[separator + 1]
    assert "--ask" not in argv[:separator]
    assert argv[argv.index("--workspace-root") + 1] == workspace_root


def test_resolve_workspace_root_defaults_to_git_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(nested)

    assert _resolve_workspace_root() == str(repo.resolve())


def test_resolve_workspace_root_honors_explicit_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    override = tmp_path / "explicit-root"
    monkeypatch.setenv("REVERSO_AUGGIE_WORKSPACE_ROOT", str(override))

    assert _resolve_workspace_root() == str(override.resolve())


def test_real_runner_defaults_workspace_to_repo_root(monkeypatch, tmp_path) -> None:
    """The real CLI runner builds argv with the caller's repository root."""
    captured: dict = {}
    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    class _Completed:
        stdout = json.dumps({"response": "repo-ok"})

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _fake_run
    )
    monkeypatch.chdir(nested)

    adapter = AuggieAdapter()
    text = adapter._run_auggie_cli("prompt", "auggie-default")

    assert text == "repo-ok"
    argv = captured["argv"]
    ws_value = argv[argv.index("--workspace-root") + 1]
    assert ws_value == str(repo.resolve())
    assert "--ask" not in argv
    assert "--print" in argv


async def test_missing_auth_raises_bounded_error(monkeypatch) -> None:
    monkeypatch.delenv("AUGMENT_SESSION_AUTH", raising=False)
    monkeypatch.setattr(
        "reverso.protocols.adapters.auggie._SESSION_PATH",
        __import__("pathlib").Path("/nonexistent/reverso/session.json"),
    )

    def _must_not_run(prompt: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("CLI must not run when auth fails")

    adapter = AuggieAdapter(cli_runner=_must_not_run)
    request = ResponsesRequest(model="auggie-default", input="hi")

    with pytest.raises(AuggieError) as excinfo:
        await adapter.create_response(request)
    assert "fake-session" not in str(excinfo.value)


def test_missing_cli_raises_bounded_error(monkeypatch) -> None:
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("auggie")

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _fake_run
    )
    adapter = AuggieAdapter()

    with pytest.raises(AuggieError) as excinfo:
        adapter._run_auggie_cli("prompt", "auggie-default")
    assert "not found" in str(excinfo.value)


def test_nonzero_exit_raises_bounded_error_without_secret(monkeypatch) -> None:
    import subprocess as _subprocess

    secret_stderr = "boom token=SECRET-leak-1234567890"

    def _fake_run(argv, **kwargs):
        raise _subprocess.CalledProcessError(
            returncode=2, cmd=argv, stderr=secret_stderr
        )

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _fake_run
    )
    adapter = AuggieAdapter()

    with pytest.raises(AuggieError) as excinfo:
        adapter._run_auggie_cli("prompt", "auggie-default")
    message = str(excinfo.value)
    assert "SECRET" not in message
    assert secret_stderr not in message


def test_timeout_raises_bounded_error(monkeypatch) -> None:
    import subprocess as _subprocess

    def _fake_run(argv, **kwargs):
        raise _subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _fake_run
    )
    adapter = AuggieAdapter()

    with pytest.raises(AuggieError) as excinfo:
        adapter._run_auggie_cli("prompt", "auggie-default")
    assert "timed out" in str(excinfo.value)


async def test_no_hidden_execution_after_tool_result_surfaced() -> None:
    """A surfaced tool result must NOT trigger any further runner invocation."""
    calls = {"n": 0}

    tool_payload = json.dumps(
        {
            "response": "I would call a tool.",
            "tool_calls": [{"name": "read_file", "arguments": {"path": "x"}}],
        }
    )

    def _spy_runner(prompt: str, model: str) -> str:
        calls["n"] += 1
        # Surface the tool result as text; the adapter must not execute it.
        return _parse_completion_output(tool_payload)

    adapter = AuggieAdapter(cli_runner=_spy_runner)
    request = ResponsesRequest(model="auggie-default", input="do work")

    envelope = await adapter.create_response(request)

    # Exactly one invocation: the single completion. No tool execution loop.
    assert calls["n"] == 1
    assert envelope.output[0]["content"][0]["text"] == "I would call a tool."


def test_parse_completion_output_handles_plain_text() -> None:
    assert _parse_completion_output("plain text reply") == "plain text reply"
    assert _parse_completion_output(json.dumps({"text": "json reply"})) == "json reply"
    assert _parse_completion_output("") == ""


def test_parse_completion_output_skips_warning_prefix_lines() -> None:
    """Regression: the live CLI prefixes the JSON envelope with warning lines.

    Observed stdout for an unknown model id: a human-readable warning line
    followed by the {"type":"result",...} envelope. The old parser failed
    json.loads on the whole text and leaked the raw envelope to the client.
    """
    envelope = json.dumps(
        {
            "type": "result",
            "result": "ok\n",
            "is_error": False,
            "subtype": "success",
            "session_id": "7cea6f7b",
            "num_turns": 0,
        }
    )
    stdout = (
        'Unknown model: "claude-fable-5", falling back to default model.\n'
        + envelope
        + "\n"
    )
    assert _parse_completion_output(stdout) == "ok\n"

    multi_prefix = "line one\nline two {not json\n" + envelope
    assert _parse_completion_output(multi_prefix) == "ok\n"

    no_envelope = "warning only\nno json here"
    assert _parse_completion_output(no_envelope) == no_envelope

    tool_use_line = json.dumps({"type": "tool_use", "name": "f", "arguments": {}})
    assert _parse_completion_output(tool_use_line + "\n" + envelope) == "ok\n"


def test_parse_completion_output_result_envelope_beats_trailing_dict() -> None:
    """A trailing diagnostic dict must not shadow the real result envelope.

    The reverse scan anchors on type == "result" first; a later JSON line
    with a defensive key (e.g. "message") only wins when no result-typed
    envelope exists anywhere in the output.
    """
    envelope = json.dumps({"type": "result", "result": "ok\n", "is_error": False})
    debug_line = json.dumps({"type": "debug", "message": "model fallback engaged"})

    stdout = "warning line\n" + envelope + "\n" + debug_line
    assert _parse_completion_output(stdout) == "ok\n"

    # No result-typed envelope at all: the defensive key pass still applies.
    defensive_only = "warning line\n" + debug_line
    assert _parse_completion_output(defensive_only) == "model fallback engaged"


async def test_b4_multiturn_message_list_is_role_labeled_in_prompt() -> None:
    """A role-tagged message list reaches the CLI as labeled segments.

    The Auggie spine is a single-shot CLI: the only translation seam is the
    prompt string. The B4 lane labels each role so the model can tell
    speakers apart. Falsifiable: omitting roles would feed an unsegmented
    blob to the CLI and the model could not reconstruct the dialogue.
    """
    captured = {}

    def runner(prompt: str, model: str) -> str:
        captured["prompt"] = prompt
        return "ack"

    adapter = AuggieAdapter(cli_runner=runner)
    request = ResponsesRequest.from_payload(
        {
            "model": "auggie-default",
            "instructions": "Be concise.",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Capital of France?"}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Paris."}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "And Spain?"}],
                },
            ],
        }
    )

    await adapter.create_response(request)

    assert captured["prompt"] == (
        "Be concise.\n\n"
        "User: Capital of France?\n\n"
        "Assistant: Paris.\n\n"
        "User: And Spain?"
    )
