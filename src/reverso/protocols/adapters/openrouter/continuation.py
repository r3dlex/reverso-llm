"""Typed stateless continuation chain for OpenRouter Responses (OR-G3, U7, I3).

OpenRouter Responses is stateless. Reverso owns the full typed chain in
local storage and materializes complete input and output items before
issuing the next upstream request. Unknown, incomplete, malformed, or
text-only chains fail locally; the upstream request never carries
``previous_response_id`` or ``store``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ContinuationRejection",
    "ReplayChain",
    "ReplayItem",
    "build_continuation_request",
    "materialize_continuation",
]


class ContinuationRejection(ValueError):
    """A continuation chain is unknown, partial, or lossy."""


@dataclass(frozen=True)
class ReplayItem:
    """A single typed item in a Responses continuation chain."""

    role: str  # user | assistant | tool | system | developer
    content: str = ""
    reasoning: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    tool_output: Any = None
    response_id: str | None = None

    def to_input_item(self) -> dict[str, Any]:
        """Render this item as a Responses input shape."""
        if self.role == "tool":
            return {
                "type": "function_call_output",
                "call_id": self.tool_call_id or "",
                "output": self.tool_output,
            }
        if self.role in {"assistant", "user", "system", "developer"}:
            item: dict[str, Any] = {
                "type": "message",
                "role": self.role,
                "content": [
                    {
                        "type": "output_text"
                        if self.role == "assistant"
                        else "input_text",
                        "text": self.content,
                    }
                ],
            }
            return item
        raise ContinuationRejection(f"unknown role {self.role!r}")


@dataclass(frozen=True)
class ReplayChain:
    """The complete typed chain for one Responses conversation."""

    response_id: str
    items: tuple[ReplayItem, ...] = field(default_factory=tuple)

    @property
    def has_tool_calls(self) -> bool:
        return any(item.role == "tool" for item in self.items)


def materialize_continuation(chain: ReplayChain | None, *, store: Any) -> ReplayChain:
    """Resolve a chain from local storage and reject partial or unknown ones.

    ``store`` provides ``get_response(response_id)``; the chain is rejected when
    the store has no record of ``response_id``.
    """
    if chain is None:
        raise ContinuationRejection("chain is unknown")
    if not chain.items:
        raise ContinuationRejection("chain is empty")
    roles = [item.role for item in chain.items]
    if "assistant" not in roles:
        raise ContinuationRejection("chain has no assistant reply")
    if any(
        role not in {"user", "assistant", "tool", "system", "developer"}
        for role in roles
    ):
        raise ContinuationRejection("chain contains an unknown role")
    return chain


def build_continuation_request(chain: ReplayChain, *, model: str) -> dict[str, Any]:
    """Materialize a typed request body from the chain.

    The body never carries ``previous_response_id`` or ``store`` because
    OpenRouter Responses is stateless; upstream receives the full typed chain.
    """
    return {
        "model": model,
        "input": [item.to_input_item() for item in chain.items],
        "stream": False,
    }
