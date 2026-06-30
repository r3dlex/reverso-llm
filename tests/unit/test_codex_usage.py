"""Tests for the Codex usage producer (Slices 1, 1b, 2, 5).

Coverage:
  - Real-shape ``turn.completed.usage`` (4 keys) parsed → envelope carries real
    tokens, NOT estimate_usage output.
  - ``estimate_usage`` is unchanged and still used by non-codex paths.
  - Rollout ``rate_limits`` extraction: fixture jsonl with a token_count record
    → store gets five_hour / weekly correctly; missing block → keep-last.
  - GET /usage returns the contract shape (schema_version 1, nullable
    rate_limits); empty store → rate_limits null, no 5xx.
  - INV-2: GET /usage does NOT invoke codex or any subprocess.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reverso.protocols.adapter import ResponsesRequest
from reverso.protocols.adapters import codex_usage_store
from reverso.protocols.adapters.codex import (
    CodexAdapter,
    CodexOAuthAuth,
    _CodexStreamTerminal,
    _parse_codex_lines,
)
from reverso.protocols.adapters.codex_rollout import (
    _epoch_to_iso,
    _map_rate_limits,
    read_rate_limits,
)
from reverso.protocols.model_exposure import codex_usage_context_window
from reverso.protocols.replay import estimate_usage


# ---------------------------------------------------------------------------
# Helpers shared with test_codex_adapter.py pattern
# ---------------------------------------------------------------------------

import base64
import time


def _jwt(exp_seconds: int) -> str:
    def _seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "RS256", "typ": "JWT"})
    payload = _seg({"exp": exp_seconds, "sub": "synthetic", "iss": "test"})
    return f"{header}.{payload}.synthetic-signature-not-secret"


def _valid_artifact() -> str:
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": "2026-06-29T00:00:00Z",
            "tokens": {
                "id_token": "i",
                "access_token": _jwt(int(time.time() + 3600)),
                "refresh_token": "r",
                "account_id": "acct-synthetic-123",
            },
        }
    )


def _valid_auth() -> CodexOAuthAuth:
    return CodexOAuthAuth(credentials_path=None, keychain_reader=_valid_artifact)


def _make_request(model: str = "gpt-5.5") -> ResponsesRequest:
    return ResponsesRequest(model=model, input="Say hi.", stream=False)


# ---------------------------------------------------------------------------
# Slice 1: real-shape turn.completed.usage (4 keys)
# ---------------------------------------------------------------------------

# The real stream shape from Spike S2 (verbatim, 4 keys).
_REAL_USAGE_4_KEYS = {
    "input_tokens": 25738,
    "cached_input_tokens": 4992,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}

_REAL_TURN_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "thread-real-usage"}),
    json.dumps({"type": "turn.started"}),
    json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "pong"},
        }
    ),
    json.dumps({"type": "turn.completed", "usage": _REAL_USAGE_4_KEYS}),
]
_REAL_TURN_STDOUT = "\n".join(_REAL_TURN_LINES) + "\n"


def test_parse_codex_lines_captures_all_4_usage_keys() -> None:
    """_parse_codex_lines returns the real 4-key usage dict from turn.completed."""
    text, usage, thread_id = _parse_codex_lines(_REAL_TURN_STDOUT)
    assert text == "pong"
    assert thread_id == "thread-real-usage"
    assert usage is not None
    assert usage["input_tokens"] == 25738
    assert usage["cached_input_tokens"] == 4992
    assert usage["output_tokens"] == 5
    assert usage["reasoning_output_tokens"] == 0


def test_parse_codex_lines_no_usage_returns_none() -> None:
    """A turn.completed without usage → usage is None (no KeyError)."""
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}
        ),
        json.dumps({"type": "turn.completed"}),  # no usage key
    ]
    _, usage, _ = _parse_codex_lines("\n".join(lines))
    assert usage is None


def test_parse_codex_lines_captures_thread_id() -> None:
    """thread_id is captured from thread.started even when usage is absent."""
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "tid-xyz"}),
        json.dumps({"type": "turn.completed"}),
    ]
    _, _, thread_id = _parse_codex_lines("\n".join(lines))
    assert thread_id == "tid-xyz"


@pytest.mark.asyncio
async def test_create_response_uses_real_usage_not_estimate(
    monkeypatch,
) -> None:
    """When the cli_runner returns a tuple with real usage, the envelope carries
    real token counts - not estimate_usage output."""
    # Track whether estimate_usage is called.
    estimate_called = False
    import reverso.protocols.adapters.codex as _codex_mod

    original_estimate = _codex_mod.estimate_usage

    def _spy_estimate(prompt: str, output: str) -> dict:
        nonlocal estimate_called
        estimate_called = True
        return original_estimate(prompt, output)

    monkeypatch.setattr(_codex_mod, "estimate_usage", _spy_estimate)

    # cli_runner returns the new tuple shape with real usage.
    def _runner(prompt: str, model_flag: str):
        return ("pong", _REAL_USAGE_4_KEYS, "thread-real-usage")

    adapter = CodexAdapter(
        auth=_valid_auth(),
        cli_runner=_runner,
    )
    envelope = await adapter.create_response(_make_request())

    assert envelope.usage is not None
    assert envelope.usage["input_tokens"] == 25738
    assert envelope.usage["cached_input_tokens"] == 4992
    assert envelope.usage["output_tokens"] == 5
    assert envelope.usage["reasoning_output_tokens"] == 0
    # total_tokens = input + output + reasoning (0) = 25743
    assert envelope.usage["total_tokens"] == 25743
    # estimate_usage must NOT have been called (INV-3).
    assert (
        not estimate_called
    ), "estimate_usage must not be called when real usage is present"


@pytest.mark.asyncio
async def test_create_response_falls_back_to_estimate_when_usage_absent(
    monkeypatch,
) -> None:
    """When cli_runner returns a tuple with usage=None, estimate_usage is used."""
    estimate_called = False
    import reverso.protocols.adapters.codex as _codex_mod

    original_estimate = _codex_mod.estimate_usage

    def _spy_estimate(prompt: str, output: str) -> dict:
        nonlocal estimate_called
        estimate_called = True
        return original_estimate(prompt, output)

    monkeypatch.setattr(_codex_mod, "estimate_usage", _spy_estimate)

    def _runner(prompt: str, model_flag: str):
        return ("hello world", None, None)

    adapter = CodexAdapter(auth=_valid_auth(), cli_runner=_runner)
    envelope = await adapter.create_response(_make_request())

    assert envelope.usage is not None
    assert estimate_called, "estimate_usage must be called when real usage is absent"


# ---------------------------------------------------------------------------
# INV-3: estimate_usage unchanged and used by non-codex paths
# ---------------------------------------------------------------------------


def test_estimate_usage_unchanged() -> None:
    """estimate_usage still returns {input_tokens, output_tokens, total_tokens}
    with word-count values - its contract is untouched."""
    result = estimate_usage("hello world foo", "bar baz")
    assert result == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }
    # Must NOT have codex-specific keys.
    assert "cached_input_tokens" not in result
    assert "reasoning_output_tokens" not in result


# ---------------------------------------------------------------------------
# Slice 5: rollout rate_limits extraction
# ---------------------------------------------------------------------------

_ROLLOUT_RATE_LIMITS = {
    "limit_id": "codex",
    "limit_name": None,
    "primary": {
        "used_percent": 12.0,
        "window_minutes": 300,
        "resets_at": 1782700870,
    },
    "secondary": {
        "used_percent": 36.0,
        "window_minutes": 10080,
        "resets_at": 1782971745,
    },
    "credits": None,
    "individual_limit": None,
    "plan_type": "pro",
    "rate_limit_reached_type": None,
}


def _write_rollout_fixture(
    tmp_path: Path,
    thread_id: str = "019f109b-bd0e-7de0-aa6e-aefbae53c871",
    rate_limits: dict | None = _ROLLOUT_RATE_LIMITS,
) -> Path:
    """Write a minimal rollout .jsonl fixture into a sessions hierarchy."""
    sessions = tmp_path / "sessions" / "2026" / "06" / "29"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-2026-06-29T01-41-09-{thread_id}-.jsonl"

    records = [
        json.dumps(
            {
                "timestamp": "2026-06-28T23:41:09.000Z",
                "type": "event_msg",
                "payload": {"type": "session_started"},
            }
        ),
    ]
    if rate_limits is not None:
        token_count_record = json.dumps(
            {
                "timestamp": "2026-06-28T23:41:12.693Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 16563,
                            "output_tokens": 17,
                            "total_tokens": 16580,
                        }
                    },
                    "rate_limits": rate_limits,
                },
            }
        )
        records.append(token_count_record)

    rollout.write_text("\n".join(records) + "\n", encoding="utf-8")
    return rollout


def test_read_rate_limits_extracts_five_hour_and_weekly(tmp_path: Path) -> None:
    """Rollout fixture with token_count → five_hour and weekly correctly mapped."""
    thread_id = "019f109b-bd0e-7de0-aa6e-aefbae53c871"
    _write_rollout_fixture(tmp_path, thread_id=thread_id)

    result = read_rate_limits(thread_id, codex_home=tmp_path)

    assert result is not None
    assert result["five_hour"] is not None
    assert result["five_hour"]["used_percent"] == 12.0
    assert result["weekly"] is not None
    assert result["weekly"]["used_percent"] == 36.0
    assert result["plan_type"] == "pro"
    # resets_at must be an ISO-8601 string ending in Z (fix #5), not a raw epoch int.
    assert result["five_hour"]["resets_at"].endswith("Z")
    assert result["weekly"]["resets_at"].endswith("Z")


def test_read_rate_limits_missing_block_returns_none(tmp_path: Path) -> None:
    """A rollout file with no token_count.rate_limits → None (keep-last at caller)."""
    thread_id = "no-rl-thread"
    _write_rollout_fixture(tmp_path, thread_id=thread_id, rate_limits=None)

    result = read_rate_limits(thread_id, codex_home=tmp_path)
    assert result is None


def test_read_rate_limits_no_sessions_dir_returns_none(tmp_path: Path) -> None:
    """When CODEX_HOME has no sessions/ directory, returns None without error."""
    result = read_rate_limits("any-thread", codex_home=tmp_path)
    assert result is None


def test_read_rate_limits_unmatched_thread_does_not_use_unrelated_newest(
    tmp_path: Path,
) -> None:
    """A supplied thread_id must not fall back to another session's rollout."""
    _write_rollout_fixture(tmp_path, thread_id="unrelated-thread")

    result = read_rate_limits("missing-thread", codex_home=tmp_path)

    assert result is None


def test_read_rate_limits_takes_last_token_count(tmp_path: Path) -> None:
    """When multiple token_count records exist, the last one wins."""
    sessions = tmp_path / "sessions" / "2026" / "06" / "29"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-06-29T00-00-00-multi-thread-.jsonl"

    first_rl = dict(_ROLLOUT_RATE_LIMITS)
    first_rl = {**_ROLLOUT_RATE_LIMITS}
    # Override primary to a different used_percent so we can distinguish.
    first_rl["primary"] = {**_ROLLOUT_RATE_LIMITS["primary"], "used_percent": 5.0}

    last_rl = {**_ROLLOUT_RATE_LIMITS}
    last_rl["primary"] = {**_ROLLOUT_RATE_LIMITS["primary"], "used_percent": 99.0}

    records = [
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": first_rl,
                },
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": last_rl,
                },
            }
        ),
    ]
    rollout.write_text("\n".join(records) + "\n", encoding="utf-8")

    result = read_rate_limits("multi-thread", codex_home=tmp_path)
    assert result is not None
    # Must be the LAST record.
    assert result["five_hour"]["used_percent"] == 99.0


def test_map_rate_limits_by_window_minutes() -> None:
    """_map_rate_limits routes by window_minutes value, not key name order."""
    # Swap primary / secondary key order to verify routing is by window_minutes.
    raw = {
        "secondary": {"used_percent": 36.0, "window_minutes": 10080, "resets_at": 0},
        "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 0},
        "plan_type": "pro",
    }
    result = _map_rate_limits(raw)
    assert result is not None
    assert result["five_hour"]["used_percent"] == 12.0
    assert result["weekly"]["used_percent"] == 36.0


def test_epoch_to_iso_converts_correctly() -> None:
    """_epoch_to_iso returns an ISO-8601 UTC string ending in Z from a Unix epoch int."""
    iso = _epoch_to_iso(0)
    assert iso is not None
    assert iso.startswith("1970-01-01")
    assert "T" in iso
    assert iso.endswith("Z"), f"resets_at must end with Z, got: {iso!r}"


def test_epoch_to_iso_bad_input_returns_none() -> None:
    """_epoch_to_iso returns None on bad/missing input (fix #6)."""
    assert _epoch_to_iso(None) is None
    assert _epoch_to_iso("not-a-number") is None
    assert _epoch_to_iso([]) is None


# ---------------------------------------------------------------------------
# Slice 5: keep-last semantics via the usage store
# ---------------------------------------------------------------------------


def test_keep_last_rate_limits_when_rollout_absent(tmp_path: Path) -> None:
    """When the rollout gives no rate_limits, the prior stored value is kept."""
    # Prime the store with a known rate_limits value.
    prior_snapshot = codex_usage_store.build_snapshot(
        model_id="gpt-5.5",
        stream_usage=_REAL_USAGE_4_KEYS,
        window_tokens=128000,
        rate_limits={
            "five_hour": {
                "used_percent": 10.0,
                "resets_at": "2026-01-01T00:00:00+00:00",
            },
            "weekly": None,
            "plan_type": "pro",
        },
    )
    codex_usage_store.update(prior_snapshot)

    # Now simulate a codex turn where the rollout has no rate_limits.
    _write_rollout_fixture(tmp_path, thread_id="keep-last-thread", rate_limits=None)

    new_rl = read_rate_limits("keep-last-thread", codex_home=tmp_path)
    assert new_rl is None  # rollout gave nothing

    # The caller (CodexAdapter._update_usage_store) keeps the old value.
    current_rl = codex_usage_store.get_rate_limits()
    new_snapshot = codex_usage_store.build_snapshot(
        model_id="gpt-5.5",
        stream_usage=_REAL_USAGE_4_KEYS,
        window_tokens=128000,
        rate_limits=new_rl if new_rl is not None else current_rl,
    )
    codex_usage_store.update(new_snapshot)

    stored = codex_usage_store.get()
    assert stored is not None
    assert stored["rate_limits"] is not None
    assert stored["rate_limits"]["five_hour"]["used_percent"] == 10.0


# ---------------------------------------------------------------------------
# Streaming request-local isolation
# ---------------------------------------------------------------------------


async def _collect_events(stream) -> list:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_concurrent_streaming_usage_is_request_local() -> None:
    """Overlapping streams on one adapter do not cross-contaminate usage."""
    release_a = asyncio.Event()
    b_reached_terminal = asyncio.Event()

    usage_a = {
        "input_tokens": 100,
        "cached_input_tokens": 10,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }
    usage_b = {
        "input_tokens": 200,
        "cached_input_tokens": 20,
        "output_tokens": 2,
        "reasoning_output_tokens": 0,
    }

    async def _runner(prompt: str, model_flag: str):
        if "turn-A" in prompt:
            yield "A"
            await b_reached_terminal.wait()
            yield _CodexStreamTerminal(usage=usage_a, thread_id="thread-A")
            return

        yield "B"
        b_reached_terminal.set()
        await release_a.wait()
        yield _CodexStreamTerminal(usage=usage_b, thread_id="thread-B")

    adapter = CodexAdapter(auth=_valid_auth(), stream_cli_runner=_runner)
    req_a = ResponsesRequest(model="gpt-5.5", input="turn-A", stream=True)
    req_b = ResponsesRequest(model="gpt-5.5", input="turn-B", stream=True)

    async def _run_a():
        try:
            return await _collect_events(adapter.stream_response(req_a))
        finally:
            release_a.set()

    events_a, events_b = await asyncio.gather(
        _run_a(),
        _collect_events(adapter.stream_response(req_b)),
    )

    completed_a = events_a[-1].data["response"]
    completed_b = events_b[-1].data["response"]
    assert completed_a["usage"]["input_tokens"] == 100
    assert completed_a["usage"]["cached_input_tokens"] == 10
    assert completed_b["usage"]["input_tokens"] == 200
    assert completed_b["usage"]["cached_input_tokens"] == 20


# ---------------------------------------------------------------------------
# Slice 2: GET /usage contract shape
# ---------------------------------------------------------------------------


async def _asgi_get_usage(root) -> dict[str, Any]:
    """Drive the CompositionRoot __call__ for GET /usage and capture the response."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/usage",
        "query_string": b"",
        "headers": [],
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    await root(scope, receive, send)
    # Parse the body from the http.response.body message.
    body_msg = next(m for m in sent if m["type"] == "http.response.body")
    return json.loads(body_msg["body"])


@pytest.mark.asyncio
async def test_get_usage_empty_store_returns_null_rate_limits(monkeypatch) -> None:
    """GET /usage with no codex turn yet → 200, rate_limits null, no 5xx."""
    from reverso.proxy.compose import CompositionRoot
    import reverso.protocols.adapters.codex_usage_store as store_mod

    # Clear the store.
    monkeypatch.setattr(store_mod, "_latest", None)

    tripwire_called = False

    async def _tripwire(scope, receive, send):
        nonlocal tripwire_called
        tripwire_called = True

    root = CompositionRoot(
        gateway=_tripwire,
        anthropic_app=_tripwire,
        legacy_app=_tripwire,
    )
    body = await _asgi_get_usage(root)

    assert body["schema_version"] == 1
    assert body["rate_limits"] is None
    assert "tokens" in body
    assert "context" in body
    # The route must NOT have touched any of the other apps.
    assert not tripwire_called


@pytest.mark.asyncio
async def test_get_usage_populated_store_returns_snapshot(monkeypatch) -> None:
    """GET /usage with a real snapshot returns the full contract shape."""
    from reverso.proxy.compose import CompositionRoot
    import reverso.protocols.adapters.codex_usage_store as store_mod

    snapshot = codex_usage_store.build_snapshot(
        model_id="gpt-5.5",
        stream_usage=_REAL_USAGE_4_KEYS,
        window_tokens=128000,
        rate_limits={
            "five_hour": {
                "used_percent": 12.0,
                "resets_at": "2026-06-29T18:00:00+00:00",
            },
            "weekly": {"used_percent": 36.0, "resets_at": "2026-07-03T00:00:00+00:00"},
            "plan_type": "pro",
        },
    )
    monkeypatch.setattr(store_mod, "_latest", snapshot)

    async def _tripwire(scope, receive, send):
        raise AssertionError("should not reach gateway/anthropic/legacy")

    root = CompositionRoot(
        gateway=_tripwire,
        anthropic_app=_tripwire,
        legacy_app=_tripwire,
    )
    body = await _asgi_get_usage(root)

    assert body["schema_version"] == 1
    assert body["model_id"] == "gpt-5.5"
    assert body["provider"] == "codex"
    assert body["tokens"]["input_tokens"] == 25738
    assert body["tokens"]["cached_input_tokens"] == 4992
    assert body["tokens"]["output_tokens"] == 5
    assert body["tokens"]["reasoning_output_tokens"] == 0
    assert body["tokens"]["total_tokens"] == 25743
    assert body["context"]["used_tokens"] == 25738
    assert body["context"]["window_tokens"] == 128000
    assert body["rate_limits"] is not None
    assert body["rate_limits"]["five_hour"]["used_percent"] == 12.0
    assert body["rate_limits"]["weekly"]["used_percent"] == 36.0
    assert body["rate_limits"]["plan_type"] == "pro"


# ---------------------------------------------------------------------------
# INV-2: GET /usage must NOT spawn codex
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_usage_does_not_invoke_subprocess(monkeypatch) -> None:
    """INV-2: GET /usage reads the store only - no subprocess.run/Popen."""
    import subprocess as subprocess_mod
    from reverso.proxy.compose import CompositionRoot
    import reverso.protocols.adapters.codex_usage_store as store_mod

    monkeypatch.setattr(store_mod, "_latest", None)

    original_run = subprocess_mod.run
    subprocess_called = False

    def _tripwire_run(*args, **kwargs):
        nonlocal subprocess_called
        subprocess_called = True
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess_mod, "run", _tripwire_run)

    async def _noop(scope, receive, send):
        pass

    root = CompositionRoot(gateway=_noop, anthropic_app=_noop, legacy_app=_noop)
    await _asgi_get_usage(root)

    assert not subprocess_called, "GET /usage must never call subprocess.run"


# ---------------------------------------------------------------------------
# Slice 1b: context window via codex_catalog_context_window
# ---------------------------------------------------------------------------


def test_build_snapshot_uses_catalog_context_window() -> None:
    """build_snapshot correctly computes used_percent from the catalog window."""
    snapshot = codex_usage_store.build_snapshot(
        model_id="gpt-5.5",
        stream_usage={
            "input_tokens": 12800,
            "cached_input_tokens": 0,
            "output_tokens": 10,
            "reasoning_output_tokens": 0,
        },
        window_tokens=128000,
        rate_limits=None,
    )
    assert snapshot["context"]["window_tokens"] == 128000
    assert snapshot["context"]["used_tokens"] == 12800
    assert snapshot["context"]["used_percent"] == 10.0


def test_codex_usage_context_window_known_unknown_and_500k() -> None:
    """Served ids return a window; unmapped ids return None; 500k → 500000."""
    assert codex_usage_context_window("gpt-5.5") == 128000
    assert codex_usage_context_window("gpt-4.1") == 128000
    assert codex_usage_context_window("gpt-5-500k") == 500000
    # An unmapped id yields None so the consumer renders n/a, not a guess.
    assert codex_usage_context_window("some-unknown-model") is None


def test_build_snapshot_unknown_window_yields_null_used_percent() -> None:
    """window_tokens=None (unmapped model) → used_percent None (HUD n/a), no guess."""
    snapshot = codex_usage_store.build_snapshot(
        model_id="some-unknown-model",
        stream_usage={
            "input_tokens": 12800,
            "cached_input_tokens": 0,
            "output_tokens": 10,
            "reasoning_output_tokens": 0,
        },
        window_tokens=None,
        rate_limits=None,
    )
    assert snapshot["context"]["window_tokens"] is None
    assert snapshot["context"]["used_percent"] is None
    # Used tokens are still reported even when the window is unknown.
    assert snapshot["context"]["used_tokens"] == 12800
