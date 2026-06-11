"""Per-provider Responses feature gate and structured unsupported_feature builder.

The Responses surface that callers may exercise is heterogeneous across the four
first-party providers (claude, copilot, auggie, deepseek). This module holds the
single enforcement seam ADR 0002 D4 requires: a generated capability table
sourced from .omc/research/responses-parity-surface.json (mirrored under
src/reverso/protocols/data so it ships with the wheel and survives outside the
repo root), a request-side feature extractor, and one shared error body builder.

The gate is HYBRID: responses_app.py applies the fast path BEFORE adapter
dispatch by calling check_features over extract_features; adapters MAY also
raise UnsupportedFeature from inside create_response/stream_response when they
hit a table omission, and responses_app catches it and renders the IDENTICAL
400 body via build_unsupported_payload. Capability tables are the source of
truth; UnsupportedFeature prevents silent semantic drift if a feature is added
to a request shape that the tables have not yet enumerated.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Iterable

from reverso.protocols.adapter import ResponsesRequest

__all__ = [
    "UnsupportedFeature",
    "CAPABILITY_TABLES",
    "FEATURES",
    "PROVIDERS",
    "extract_features",
    "check_features",
    "build_unsupported_payload",
]

UNSUPPORTED = "unsupported"
NATIVE = "native"
TRANSLATED = "translated"
PARTIAL = "partial"
_ALLOWED_CLASSIFICATIONS = frozenset({NATIVE, TRANSLATED, PARTIAL, UNSUPPORTED})

_PARITY_PACKAGE = "reverso.protocols.data"
_PARITY_RESOURCE = "responses_parity_surface.json"


class UnsupportedFeature(Exception):
    """Raised by adapters (back-stop) or the gate (fast path) on capability gaps.

    Carries the provider prefix and the feature key from the capability table so
    the shared 400 builder can name both in the error body without the caller
    having to reconstruct them.
    """

    __slots__ = ("provider", "feature")

    def __init__(self, provider: str, feature: str) -> None:
        super().__init__(f"{provider} does not support {feature}")
        self.provider = provider
        self.feature = feature


def _load_parity_surface() -> dict[str, Any]:
    raw = (
        resources.files(_PARITY_PACKAGE)
        .joinpath(_PARITY_RESOURCE)
        .read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("responses_parity_surface.json must be a JSON object")
    return payload


def _build_capability_tables(
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, str]], tuple[str, ...], tuple[str, ...]]:
    providers_raw = payload.get("providers")
    features_raw = payload.get("features")
    if not isinstance(providers_raw, list) or not providers_raw:
        raise RuntimeError("parity surface: 'providers' must be a non-empty list")
    if not isinstance(features_raw, dict) or not features_raw:
        raise RuntimeError("parity surface: 'features' must be a non-empty object")

    providers = tuple(str(p) for p in providers_raw)
    tables: dict[str, dict[str, str]] = {provider: {} for provider in providers}
    features: list[str] = []
    for feature, per_provider in features_raw.items():
        if not isinstance(per_provider, dict):
            raise RuntimeError(
                f"parity surface: feature {feature!r} must map provider to classification"
            )
        features.append(str(feature))
        for provider in providers:
            value = per_provider.get(provider)
            if value not in _ALLOWED_CLASSIFICATIONS:
                raise RuntimeError(
                    f"parity surface: feature {feature!r} provider {provider!r} "
                    f"classification {value!r} not in {sorted(_ALLOWED_CLASSIFICATIONS)}"
                )
            tables[provider][str(feature)] = str(value)
    return tables, tuple(features), providers


_PARITY_PAYLOAD = _load_parity_surface()
CAPABILITY_TABLES, FEATURES, PROVIDERS = _build_capability_tables(_PARITY_PAYLOAD)


def build_unsupported_payload(provider: str, feature: str) -> dict[str, Any]:
    """Return the canonical 400 body for an unsupported feature.

    Fast path (responses_app pre-dispatch) and back-stop (UnsupportedFeature
    raised by an adapter) MUST render the same body via this builder so the
    error shape never diverges between the two paths.
    """
    return {
        "error": {
            "type": "invalid_request_error",
            "code": "unsupported_feature",
            "message": f"{provider} does not support {feature}",
        }
    }


def check_features(provider: str, features: Iterable[str]) -> None:
    """Raise UnsupportedFeature for the first feature classified as unsupported.

    Provider lookup falls through to an empty dict if the prefix is unknown so a
    new prefix wired through build_app without a table entry behaves like 'no
    classifications declared' rather than crashing the gateway. The fast path
    iterates request features only, so unfamiliar features (not in the table)
    are silently allowed at the gate and rely on the adapter back-stop. Sorted
    iteration keeps the error name stable across runs when a request triggers
    several unsupported features at once (sets have no insertion order).
    """
    table = CAPABILITY_TABLES.get(provider, {})
    for feature in sorted(features):
        if table.get(feature) == UNSUPPORTED:
            raise UnsupportedFeature(provider=provider, feature=feature)


def _is_text_only_message_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    saw_message = False
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in (None, "message"):
            saw_message = True
    return saw_message


def _contains_content_type(value: Any, target: str) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == target:
                return True
    return False


def _tool_features(tools: Any) -> set[str]:
    if not isinstance(tools, list):
        return set()
    result: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            result.add("tools.function")
        elif tool_type == "web_search":
            result.add("tools.web_search")
        elif tool_type == "file_search":
            result.add("tools.file_search")
        elif tool_type == "computer_use":
            result.add("tools.computer_use")
        elif tool_type == "code_interpreter":
            result.add("tools.code_interpreter")
    return result


def _tool_choice_feature(tool_choice: Any) -> str | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "tool_choice.auto"
        if tool_choice == "required":
            return "tool_choice.required"
        if tool_choice == "none":
            return "tool_choice.none"
        return None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            return "tool_choice.named"
    return None


def _text_format_feature(text_extra: Any) -> str | None:
    if not isinstance(text_extra, dict):
        return None
    fmt = text_extra.get("format")
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "text":
        return "text.format.text"
    if fmt_type == "json_schema":
        return "text.format.json_schema"
    if fmt_type == "json_object":
        return "text.format.json_object"
    return None


def extract_features(request: ResponsesRequest) -> set[str]:
    """Return the set of capability-table features this request exercises.

    Only features declared in .omc/research/responses-parity-surface.json are
    emitted. ResponsesRequest.from_payload preserves Codex-only fields in extra,
    so this extractor sees fields BEFORE normalize_codex_responses_payload would
    drop them (the gate must reject e.g. parallel_tool_calls for claude even
    though the Codex normalizer would otherwise silently strip it downstream).
    """
    features: set[str] = set()

    if isinstance(request.input, str):
        if request.input:
            features.add("input.string")
    elif isinstance(request.input, list):
        if _is_text_only_message_list(request.input):
            features.add("input.message_list_text")
        if _contains_content_type(request.input, "input_image"):
            features.add("input.image")
        if _contains_content_type(request.input, "input_file"):
            features.add("input.file")

    if request.instructions:
        features.add("instructions")
    if request.previous_response_id:
        features.add("previous_response_id")
    if request.stream:
        features.add("stream")

    features.update(_tool_features(request.tools))
    tool_choice_feature = _tool_choice_feature(request.tool_choice)
    if tool_choice_feature is not None:
        features.add(tool_choice_feature)

    extra = request.extra or {}

    if "store" in extra:
        features.add("store")
    if extra.get("parallel_tool_calls") is not None:
        features.add("parallel_tool_calls")

    reasoning = extra.get("reasoning")
    if isinstance(reasoning, dict):
        if reasoning.get("effort") is not None:
            features.add("reasoning.effort")
        if reasoning.get("summary") is not None:
            features.add("reasoning.summary")

    if extra.get("temperature") is not None:
        features.add("sampling.temperature")
    if extra.get("top_p") is not None:
        features.add("sampling.top_p")
    if extra.get("max_output_tokens") is not None:
        features.add("max_output_tokens")
    if extra.get("truncation") is not None:
        features.add("truncation")
    if extra.get("metadata") is not None:
        features.add("metadata")
    include_value = extra.get("include")
    if isinstance(include_value, list) and include_value:
        features.add("include")
    if extra.get("background") is not None:
        features.add("background")
    if extra.get("service_tier") is not None:
        features.add("service_tier")
    if extra.get("user") is not None:
        features.add("user")
    if extra.get("safety_identifier") is not None:
        features.add("safety_identifier")

    text_format = _text_format_feature(extra.get("text"))
    if text_format is not None:
        features.add(text_format)

    return features
