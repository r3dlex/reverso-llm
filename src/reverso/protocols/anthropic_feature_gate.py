"""Per-backend capability gating for the inbound Anthropic Messages surface (G005).

ADR 0006 fixes a per-(feature x backend) capability ceiling. This module is the
single data-driven seam that enforces it for the Anthropic surface: it extracts
the capability-relevant features from an inbound Anthropic Messages payload, maps
each to a row in the shared feature_policy capability table (sourced from
responses_parity_surface.json), and rejects any feature the resolved backend
classifies as ``unsupported`` with a hard Anthropic ``invalid_request_error``
(HTTP 400). It adds no scattered conditionals: the allow/reject decision is the
table classification, so the only Anthropic-specific logic is the payload-shape
feature extraction.

Mapping from Anthropic request shapes to capability-table feature keys:
  - image content block (top-level message content OR nested inside a tool_result
    inner content list) -> ``input.image`` (native on copilot, unsupported on
    deepseek/auggie).
  - extended thinking: the ``thinking`` request param OR a thinking /
    redacted_thinking content block -> ``thinking`` (unsupported on all backends,
    structurally-impossible-M1).
  - ``cache_control`` carried on ANY message content block, system block, tool
    definition (``tools[].cache_control``), or nested tool_result inner content
    block -> ``caching.cache_control``. Unsupported on all backends, but it is a
    transparent caching OPTIMIZATION, so the surface DEGRADES it (strips it via
    ``strip_degradable_features`` before gating) instead of hard-rejecting, keeping
    clients that always send it (e.g. Claude Code) usable. It therefore never
    reaches the reject path below in normal flow.
  - ``tools`` / ``tool_use`` -> ``tools.function``. auggie classifies this as
    ``partial`` (text-only ceiling), NOT ``unsupported``, so it is ACCEPTED and
    degrades to text; copilot/deepseek accept it natively/translated. No backend
    hard-rejects tools, so it never raises here.

A backend classified ``partial`` for a requested feature is accepted (the partial
subset is served and the rest degrades through the translation/adapter layer);
only an ``unsupported`` classification is a hard reject, matching
feature_policy.check_features.
"""

from __future__ import annotations

from typing import Any

from reverso.protocols.feature_policy import CAPABILITY_TABLES, UNSUPPORTED

__all__ = [
    "AnthropicFeatureRejected",
    "FEATURE_IMAGE",
    "FEATURE_THINKING",
    "FEATURE_CACHE_CONTROL",
    "FEATURE_TOOLS",
    "extract_anthropic_features",
    "gate_anthropic_features",
    "strip_degradable_features",
]

# Capability-table feature keys this gate maps onto. These MUST exist as rows in
# responses_parity_surface.json so the allow/reject decision stays data-driven.
FEATURE_IMAGE = "input.image"
FEATURE_THINKING = "thinking"
FEATURE_CACHE_CONTROL = "caching.cache_control"
FEATURE_TOOLS = "tools.function"


class AnthropicFeatureRejected(Exception):
    """Raised when a requested Anthropic feature is unsupported on the backend.

    Carries the capability-table feature key and the resolved backend so the
    caller can render a secret-free Anthropic ``invalid_request_error`` naming
    both. The exception message itself is secret-free (feature key + backend
    name only).
    """

    __slots__ = ("feature", "backend")

    def __init__(self, feature: str, backend: str) -> None:
        super().__init__(f"feature {feature} is not supported on the {backend} backend")
        self.feature = feature
        self.backend = backend


# Maximum nesting depth for tool_result inner content scans. Anthropic does not
# legitimately nest tool_result inside tool_result; this cap prevents a crafted
# payload from driving unbounded recursion into a RecursionError (cheap loopback
# DoS). Depth 8 covers any real multi-level content list with large headroom.
_MAX_BLOCK_DEPTH = 8


def _has_cache_control(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("cache_control") is not None


def _scan_block_list(blocks: Any, found: set[str], *, depth: int = 0) -> None:
    """Scan a list of Anthropic content blocks, accumulating feature keys.

    Detects image blocks, thinking / redacted_thinking blocks, cache_control on
    any block, and recurses into a tool_result block's inner ``content`` list so
    nested image and nested cache_control are gated consistently with top-level.

    ``depth`` is the current recursion depth; scanning stops silently at
    ``_MAX_BLOCK_DEPTH`` so a malformed deeply-nested payload cannot exhaust
    the Python call stack.
    """
    if depth > _MAX_BLOCK_DEPTH:
        return
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if _has_cache_control(block):
            found.add(FEATURE_CACHE_CONTROL)
        block_type = block.get("type")
        if block_type == "image":
            found.add(FEATURE_IMAGE)
        elif block_type in ("thinking", "redacted_thinking"):
            found.add(FEATURE_THINKING)
        elif block_type == "tool_result":
            # tool_result inner content may itself carry image / cache_control
            # blocks; scan it so nested features are gated like top-level ones.
            _scan_block_list(block.get("content"), found, depth=depth + 1)


def _scan_messages(messages: Any, found: set[str]) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            _scan_block_list(content, found)


def _scan_system(system: Any, found: set[str]) -> None:
    """Scan an Anthropic ``system`` field (string or block list) for cache_control."""
    if isinstance(system, list):
        for block in system:
            if _has_cache_control(block):
                found.add(FEATURE_CACHE_CONTROL)


def _scan_tools(tools: Any, found: set[str]) -> None:
    """Scan tool definitions for the tools feature and tool-definition cache_control."""
    if not isinstance(tools, list):
        return
    has_tool = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        has_tool = True
        if _has_cache_control(tool):
            found.add(FEATURE_CACHE_CONTROL)
    if has_tool:
        # Extracted for symmetry and future rows; no M1 backend classifies
        # tools.function as unsupported, so this never triggers the gate today.
        found.add(FEATURE_TOOLS)


def extract_anthropic_features(payload: dict[str, Any]) -> set[str]:
    """Return the capability-table feature keys an Anthropic payload exercises.

    The returned keys are exactly the rows the gate looks up in the capability
    table. Detection covers image and thinking content blocks, the ``thinking``
    request param, tool definitions, and ``cache_control`` on message content
    blocks, system blocks, tool definitions, and nested tool_result inner blocks.
    """
    found: set[str] = set()
    if not isinstance(payload, dict):
        return found

    _scan_messages(payload.get("messages"), found)
    _scan_system(payload.get("system"), found)
    _scan_tools(payload.get("tools"), found)

    # The extended-thinking request param is gated even without thinking blocks.
    if payload.get("thinking") is not None:
        found.add(FEATURE_THINKING)

    return found


def _strip_cache_control_blocks(blocks: Any, *, depth: int = 0) -> None:
    """Remove ``cache_control`` from each block in a content list, recursively.

    Mirrors _scan_block_list's traversal (incl. nested tool_result content) but
    deletes the key instead of recording it. Depth-capped like the scanner so a
    crafted deep payload cannot exhaust the stack.
    """
    if depth > _MAX_BLOCK_DEPTH:
        return
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block.pop("cache_control", None)
        if block.get("type") == "tool_result":
            _strip_cache_control_blocks(block.get("content"), depth=depth + 1)


def strip_degradable_features(payload: dict[str, Any]) -> None:
    """Strip transparently-degradable features from an Anthropic payload IN PLACE.

    Currently strips ``cache_control`` everywhere it can appear (message content
    blocks including nested tool_result, ``system`` blocks, and tool definitions).
    cache_control is a prompt-caching OPTIMIZATION: dropping it changes no response
    semantics, only whether the provider caches. No backend on this surface can
    honor it, so rather than hard-rejecting (a 400 on every request), the surface
    silently DEGRADES by stripping it. Clients such as Claude Code attach
    cache_control to essentially every request, so degrading is what keeps them
    usable instead of failing each call. Semantic features (thinking, image) change
    the response and are NOT degraded here; they remain hard-gated by
    gate_anthropic_features. Call this BEFORE gating and translation so the stripped
    payload is what both the gate and the downstream adapter observe.
    """
    if not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                _strip_cache_control_blocks(message.get("content"))
    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                block.pop("cache_control", None)
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                tool.pop("cache_control", None)


def gate_anthropic_features(payload: dict[str, Any], backend: str) -> None:
    """Reject the first feature the backend classifies as unsupported.

    Looks each extracted feature up in the shared capability table for the
    resolved backend; a feature classified ``unsupported`` raises
    AnthropicFeatureRejected. ``partial`` (e.g. auggie tools.function) is accepted
    and degrades downstream, matching feature_policy.check_features. Sorted
    iteration keeps the rejected-feature name stable when several are present.
    """
    table = CAPABILITY_TABLES.get(backend, {})
    for feature in sorted(extract_anthropic_features(payload)):
        if table.get(feature) == UNSUPPORTED:
            raise AnthropicFeatureRejected(feature=feature, backend=backend)
