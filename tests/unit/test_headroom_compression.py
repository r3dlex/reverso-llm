from __future__ import annotations

import asyncio
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.headroom_compression import (
    HeadroomCompressionConfig,
    HeadroomUsageMetrics,
    compress_responses_request,
    configure_headroom_environment,
)


@dataclass
class FakeHeadroomResult:
    messages: list[dict[str, Any]]
    tokens_before: int = 100
    tokens_after: int = 40
    tokens_saved: int = 60
    compression_ratio: float = 0.6


def _rich_request() -> ResponsesRequest:
    return ResponsesRequest(
        model="claude-test",
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "large user text"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                ],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"x"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "large tool output",
                "is_error": True,
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "assistant context"}],
            },
        ],
        stream=True,
        previous_response_id="resp_previous",
        tools=[{"type": "function", "name": "read_file"}],
        instructions="system text",
        tool_choice="auto",
        extra={"temperature": 0.2},
    )


@pytest.mark.asyncio
async def test_disabled_returns_original_without_calling_compressor() -> None:
    request = _rich_request()

    def boom(**_: Any) -> None:
        raise AssertionError("compressor should not be called")

    outcome = await compress_responses_request(
        request,
        config=HeadroomCompressionConfig(enabled=False),
        compressor=boom,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.reason == "disabled"
    assert outcome.compressed is False
    assert outcome.fail_open is False


@pytest.mark.asyncio
async def test_projects_and_reconstructs_text_without_losing_structure() -> None:
    request = _rich_request()
    seen_messages: list[dict[str, Any]] = []

    def fake_compress(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> FakeHeadroomResult:
        nonlocal seen_messages
        seen_messages = messages
        assert kwargs["model"] == "claude-test"
        assert kwargs["savings_profile"] == "agent-90"
        return FakeHeadroomResult(
            messages=[
                {"role": "system", "content": "compressed system"},
                {"role": "user", "content": "compressed user"},
                {"role": "tool", "content": "compressed tool"},
                {"role": "assistant", "content": "compressed assistant"},
            ]
        )

    metrics = HeadroomUsageMetrics()
    outcome = await compress_responses_request(
        request,
        compressor=fake_compress,
        metrics=metrics,
    )

    assert seen_messages == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "large user text"},
        {"role": "tool", "content": "large tool output"},
        {"role": "assistant", "content": "assistant context"},
    ]
    assert outcome.compressed is True
    assert outcome.request is not request
    assert request.input[0]["content"][0]["text"] == "large user text"
    assert outcome.request.instructions == "compressed system"
    assert outcome.request.input[0]["content"][0]["text"] == "compressed user"
    assert outcome.request.input[0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc",
    }
    assert outcome.request.input[1] == request.input[1]
    assert outcome.request.input[2]["output"] == "compressed tool"
    assert outcome.request.input[2]["is_error"] is True
    assert outcome.request.input[3]["content"][0]["text"] == "compressed assistant"
    assert outcome.request.tools == request.tools
    assert outcome.request.previous_response_id == "resp_previous"
    assert outcome.request.extra == {"temperature": 0.2}
    assert metrics.snapshot()["requests_compressed"] == 1
    assert metrics.snapshot()["tokens_saved"] == 60


@pytest.mark.asyncio
async def test_compressor_exception_fails_open() -> None:
    request = _rich_request()
    metrics = HeadroomUsageMetrics()

    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("nope")

    outcome = await compress_responses_request(
        request,
        compressor=broken,
        metrics=metrics,
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "exception"
    assert outcome.error_type == "RuntimeError"
    snapshot = metrics.snapshot()
    assert snapshot["fail_open_count"] == 1
    assert snapshot["failure_reasons"] == {"exception": 1}
    assert snapshot["error_types"] == {"RuntimeError": 1}


@pytest.mark.asyncio
async def test_timeout_fails_open() -> None:
    request = ResponsesRequest(model="m", input="large text")

    def slow(*_: Any, **__: Any) -> FakeHeadroomResult:
        time.sleep(0.05)
        return FakeHeadroomResult(messages=[{"role": "user", "content": "late"}])

    outcome = await compress_responses_request(
        request,
        config=HeadroomCompressionConfig(timeout_seconds=0.001),
        compressor=slow,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "timeout"
    assert outcome.error_type == "TimeoutError"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_timeout_leaves_worker_busy_instead_of_queueing_work() -> None:
    request = ResponsesRequest(model="m", input="large text")
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow(*_: Any, **__: Any) -> FakeHeadroomResult:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1.0)
        return FakeHeadroomResult(messages=[{"role": "user", "content": "late"}])

    try:
        first = await compress_responses_request(
            request,
            config=HeadroomCompressionConfig(timeout_seconds=0.05),
            compressor=slow,
            metrics=HeadroomUsageMetrics(),
        )
        assert started.wait(timeout=1.0)

        second = await compress_responses_request(
            request,
            config=HeadroomCompressionConfig(timeout_seconds=0.1),
            compressor=slow,
            metrics=HeadroomUsageMetrics(),
        )
    finally:
        release.set()
        await asyncio.sleep(0.05)

    assert first.reason == "timeout"
    assert second.reason == "worker_busy"
    assert second.error_type == "WorkerBusy"
    assert calls == 1


@pytest.mark.asyncio
async def test_inflation_guard_fails_open() -> None:
    request = ResponsesRequest(model="m", input="large text")

    def inflated(*_: Any, **__: Any) -> FakeHeadroomResult:
        return FakeHeadroomResult(
            messages=[{"role": "user", "content": "larger text"}],
            tokens_before=10,
            tokens_after=20,
            tokens_saved=-10,
            compression_ratio=-1.0,
        )

    outcome = await compress_responses_request(
        request,
        compressor=inflated,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "inflation_guard"
    assert outcome.tokens_before == 10
    assert outcome.tokens_after == 20
    assert outcome.error_type == "InflationGuard"


@pytest.mark.asyncio
async def test_unsafe_output_shape_fails_open() -> None:
    request = _rich_request()

    def unsafe(*_: Any, **__: Any) -> FakeHeadroomResult:
        return FakeHeadroomResult(messages=[{"role": "user", "content": "too few"}])

    outcome = await compress_responses_request(
        request,
        compressor=unsafe,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "unsafe_output"
    assert outcome.error_type == "UnsafeOutput"


@pytest.mark.asyncio
async def test_non_dict_output_message_fails_open() -> None:
    request = ResponsesRequest(model="m", input="large text")

    def unsafe(*_: Any, **__: Any) -> FakeHeadroomResult:
        return FakeHeadroomResult(messages=[None])  # type: ignore[list-item]

    outcome = await compress_responses_request(
        request,
        compressor=unsafe,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "unsafe_output"


@pytest.mark.asyncio
async def test_role_mismatch_fails_open() -> None:
    request = _rich_request()

    def swapped_roles(*_: Any, **__: Any) -> FakeHeadroomResult:
        return FakeHeadroomResult(
            messages=[
                {"role": "user", "content": "wrong system"},
                {"role": "system", "content": "wrong user"},
                {"role": "tool", "content": "compressed tool"},
                {"role": "assistant", "content": "compressed assistant"},
            ]
        )

    outcome = await compress_responses_request(
        request,
        compressor=swapped_roles,
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.request is request
    assert outcome.fail_open is True
    assert outcome.reason == "unsafe_output"
    assert request.instructions == "system text"


def test_env_defaults_enforce_stateless_memory_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_STATELESS", raising=False)
    monkeypatch.delenv("HEADROOM_TELEMETRY", raising=False)
    monkeypatch.setenv("HEADROOM_UPDATE_CHECK", "custom")

    configure_headroom_environment()

    assert os_environ("HEADROOM_STATELESS") == "true"
    assert os_environ("HEADROOM_TELEMETRY") == "off"
    assert os_environ("HEADROOM_TELEMETRY_DISABLED") == "1"
    assert os_environ("HEADROOM_UPDATE_CHECK") == "off"
    assert os_environ("HEADROOM_PERIODIC_TOIN_STATS") == "0"
    assert os_environ("HEADROOM_CCR_BACKEND") == "memory"
    assert os_environ("HEADROOM_MEMORY_ENABLED") == "0"


@pytest.mark.asyncio
async def test_real_headroom_smoke_uses_memory_only_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HEADROOM_CCR_BACKEND", raising=False)
    monkeypatch.delenv("HEADROOM_WORKSPACE_DIR", raising=False)
    request = ResponsesRequest(model="test-model", input="hello world " * 20)

    outcome = await compress_responses_request(
        request,
        config=HeadroomCompressionConfig(timeout_seconds=5.0, model_limit=1000),
        metrics=HeadroomUsageMetrics(),
    )

    assert outcome.reason in {"compressed", "unchanged"}
    assert list(tmp_path.rglob("*")) == []


def os_environ(name: str) -> str | None:
    import os

    return os.environ.get(name)


def test_config_from_env_defaults_enabled_agent_90(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVERSO_HEADROOM_ENABLED", raising=False)
    monkeypatch.delenv("REVERSO_HEADROOM_PROFILE", raising=False)

    config = HeadroomCompressionConfig.from_env()

    assert config.enabled is True
    assert config.profile == "agent-90"


def test_config_from_env_supports_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVERSO_HEADROOM_ENABLED", "0")
    monkeypatch.setenv("REVERSO_HEADROOM_PROFILE", "balanced")

    config = HeadroomCompressionConfig.from_env()

    assert config.enabled is False
    assert config.profile == "balanced"
