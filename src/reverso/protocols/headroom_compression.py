"""Headroom compression seam for adapter-bound Responses requests.

The seam is intentionally provider-agnostic and keeps the frozen
ProviderAdapter contract unchanged. It projects text-bearing request fields into
Headroom's message-list shape, runs compression off the event loop, then
reconstructs a structurally equivalent ResponsesRequest. Any unsafe condition
returns the original request.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from reverso.protocols.adapter import ResponsesRequest

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_DEFAULT_PROFILE = "coding"
_DEFAULT_TIMEOUT_SECONDS = 2.0
_DEFAULT_MODEL_LIMIT = 200000
_DEFAULT_PROFILE_MIN_TOKENS_TO_COMPRESS = 10
_OUTCOME_KEYS = ("compressed", "passed_through", "fail_open", "other")
_FAILURE_REASON_KEYS = (
    "worker_busy",
    "timeout",
    "exception",
    "inflation_guard",
    "retrieval_marker",
    "unsafe_output",
    "other",
)
_ERROR_TYPE_KEYS = (
    "timeout",
    "worker_busy",
    "dependency_exception",
    "inflation_guard",
    "retrieval_marker",
    "unsafe_output",
    "other",
)
_PROVIDER_KEYS = (
    "claude",
    "copilot",
    "auggie",
    "deepseek",
    "kimi",
    "codex-direct",
    "openai-pass-through",
    "other",
)
_SURFACE_KEYS = ("responses", "anthropic_messages", "other")
_PASS_THROUGH_REASONS = {
    "disabled",
    "no_text",
    "below_min_tokens",
    "unchanged",
    "pass_through",
}

CompressCallable = Callable[..., Any]

logger = logging.getLogger(__name__)

_HEADROOM_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="reverso-headroom",
)
_HEADROOM_WORKER_LOCK = threading.Lock()
_HEADROOM_ACTIVE_WORKERS = 0


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _zero_counts(keys: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


def normalize_headroom_provider(value: str) -> str:
    """Map a dispatch provider to the bounded Headroom provider dimension."""
    aliases = {
        "codex": "codex-direct",
        "openai": "openai-pass-through",
    }
    normalized = aliases.get(value, value)
    return normalized if normalized in _PROVIDER_KEYS else "other"


def _normalize_headroom_surface(value: str) -> str:
    return value if value in _SURFACE_KEYS else "other"


def _normalize_failure_reason(value: str) -> str:
    return value if value in _FAILURE_REASON_KEYS else "other"


def _normalize_error_type(value: str, reason: str) -> str:
    known = {
        "TimeoutError": "timeout",
        "WorkerBusy": "worker_busy",
        "InflationGuard": "inflation_guard",
        "RetrievalMarker": "retrieval_marker",
        "UnsafeOutput": "unsafe_output",
    }
    if value in _ERROR_TYPE_KEYS:
        return value
    if value in known:
        return known[value]
    if reason == "exception":
        return "dependency_exception"
    return "other"


def _clamp_ratio(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _try_reserve_headroom_worker() -> bool:
    global _HEADROOM_ACTIVE_WORKERS
    with _HEADROOM_WORKER_LOCK:
        if _HEADROOM_ACTIVE_WORKERS >= 1:
            return False
        _HEADROOM_ACTIVE_WORKERS += 1
        return True


def _release_headroom_worker() -> None:
    global _HEADROOM_ACTIVE_WORKERS
    with _HEADROOM_WORKER_LOCK:
        _HEADROOM_ACTIVE_WORKERS = max(_HEADROOM_ACTIVE_WORKERS - 1, 0)


@dataclass(frozen=True)
class HeadroomCompressionConfig:
    """Runtime controls for the Headroom seam."""

    enabled: bool = True
    profile: str = _DEFAULT_PROFILE
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    model_limit: int = _DEFAULT_MODEL_LIMIT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HeadroomCompressionConfig:
        """Build config from environment with compression enabled by default."""
        source = os.environ if env is None else env
        enabled = source.get("REVERSO_HEADROOM_ENABLED", "1").strip().lower()
        profile = source.get("REVERSO_HEADROOM_PROFILE", _DEFAULT_PROFILE).strip()
        timeout_raw = source.get(
            "REVERSO_HEADROOM_TIMEOUT", str(_DEFAULT_TIMEOUT_SECONDS)
        )
        model_limit_raw = source.get(
            "REVERSO_HEADROOM_MODEL_LIMIT", str(_DEFAULT_MODEL_LIMIT)
        )
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError:
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        try:
            model_limit = int(model_limit_raw)
        except ValueError:
            model_limit = _DEFAULT_MODEL_LIMIT
        return cls(
            enabled=enabled not in _FALSE_VALUES,
            profile=profile or _DEFAULT_PROFILE,
            timeout_seconds=max(timeout_seconds, 0.001),
            model_limit=max(model_limit, 1),
        )


def configure_headroom_environment(env: dict[str, str] | None = None) -> None:
    """Enforce Headroom defaults that preserve Reverso's no-persistence posture."""
    target = os.environ if env is None else env
    target["HEADROOM_STATELESS"] = "true"
    target["HEADROOM_TELEMETRY"] = "off"
    target["HEADROOM_TELEMETRY_DISABLED"] = "1"
    target["HEADROOM_UPDATE_CHECK"] = "off"
    target["HEADROOM_PERIODIC_TOIN_STATS"] = "0"
    target["HEADROOM_CCR_BACKEND"] = "memory"
    target["HEADROOM_MEMORY_ENABLED"] = "0"


@dataclass(frozen=True)
class HeadroomCompressionOutcome:
    """Result of attempting compression for one request."""

    request: ResponsesRequest
    compressed: bool = False
    fail_open: bool = False
    reason: str = "pass_through"
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    error_type: str | None = None


@dataclass
class HeadroomUsageMetrics:
    """In-memory aggregate Headroom savings counters."""

    process_started_at: str = field(init=False)
    measurement_started_at: str = field(init=False)
    requests_seen: int = 0
    requests_compressed: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    fail_open_count: int = 0
    outcome_counts: dict[str, int] = field(
        default_factory=lambda: _zero_counts(_OUTCOME_KEYS)
    )
    failure_reasons: dict[str, int] = field(
        default_factory=lambda: _zero_counts(_FAILURE_REASON_KEYS)
    )
    error_types: dict[str, int] = field(
        default_factory=lambda: _zero_counts(_ERROR_TYPE_KEYS)
    )
    provider_counts: dict[str, int] = field(
        default_factory=lambda: _zero_counts(_PROVIDER_KEYS)
    )
    surface_counts: dict[str, int] = field(
        default_factory=lambda: _zero_counts(_SURFACE_KEYS)
    )
    updated_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    reset_reason: str = "process_start"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        started_at = _timestamp()
        self.process_started_at = started_at
        self.measurement_started_at = started_at

    def record(
        self,
        outcome: HeadroomCompressionOutcome,
        *,
        provider: str = "other",
        surface: str = "other",
    ) -> None:
        """Record one compression attempt without storing prompt content."""
        with self._lock:
            timestamp = _timestamp()
            self.requests_seen += 1
            provider_key = normalize_headroom_provider(provider)
            surface_key = _normalize_headroom_surface(surface)
            self.provider_counts[provider_key] = (
                self.provider_counts.get(provider_key, 0) + 1
            )
            self.surface_counts[surface_key] = (
                self.surface_counts.get(surface_key, 0) + 1
            )
            if outcome.compressed:
                self.requests_compressed += 1
                self.outcome_counts["compressed"] = (
                    self.outcome_counts.get("compressed", 0) + 1
                )
                self.last_success_at = timestamp
            elif outcome.fail_open:
                self.outcome_counts["fail_open"] = (
                    self.outcome_counts.get("fail_open", 0) + 1
                )
            elif outcome.reason in _PASS_THROUGH_REASONS:
                self.outcome_counts["passed_through"] = (
                    self.outcome_counts.get("passed_through", 0) + 1
                )
            else:
                self.outcome_counts["other"] = self.outcome_counts.get("other", 0) + 1
            if outcome.fail_open:
                self.fail_open_count += 1
                reason_key = _normalize_failure_reason(outcome.reason)
                self.failure_reasons[reason_key] = (
                    self.failure_reasons.get(reason_key, 0) + 1
                )
                self.last_failure_at = timestamp
            if outcome.error_type:
                error_key = _normalize_error_type(
                    outcome.error_type,
                    outcome.reason,
                )
                self.error_types[error_key] = self.error_types.get(error_key, 0) + 1
            self.tokens_before += max(outcome.tokens_before, 0)
            self.tokens_after += max(outcome.tokens_after, 0)
            self.tokens_saved += max(outcome.tokens_saved, 0)
            self.updated_at = timestamp

    def snapshot(
        self, config: HeadroomCompressionConfig | None = None
    ) -> dict[str, Any]:
        """Return prompt-free aggregate metrics."""
        with self._lock:
            requests_seen = max(self.requests_seen, 0)
            requests_compressed = max(self.requests_compressed, 0)
            fail_open_count = max(self.fail_open_count, 0)
            tokens_before = max(self.tokens_before, 0)
            tokens_after = max(self.tokens_after, 0)
            tokens_saved = max(self.tokens_saved, 0)
            compression_ratio = _clamp_ratio(
                tokens_saved / tokens_before if tokens_before else 0.0
            )
            compression_success_rate = _clamp_ratio(
                requests_compressed / requests_seen if requests_seen else 0.0
            )
            average_tokens_saved = (
                tokens_saved / requests_compressed if requests_compressed else 0.0
            )
            return {
                "schema_version": 2,
                "enabled": True if config is None else config.enabled,
                "profile": _DEFAULT_PROFILE if config is None else config.profile,
                "requests_seen": requests_seen,
                "requests_compressed": requests_compressed,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "tokens_saved": tokens_saved,
                "compression_ratio": compression_ratio,
                "fail_open_count": fail_open_count,
                "failure_reasons": {
                    key: max(self.failure_reasons.get(key, 0), 0)
                    for key in _FAILURE_REASON_KEYS
                },
                "error_types": {
                    key: max(self.error_types.get(key, 0), 0)
                    for key in _ERROR_TYPE_KEYS
                },
                "updated_at": self.updated_at,
                "process_started_at": self.process_started_at,
                "measurement_started_at": self.measurement_started_at,
                "requests_passed_through": max(
                    requests_seen - requests_compressed - fail_open_count,
                    0,
                ),
                "compression_success_rate": compression_success_rate,
                "average_tokens_saved": average_tokens_saved,
                "outcome_counts": {
                    key: max(self.outcome_counts.get(key, 0), 0)
                    for key in _OUTCOME_KEYS
                },
                "provider_counts": {
                    key: max(self.provider_counts.get(key, 0), 0)
                    for key in _PROVIDER_KEYS
                },
                "surface_counts": {
                    key: max(self.surface_counts.get(key, 0), 0)
                    for key in _SURFACE_KEYS
                },
                "timeout_seconds": (
                    _DEFAULT_TIMEOUT_SECONDS
                    if config is None
                    else config.timeout_seconds
                ),
                "model_limit": (
                    _DEFAULT_MODEL_LIMIT if config is None else config.model_limit
                ),
                "last_success_at": self.last_success_at,
                "last_failure_at": self.last_failure_at,
                "reset_reason": self.reset_reason,
            }

    def reset(self) -> None:
        """Reset process-local metrics, used by tests."""
        with self._lock:
            self.measurement_started_at = _timestamp()
            self.requests_seen = 0
            self.requests_compressed = 0
            self.tokens_before = 0
            self.tokens_after = 0
            self.tokens_saved = 0
            self.fail_open_count = 0
            self.outcome_counts = _zero_counts(_OUTCOME_KEYS)
            self.failure_reasons = _zero_counts(_FAILURE_REASON_KEYS)
            self.error_types = _zero_counts(_ERROR_TYPE_KEYS)
            self.provider_counts = _zero_counts(_PROVIDER_KEYS)
            self.surface_counts = _zero_counts(_SURFACE_KEYS)
            self.updated_at = None
            self.last_success_at = None
            self.last_failure_at = None
            self.reset_reason = "manual_test_reset"


DEFAULT_HEADROOM_METRICS = HeadroomUsageMetrics()


@dataclass(frozen=True)
class _TextTarget:
    path: tuple[Any, ...]
    role: str


@dataclass(frozen=True)
class _Projection:
    request: ResponsesRequest
    messages: list[dict[str, Any]]
    targets: list[_TextTarget]


def _request_copy(request: ResponsesRequest) -> ResponsesRequest:
    return replace(
        request,
        input=copy.deepcopy(request.input),
        tools=copy.deepcopy(request.tools),
        tool_choice=copy.deepcopy(request.tool_choice),
        extra=copy.deepcopy(request.extra),
    )


def _part_text_key(part: dict[str, Any]) -> str | None:
    part_type = part.get("type")
    if part_type in {"input_text", "output_text", "text"} and isinstance(
        part.get("text"), str
    ):
        return "text"
    return None


def _collect_projection(request: ResponsesRequest) -> _Projection:
    copied = _request_copy(request)
    messages: list[dict[str, Any]] = []
    targets: list[_TextTarget] = []

    def add(path: tuple[Any, ...], role: str, text: str) -> None:
        if text.strip():
            messages.append({"role": role, "content": text})
            targets.append(_TextTarget(path=path, role=role))

    if isinstance(copied.instructions, str):
        add(("instructions",), "system", copied.instructions)

    if isinstance(copied.input, str):
        add(("input",), "user", copied.input)
        return _Projection(request=copied, messages=messages, targets=targets)

    if not isinstance(copied.input, list):
        return _Projection(request=copied, messages=messages, targets=targets)

    for item_index, item in enumerate(copied.input):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role_value = item.get("role")
        role = role_value if isinstance(role_value, str) else "user"
        if item_type == "message":
            content = item.get("content")
            if isinstance(content, str):
                add(("input", item_index, "content"), role, content)
            elif isinstance(content, list):
                for part_index, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    key = _part_text_key(part)
                    if key is not None:
                        add(
                            ("input", item_index, "content", part_index, key),
                            role,
                            part[key],
                        )
        elif item_type == "function_call_output" and isinstance(
            item.get("output"), str
        ):
            add(("input", item_index, "output"), "tool", item["output"])
    return _Projection(request=copied, messages=messages, targets=targets)


def _set_path(request: ResponsesRequest, path: tuple[Any, ...], value: str) -> None:
    if path == ("instructions",):
        request.instructions = value
        return
    if path == ("input",):
        request.input = value
        return
    cursor: Any = request
    for part in path[:-1]:
        cursor = (
            getattr(cursor, part)
            if isinstance(part, str) and hasattr(cursor, part)
            else cursor[part]
        )
    cursor[path[-1]] = value


def _extract_message_content(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        return "\n".join(texts) if texts else None
    return None


_RETRIEVAL_MARKER_RE = re.compile(
    r"(?:Retrieve (?:more|original): hash=|headroom_retrieve|<<ccr:|\[\d+[^\]]*compressed[^\]]*hash=)",
    re.IGNORECASE,
)


def _contains_retrieval_marker(value: Any) -> bool:
    """Return True when compressed output needs an unavailable retrieve tool."""
    if isinstance(value, str):
        return bool(_RETRIEVAL_MARKER_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_retrieval_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_retrieval_marker(item) for item in value)
    return False


def _reconstruct_request(
    projection: _Projection,
    compressed_messages: Any,
) -> ResponsesRequest | None:
    if not isinstance(compressed_messages, list):
        return None
    if len(compressed_messages) != len(projection.targets):
        return None
    request = _request_copy(projection.request)
    for target, message in zip(projection.targets, compressed_messages, strict=True):
        if not isinstance(message, dict):
            return None
        if message.get("role") != target.role:
            return None
        text = _extract_message_content(message)
        if text is None:
            return None
        _set_path(request, target.path, text)
    return request


def _import_headroom_compress() -> CompressCallable:
    from headroom import compress

    return compress


def _read_result_int(result: Any, name: str) -> int:
    value = getattr(result, name, 0)
    return value if isinstance(value, int) else 0


def _read_result_float(result: Any, name: str) -> float:
    value = getattr(result, name, 0.0)
    return value if isinstance(value, int | float) else 0.0


def _may_reach_headroom_token_floor(
    messages: list[dict[str, Any]],
    profile: str,
) -> bool:
    """Return whether any message may meet Headroom's compression threshold.

    The pinned default profile uses a 10-token floor. A tokenizer cannot
    produce more tokens than the UTF-8 bytes in non-whitespace content, so this
    upper bound can safely bypass the expensive cold import for tiny default
    requests. Custom profiles may use a lower floor and therefore always import.
    """
    if profile != _DEFAULT_PROFILE:
        return True
    return any(
        len(message["content"].strip().encode("utf-8"))
        >= _DEFAULT_PROFILE_MIN_TOKENS_TO_COMPRESS
        for message in messages
    )


async def compress_responses_request(
    request: ResponsesRequest,
    *,
    provider: str = "other",
    surface: str = "other",
    config: HeadroomCompressionConfig | None = None,
    compressor: CompressCallable | None = None,
    metrics: HeadroomUsageMetrics | None = None,
) -> HeadroomCompressionOutcome:
    """Compress a ResponsesRequest safely, returning original content on failure."""
    resolved = config or HeadroomCompressionConfig.from_env()
    recorder = DEFAULT_HEADROOM_METRICS if metrics is None else metrics

    async def finish(outcome: HeadroomCompressionOutcome) -> HeadroomCompressionOutcome:
        recorder.record(outcome, provider=provider, surface=surface)
        return outcome

    if not resolved.enabled:
        return await finish(
            HeadroomCompressionOutcome(request=request, reason="disabled")
        )

    projection = _collect_projection(request)
    if not projection.messages:
        return await finish(
            HeadroomCompressionOutcome(request=request, reason="no_text")
        )
    if compressor is None and not _may_reach_headroom_token_floor(
        projection.messages,
        resolved.profile,
    ):
        return await finish(
            HeadroomCompressionOutcome(request=request, reason="below_min_tokens")
        )

    configure_headroom_environment()

    if not _try_reserve_headroom_worker():
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="worker_busy",
                error_type="WorkerBusy",
            )
        )

    worker_started = threading.Event()

    def run_headroom() -> Any:
        worker_started.set()
        try:
            fn = compressor or _import_headroom_compress()
            return fn(
                copy.deepcopy(projection.messages),
                model=request.model or "reverso",
                model_limit=resolved.model_limit,
                savings_profile=resolved.profile,
            )
        finally:
            _release_headroom_worker()

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(_HEADROOM_EXECUTOR, run_headroom),
            timeout=resolved.timeout_seconds,
        )
    except TimeoutError:
        if not worker_started.is_set():
            _release_headroom_worker()
        logger.warning("Headroom compression failed open: timeout")
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="timeout",
                error_type="TimeoutError",
            )
        )
    except Exception as exc:  # noqa: BLE001 - fail open at the dependency boundary
        error_type = type(exc).__name__
        logger.warning("Headroom compression failed open: %s", error_type)
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="exception",
                error_type=error_type,
            )
        )

    tokens_before = _read_result_int(result, "tokens_before")
    tokens_after = _read_result_int(result, "tokens_after")
    tokens_saved = _read_result_int(result, "tokens_saved")
    ratio = _read_result_float(result, "compression_ratio")
    if tokens_before > 0 and tokens_after > tokens_before:
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="inflation_guard",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                error_type="InflationGuard",
            )
        )

    compressed_request = _reconstruct_request(
        projection, getattr(result, "messages", None)
    )
    if compressed_request is not None and _contains_retrieval_marker(
        compressed_request.input
    ):
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="retrieval_marker",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tokens_saved=0,
                compression_ratio=0.0,
                error_type="RetrievalMarker",
            )
        )
    if compressed_request is None:
        return await finish(
            HeadroomCompressionOutcome(
                request=request,
                fail_open=True,
                reason="unsafe_output",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                tokens_saved=0,
                compression_ratio=0.0,
                error_type="UnsafeOutput",
            )
        )

    compressed = tokens_saved > 0 and compressed_request != request
    return await finish(
        HeadroomCompressionOutcome(
            request=compressed_request if compressed else request,
            compressed=compressed,
            reason="compressed" if compressed else "unchanged",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=max(tokens_saved, 0) if compressed else 0,
            compression_ratio=ratio if compressed else 0.0,
        )
    )
