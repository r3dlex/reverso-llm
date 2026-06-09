"""Stable provider adapter contract for the Responses gateway (ADR 0002 11.3).

These types are the frozen boundary that the Claude and Copilot adapters, the
first-party ASGI app, and the parity harness all import. They are OpenAI
Responses shaped and intentionally minimal: adapters own their internal model
mapping and credential handling behind this Protocol so they can evolve
independently. Field shapes mirror Codex-observed Responses traffic; unknown
keys are carried in ``extra`` rather than typed exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class ResponsesRequest:
    """A normalized OpenAI Responses request bound for a provider adapter.

    ``model`` is the provider-resolved model id, ``input`` is the Responses
    input item list (or a bare string), ``stream`` selects SSE vs unary,
    ``previous_response_id`` carries Codex-observed session chaining, and
    ``tools`` holds function-tool declarations. ``extra`` preserves any other
    request fields without losing them.
    """

    model: str
    input: Any
    stream: bool = False
    previous_response_id: str | None = None
    tools: list[dict[str, Any]] | None = None
    instructions: str | None = None
    tool_choice: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ResponsesRequest":
        """Build a request from a raw Responses POST body."""
        known = {
            "model",
            "input",
            "stream",
            "previous_response_id",
            "tools",
            "instructions",
            "tool_choice",
        }
        extra = {key: value for key, value in payload.items() if key not in known}
        return cls(
            model=payload.get("model", ""),
            input=payload.get("input"),
            stream=bool(payload.get("stream", False)),
            previous_response_id=payload.get("previous_response_id"),
            tools=payload.get("tools"),
            instructions=payload.get("instructions"),
            tool_choice=payload.get("tool_choice"),
            extra=extra,
        )


@dataclass
class ResponseEnvelope:
    """A non-streaming Responses object (the ``response.completed`` body).

    ``id`` is the response_id used for store lookups and previous_response_id
    chaining. ``output`` is the Responses output item list. ``raw`` carries the
    full provider response body so callers can serialize it verbatim.
    """

    id: str
    model: str
    output: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    usage: dict[str, Any] | None = None
    previous_response_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SSEEvent:
    """A single Server-Sent Event in a Responses stream.

    ``event`` is the SSE event name (for example ``response.output_text.delta``)
    and ``data`` is the already-decoded JSON payload for that event. ``raw`` may
    hold the exact wire bytes when an adapter forwards upstream SSE verbatim.
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: bytes | None = None


@dataclass
class ModelList:
    """An OpenAI-style ``/v1/models`` listing.

    ``data`` is the canonical OpenAI-shaped model list. ``models`` mirrors the
    Codex-private refresh field (see codex_models_compat) and defaults empty.
    """

    data: list[dict[str, Any]] = field(default_factory=list)
    object: str = "list"
    models: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class InputItemList:
    """The input items recorded for a prior response (``/input_items``)."""

    response_id: str
    data: list[dict[str, Any]] = field(default_factory=list)
    object: str = "list"


@runtime_checkable
class ProviderAdapter(Protocol):
    """The stable provider boundary (ADR 0002 11.3).

    Implementations are injected into the first-party app by prefix
    (``claude``, ``copilot``). All methods are provider-agnostic; the app does
    not depend on any provider internals beyond this surface.
    """

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        """Return a non-streaming Responses object for ``request``."""
        ...

    def stream_response(self, request: ResponsesRequest) -> AsyncIterator[SSEEvent]:
        """Yield Responses SSE events for ``request`` (stream=True)."""
        ...

    async def list_models(self) -> ModelList:
        """Return the provider model listing for ``/v1/models``."""
        ...

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        """Return a previously created response by id."""
        ...

    async def list_input_items(self, response_id: str) -> InputItemList:
        """Return the input items recorded for a prior response id."""
        ...
