"""Runtime-scoped LiteLLM quarantine guard (ADR 0002 D2, test-spec item 11).

LiteLLM is quarantined, not the core router, for the Claude and Copilot
/v1/responses paths. This is enforced at runtime, not by import shape alone: a
trace asserts litellm.proxy.proxy_server.app is invoked ZERO times while the
first-party app handles claude and copilot requests, and a clean-subprocess
import-graph check asserts reverso.protocols.responses_app does not import the
legacy reverso.proxy.app wrapper. An import-level-only assertion is insufficient
because legacy modules may coexist in the same process.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, AsyncIterator

import pytest

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.responses_app import build_app


class _StubAdapter:
    """Minimal in-process adapter; never calls a real provider or LiteLLM."""

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        return ResponseEnvelope(
            id="resp_quarantine_0001",
            model=request.model or "gpt-5.5",
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            ],
        )

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        yield SSEEvent(event="response.completed", data={"type": "response.completed"})

    async def list_models(self) -> ModelList:
        return ModelList(data=[{"id": "gpt-5.5", "object": "model"}])

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        return ResponseEnvelope(id=response_id, model="gpt-5.5")

    async def list_input_items(self, response_id: str) -> InputItemList:
        return InputItemList(response_id=response_id)


async def _drive(app: Any, method: str, path: str, body: bytes = b"") -> int:
    """Drive one ASGI request through the app and return the response status."""
    sent: list[dict[str, Any]] = []
    received = {"done": False}

    async def receive() -> dict[str, Any]:
        if received["done"]:
            return {"type": "http.disconnect"}
        received["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m.get("type") == "http.response.start")
    return int(start["status"])


@pytest.mark.asyncio
async def test_litellm_proxy_app_not_invoked_during_provider_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNTIME: litellm.proxy.proxy_server.app is never called for claude/copilot."""
    import litellm.proxy.proxy_server as proxy_server

    calls: list[tuple[Any, ...]] = []

    async def _tripwire(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(proxy_server, "app", _tripwire, raising=False)

    app = build_app({"claude": _StubAdapter(), "copilot": _StubAdapter()})

    for provider in ("claude", "copilot"):
        status = await _drive(
            app,
            "POST",
            f"/{provider}/v1/responses",
            body=b'{"model":"gpt-5.5","input":"hi"}',
        )
        assert status == 200, f"{provider} create_response should succeed"
        models_status = await _drive(app, "GET", f"/{provider}/v1/models")
        assert models_status == 200, f"{provider} list_models should succeed"
        stream_status = await _drive(
            app,
            "POST",
            f"/{provider}/v1/responses",
            body=b'{"model":"gpt-5.5","input":"hi","stream":true}',
        )
        assert stream_status == 200, f"{provider} streaming should succeed"

    assert calls == [], (
        "litellm.proxy.proxy_server.app must NOT be invoked while the first-party "
        f"app handles claude/copilot requests; observed {len(calls)} call(s)"
    )


def test_responses_app_import_graph_excludes_legacy_proxy_app() -> None:
    """IMPORT GRAPH: importing responses_app must not pull in reverso.proxy.app.

    Checked in a fresh subprocess so a prior in-process import of the legacy app
    by an unrelated test cannot mask a real static-import edge.
    """
    code = (
        "import sys, importlib;"
        "importlib.import_module('reverso.protocols.responses_app');"
        "importlib.import_module('reverso.protocols.middleware');"
        "leaked_app = 'reverso.proxy.app' in sys.modules;"
        "leaked_litellm = any("
        "m == 'litellm.proxy.proxy_server' "
        "or m.startswith('litellm.proxy.proxy_server.') for m in sys.modules);"
        "print('proxy_app=' + ('LEAKED' if leaked_app else 'CLEAN'));"
        "print('litellm_proxy_server=' + ('LEAKED' if leaked_litellm else 'CLEAN'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess import failed: rc={result.returncode}\n{result.stderr}"
    )
    out = result.stdout.strip()
    assert "proxy_app=CLEAN" in out, (
        "reverso.protocols.responses_app must NOT import reverso.proxy.app "
        f"(legacy LiteLLM wrapper); subprocess reported: {out!r}"
    )
    assert "litellm_proxy_server=CLEAN" in out, (
        "reverso.protocols.responses_app must NOT statically import "
        f"litellm.proxy.proxy_server; subprocess reported: {out!r}"
    )
