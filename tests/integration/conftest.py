"""Shared loaders and a fixture-driven fake adapter for the parity harness.

The parity suite runs the SAME Codex-observed fixtures (tests/fixtures/responses)
against BOTH the claude and copilot provider paths through the first-party app
(reverso.protocols.responses_app.build_app). To stay provider-agnostic and never
touch real endpoints or credentials, each provider is backed by a FixtureAdapter
that replays the fixture's expected bodies/events and authenticates through the
deterministic fake-auth seam (reverso.protocols.auth.fake_auth).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.auth import fake_auth

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "responses"


def load_manifest() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "manifest.json").read_text())


def load_fixture(file_name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / file_name).read_text())


class FixtureAdapter:
    """A ProviderAdapter that replays fixture expected bodies and SSE events.

    It satisfies the frozen ProviderAdapter Protocol and resolves a deterministic
    fake OAuth credential (no Keychain, no filesystem, no network). It does not
    speak to any real provider; it returns exactly what the active fixture says
    the provider-agnostic contract requires, so claude and copilot are exercised
    identically.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.auth = fake_auth(authenticated=True, method=f"{provider}_fake")
        self._nonstream = load_fixture("create_response_nonstreaming.json")
        self._stream = load_fixture("create_response_streaming.json")
        self._models = load_fixture("list_models.json")
        self._get = load_fixture("get_response.json")
        self._input_items = load_fixture("list_input_items.json")
        self._tools = load_fixture("tools_function_call.json")
        self._chain = load_fixture("previous_response_id_chain.json")
        self._store: dict[str, dict[str, Any]] = {}
        self._seed_store()

    def _seed_store(self) -> None:
        get_body = self._get["expected"]["body"]
        self._store[get_body["id"]] = get_body

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        body = self._select_nonstream_body(request)
        return ResponseEnvelope(
            id=body["id"],
            model=body.get("model", request.model),
            output=body.get("output", []),
            status=body.get("status", "completed"),
            usage=body.get("usage"),
            previous_response_id=body.get("previous_response_id"),
            raw=body,
        )

    def _select_nonstream_body(self, request: ResponsesRequest) -> dict[str, Any]:
        if request.tools:
            return self._tools["expected"]["body"]
        if request.previous_response_id is not None:
            input_items = request.input
            is_tool_output = isinstance(input_items, list) and any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            )
            if is_tool_output:
                return self._tools["followup"]["expected"]["body"]
            return self._chain["turns"][1]["expected"]["body"]
        chain_first = self._chain["turns"][0]
        if request.input == chain_first["request"]["body"]["input"]:
            return chain_first["expected"]["body"]
        return self._nonstream["expected"]["body"]

    async def stream_response(
        self, request: ResponsesRequest
    ) -> AsyncIterator[SSEEvent]:
        for event in self._stream["expected"]["events"]:
            yield SSEEvent(event=event["type"], data=event)

    async def list_models(self) -> ModelList:
        body = self._models["expected"]["body"]
        return ModelList(
            data=body.get("data", []),
            object=body.get("object", "list"),
            models=body.get("models", []),
        )

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        body = self._store.get(response_id, self._get["expected"]["body"])
        return ResponseEnvelope(
            id=body["id"],
            model=body.get("model", ""),
            output=body.get("output", []),
            status=body.get("status", "completed"),
            usage=body.get("usage"),
            raw=body,
        )

    async def list_input_items(self, response_id: str) -> InputItemList:
        body = self._input_items["expected"]["body"]
        return InputItemList(
            response_id=response_id,
            data=body.get("data", []),
            object=body.get("object", "list"),
        )
