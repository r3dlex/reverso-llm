"""In-memory response/session store for Codex previous_response_id chaining.

This milestone is in-memory only (ADR 0002 11.3). The store keeps response
envelopes and their recorded input items keyed by response_id so adapters can
honor Codex-observed ``previous_response_id`` lookups and ``/input_items``
queries. A later milestone may swap the backing implementation behind this same
surface.
"""

from __future__ import annotations

import threading

from reverso.protocols.adapter import InputItemList, ResponseEnvelope


class ResponseStore:
    """Thread-safe in-memory store for response envelopes and input items."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: dict[str, ResponseEnvelope] = {}
        self._input_items: dict[str, list[dict]] = {}

    def put_response(
        self,
        envelope: ResponseEnvelope,
        input_items: list[dict] | None = None,
    ) -> None:
        """Store a response envelope and the input items that produced it."""
        with self._lock:
            self._responses[envelope.id] = envelope
            if input_items is not None:
                self._input_items[envelope.id] = list(input_items)

    def get_response(self, response_id: str) -> ResponseEnvelope | None:
        """Return a stored response envelope, or None if unknown."""
        with self._lock:
            return self._responses.get(response_id)

    def get_input_items(self, response_id: str) -> InputItemList | None:
        """Return recorded input items for a response id, or None if unknown."""
        with self._lock:
            if response_id not in self._responses:
                return None
            items = list(self._input_items.get(response_id, []))
        return InputItemList(response_id=response_id, data=items)

    def clear(self) -> None:
        """Drop all stored state (used by tests)."""
        with self._lock:
            self._responses.clear()
            self._input_items.clear()
