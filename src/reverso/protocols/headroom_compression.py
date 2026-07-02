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
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable

from reverso.protocols.adapter import ResponsesRequest

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_DEFAULT_PROFILE = "agent-90"
_DEFAULT_TIMEOUT_SECONDS = 2.0
_DEFAULT_MODEL_LIMIT = 200000

CompressCallable = Callable[..., Any]

logger = logging.getLogger(__name__)

_HEADROOM_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="reverso-headroom",
)
_HEADROOM_WORKER_LOCK = threading.Lock()
_HEADROOM_ACTIVE_WORKERS = 0


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
    def from_env(cls, env: dict[str, str] | None = None) -> "HeadroomCompressionConfig":
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

    requests_seen: int = 0
    requests_compressed: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    fail_open_count: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    error_types: dict[str, int] = field(default_factory=dict)
    updated_at: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, outcome: HeadroomCompressionOutcome) -> None:
        """Record one compression attempt without storing prompt content."""
        with self._lock:
            self.requests_seen += 1
            if outcome.compressed:
                self.requests_compressed += 1
            if outcome.fail_open:
                self.fail_open_count += 1
                self.failure_reasons[outcome.reason] = (
                    self.failure_reasons.get(outcome.reason, 0) + 1
                )
            if outcome.error_type:
                self.error_types[outcome.error_type] = (
                    self.error_types.get(outcome.error_type, 0) + 1
                )
            self.tokens_before += max(outcome.tokens_before, 0)
            self.tokens_after += max(outcome.tokens_after, 0)
            self.tokens_saved += max(outcome.tokens_saved, 0)
            self.updated_at = datetime.now(UTC).isoformat()

    def snapshot(
        self, config: HeadroomCompressionConfig | None = None
    ) -> dict[str, Any]:
        """Return prompt-free aggregate metrics."""
        with self._lock:
            ratio = (
                self.tokens_saved / self.tokens_before if self.tokens_before else 0.0
            )
            return {
                "enabled": True if config is None else config.enabled,
                "profile": _DEFAULT_PROFILE if config is None else config.profile,
                "requests_seen": self.requests_seen,
                "requests_compressed": self.requests_compressed,
                "tokens_before": self.tokens_before,
                "tokens_after": self.tokens_after,
                "tokens_saved": self.tokens_saved,
                "compression_ratio": ratio,
                "fail_open_count": self.fail_open_count,
                "failure_reasons": dict(self.failure_reasons),
                "error_types": dict(self.error_types),
                "updated_at": self.updated_at,
            }

    def reset(self) -> None:
        """Reset process-local metrics, used by tests."""
        with self._lock:
            self.requests_seen = 0
            self.requests_compressed = 0
            self.tokens_before = 0
            self.tokens_after = 0
            self.tokens_saved = 0
            self.fail_open_count = 0
            self.failure_reasons = {}
            self.error_types = {}
            self.updated_at = None


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
        if text:
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


async def compress_responses_request(
    request: ResponsesRequest,
    *,
    config: HeadroomCompressionConfig | None = None,
    compressor: CompressCallable | None = None,
    metrics: HeadroomUsageMetrics | None = None,
) -> HeadroomCompressionOutcome:
    """Compress a ResponsesRequest safely, returning original content on failure."""
    resolved = config or HeadroomCompressionConfig.from_env()
    recorder = DEFAULT_HEADROOM_METRICS if metrics is None else metrics

    async def finish(outcome: HeadroomCompressionOutcome) -> HeadroomCompressionOutcome:
        recorder.record(outcome)
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
    except Exception as exc:
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
