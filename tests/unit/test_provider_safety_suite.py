"""Consolidated cross-provider safety reference suite (G004 safety matrix).

This suite ties the G004 falsifiable safety guarantees together as a single
discoverable place, WITHOUT re-implementing logic already proven in the
per-adapter unit tests (test_auggie_adapter, test_deepseek_adapter) or the
no-hidden-execution suite. It is the "safety-suite level" cross-check the
test-spec asks for, exercised uniformly across both new providers where the
guarantee is shared:

- The Auggie indexing caveat is the EXACT literal ``hard-disable unproven`` that
  AuggieAdapter.list_models() emits, and the weaker word ``disabled`` is never
  used as the indexing value.
- The Auggie default workspace root passed to the CLI is an ephemeral sandbox
  under the OS temp dir and never equals/contains a caller-supplied workspace.
- A secret sentinel never appears in any adapter response envelope, serialized
  output, or captured logs, asserted UNIFORMLY across auggie and deepseek.

No real ``auggie`` binary, OAuth session, or DeepSeek endpoint is touched: the
Auggie session is forced via the AUGMENT_SESSION_AUTH env seam with a benign
cli_runner, and DeepSeek rides an httpx MockTransport.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import httpx
import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters.auggie import (
    INDEXING_CAVEAT,
    AuggieAdapter,
    _build_completion_argv,
)
from reverso.protocols.adapters.deepseek import DeepSeekAdapter

AUGGIE_SESSION_ENV = "AUGMENT_SESSION_AUTH"
AUGGIE_SESSION_SENTINEL = "augment-session-SENTINEL-do-not-leak-7e6d5c4b"
DEEPSEEK_API_KEY_SENTINEL = "sk-DEEPSEEKsentinelKEY-safety-suite-3c2b1a09"


def _auggie_models_runner(models: list) -> object:
    def runner():
        return {"models": models}

    return runner


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=300.0)

    return factory


def _deepseek_chat_body(text: str = "benign reply") -> dict:
    return {
        "id": "chatcmpl-fake",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# --------------------------------------------------------------------------- #
# Auggie indexing caveat literal (cross-check at the safety-suite level).       #
# --------------------------------------------------------------------------- #


async def test_auggie_list_models_emits_hard_disable_unproven_literal() -> None:
    """list_models() carries the EXACT literal and never the weaker 'disabled'."""
    adapter = AuggieAdapter(
        cli_runner=lambda prompt, model: "ok",
        models_runner=_auggie_models_runner(
            [{"id": "auggie-default"}, {"id": "auggie-pro"}]
        ),
    )

    models = await adapter.list_models()

    serialized = json.dumps({"data": models.data, "models": models.models})
    assert INDEXING_CAVEAT == "hard-disable unproven"
    assert "hard-disable unproven" in serialized
    for model in models.data:
        assert model["indexing"] == "hard-disable unproven"
        # Falsifiable: substituting the weaker claim must fail here.
        assert model["indexing"] != "disabled"
    assert '"disabled"' not in serialized


# --------------------------------------------------------------------------- #
# Auggie ephemeral sandbox workspace root (never the caller's workspace).       #
# --------------------------------------------------------------------------- #


def test_auggie_default_workspace_root_is_ephemeral_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real runner's --workspace-root is an OS-temp sandbox, never the caller.

    Drives the real CLI runner with subprocess.run stubbed to capture argv, then
    asserts the resolved workspace root lives under the OS temp dir and does not
    equal or contain a caller-supplied workspace path.
    """
    monkeypatch.setenv(AUGGIE_SESSION_ENV, AUGGIE_SESSION_SENTINEL)
    caller_workspace = "/Users/someone/private-caller-workspace"
    captured: dict = {}

    class _Completed:
        stdout = json.dumps({"response": "sandbox-ok"})

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(
        "reverso.protocols.adapters.cli_spine.subprocess.run", _fake_run
    )

    adapter = AuggieAdapter()
    text = adapter._run_auggie_cli("prompt", "auggie-default")

    assert text == "sandbox-ok"
    argv = captured["argv"]
    ws_value = argv[argv.index("--workspace-root") + 1]
    temp_root = os.path.realpath(tempfile.gettempdir())
    # The sandbox is created under the OS temp dir with the reverso prefix.
    assert os.path.realpath(ws_value).startswith(temp_root)
    assert "reverso-auggie-" in ws_value
    # The caller's workspace is never used, contained, or matched.
    assert ws_value != caller_workspace
    assert caller_workspace not in ws_value
    assert caller_workspace not in argv


def test_auggie_pure_argv_builder_keeps_caller_workspace_out() -> None:
    """The pure argv builder never substitutes a caller workspace for the sandbox."""
    caller_workspace = "/Users/someone/private-caller-workspace"
    sandbox_root = os.path.join(tempfile.gettempdir(), "reverso-auggie-pure")
    argv = _build_completion_argv("the prompt", "auggie-default", sandbox_root)

    ws_value = argv[argv.index("--workspace-root") + 1]
    assert ws_value == sandbox_root
    assert ws_value != caller_workspace
    assert caller_workspace not in argv
    assert "--ask" in argv


# --------------------------------------------------------------------------- #
# Cross-provider secret non-leakage (parametrized over auggie + deepseek).      #
# --------------------------------------------------------------------------- #


async def _run_auggie_turn(caplog: pytest.LogCaptureFixture) -> tuple[str, object]:
    """Drive one benign Auggie turn; return (injected sentinel, envelope)."""
    adapter = AuggieAdapter(cli_runner=lambda prompt, model: "benign assistant reply")
    request = ResponsesRequest(model="auggie-default", input="hi")
    with caplog.at_level(logging.DEBUG):
        envelope = await adapter.create_response(request)
    return AUGGIE_SESSION_SENTINEL, envelope


async def _run_deepseek_turn(caplog: pytest.LogCaptureFixture) -> tuple[str, object]:
    """Drive one benign DeepSeek turn; return (injected sentinel, envelope)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_deepseek_chat_body(text="benign reply"))

    adapter = DeepSeekAdapter(client_factory=_mock_client_factory(handler))
    request = ResponsesRequest.from_payload({"model": "deepseek-chat", "input": "hi"})
    with caplog.at_level(logging.DEBUG):
        envelope = await adapter.create_response(request)
    return DEEPSEEK_API_KEY_SENTINEL, envelope


@pytest.mark.parametrize("provider", ["auggie", "deepseek"])
async def test_secret_sentinel_never_leaks_for_any_provider(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A credential sentinel never reaches the envelope, output, or logs.

    The same guarantee is asserted UNIFORMLY for both new providers. The Auggie
    sentinel is injected via AUGMENT_SESSION_AUTH (existence seam); the DeepSeek
    sentinel via DEEPSEEK_API_KEY. Both adapters run a benign turn through their
    injected backends, so no real process or network is touched.
    """
    if provider == "auggie":
        monkeypatch.setenv(AUGGIE_SESSION_ENV, AUGGIE_SESSION_SENTINEL)
        sentinel, envelope = await _run_auggie_turn(caplog)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY_SENTINEL)
        sentinel, envelope = await _run_deepseek_turn(caplog)

    assert sentinel not in caplog.text
    assert sentinel not in json.dumps(envelope.raw)
    assert sentinel not in json.dumps(envelope.output)
    assert sentinel not in str(envelope.id)
