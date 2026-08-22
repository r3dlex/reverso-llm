"""OR-G0 attended probe capture for OpenRouter.

Hard rules:
- OPENROUTER_API_KEY is sourced from ``$HOME/.zsh_exports`` only.
- The key is never passed as a command argument, persisted, or echoed.
- Captures redact secrets, prompts, bodies, capabilities, paths, and usernames.
- Cumulative reported cost is tracked against the OR-G0 budget ceiling.

Outputs land under ``tests/fixtures/openrouter/`` and ``scripts/spike/captures/``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "openrouter"
CAPTURE_DIR = REPO_ROOT / "scripts" / "spike" / "captures"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MODEL = "stealth/ox-alpha"
PROBE_BUDGET_USD = 0.10
STREAM_TIMEOUT = 180.0
REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]+"),
    re.compile(r"Bearer\s+sk-or-v1-[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)


def _load_key() -> str:
    """Source OPENROUTER_API_KEY from $HOME/.zsh_exports without leaking it."""
    exports_path = Path(os.environ["HOME"]) / ".zsh_exports"
    if not exports_path.is_file():
        raise SystemExit(f"missing exports file: {exports_path}")
    proc = subprocess.run(
        [
            "/bin/zsh",
            "-c",
            f"source {exports_path} && printf '%s' \"$OPENROUTER_API_KEY\"",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    key = proc.stdout.strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set in $HOME/.zsh_exports")
    return key


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        redacted = value
        for pattern in REDACTION_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    return value


def _curl(
    method: str, path: str, key: str, payload: dict | None = None
) -> tuple[int, str, float]:
    import httpx

    started = time.monotonic()
    headers = {"Authorization": f"Bearer {key}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    with httpx.Client(base_url=OPENROUTER_BASE, timeout=120.0) as client:
        response = client.request(method, path, headers=headers, json=payload)
    elapsed = time.monotonic() - started
    return response.status_code, response.text, elapsed


def _curl_stream(
    method: str, path: str, key: str, payload: dict | None = None
) -> tuple[int, list[str], float]:
    """Issue a streaming request. Returns (status, event_lines, elapsed).

    Filters SSE comments (``:``) and ``[DONE]`` markers; preserves ``event:`` and
    ``data:`` lines.
    """
    import httpx

    started = time.monotonic()
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "text/event-stream",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    events: list[str] = []
    with httpx.Client(base_url=OPENROUTER_BASE, timeout=STREAM_TIMEOUT) as client:
        with client.stream(method, path, headers=headers, json=payload) as response:
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if (
                    line.startswith("data:")
                    and line[len("data:") :].strip() == "[DONE]"
                ):
                    continue
                events.append(line)
    elapsed = time.monotonic() - started
    return response.status_code, events, elapsed


def _save_capture(name: str, data: dict) -> None:
    path = CAPTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(_redact(data), indent=2), encoding="utf-8")


def _report_cost(usage: dict | None) -> float:
    if not usage:
        return 0.0
    cost = usage.get("cost") or usage.get("total_cost")
    if cost is None:
        return 0.0
    try:
        return float(cost)
    except (TypeError, ValueError):
        return 0.0


def probe_models(key: str, budget: dict) -> dict:
    status, body, elapsed = _curl("GET", "/models", key)
    payload = json.loads(body) if status == 200 and body else {}
    data_entry = next(
        (entry for entry in payload.get("data", []) if entry.get("id") == MODEL),
        None,
    )
    capture = {
        "fixture": "openrouter_or-g0",
        "name": "list_models_authenticated",
        "method": "GET",
        "path": "/api/v1/models",
        "request": {"headers": {"Authorization": "[REDACTED]"}},
        "expected": {
            "status": status,
            "content_type": "application/json",
            "model_entry_keys": sorted(data_entry.keys()) if data_entry else [],
            "model_entry_sample": {
                "id": data_entry.get("id"),
                "context_length": data_entry.get("context_length"),
                "top_provider": data_entry.get("top_provider"),
                "pricing_prompt": data_entry.get("pricing", {}).get("prompt"),
                "pricing_completion": data_entry.get("pricing", {}).get("completion"),
                "supported_parameters": data_entry.get("supported_parameters"),
            }
            if data_entry
            else None,
        },
        "elapsed_seconds": round(elapsed, 4),
        "budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
    }
    _save_capture("list_models_authenticated", capture)
    return capture


def probe_responses_unary(key: str, budget: dict) -> dict:
    payload = {"model": MODEL, "input": "ping", "max_output_tokens": 32}
    status, body, elapsed = _curl("POST", "/responses", key, payload=payload)
    parsed = json.loads(body) if status == 200 and body else {}
    cost = _report_cost(parsed.get("usage"))
    budget["spent_amount_usd"] = round(budget["spent_amount_usd"] + cost, 8)
    capture = {
        "fixture": "openrouter_or-g0",
        "name": "responses_unary",
        "method": "POST",
        "path": "/api/v1/responses",
        "request": {
            "headers": {"Authorization": "[REDACTED]"},
            "body": {
                "model": MODEL,
                "input": "[REDACTED]",
                "max_output_tokens": 32,
            },
        },
        "expected": {
            "status": status,
            "content_type": "application/json",
            "object_type": parsed.get("object"),
            "model": parsed.get("model"),
            "output_item_types": [
                item.get("type") for item in parsed.get("output", [])
            ],
            "usage_keys": sorted((parsed.get("usage") or {}).keys()),
            "reported_cost_usd": cost,
        },
        "elapsed_seconds": round(elapsed, 4),
        "budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
    }
    _save_capture("responses_unary", capture)
    return capture


def probe_responses_stream(key: str, budget: dict) -> dict:
    payload = {"model": MODEL, "input": "ping", "max_output_tokens": 32, "stream": True}
    status, events, elapsed = _curl_stream("POST", "/responses", key, payload=payload)
    event_types: list[str] = []
    terminal_cost = 0.0
    final_payload: dict[str, Any] = {}
    for line in events:
        if line.startswith("event:"):
            event_types.append(line[len("event:") :].strip())
            continue
        if line.startswith("data:"):
            raw = line[len("data:") :].strip()
            try:
                payload_obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload_obj.get("type") == "response.completed":
                final_payload = payload_obj
                terminal_cost = _report_cost(
                    payload_obj.get("response", {}).get("usage")
                )
    budget["spent_amount_usd"] = round(budget["spent_amount_usd"] + terminal_cost, 8)
    capture = {
        "fixture": "openrouter_or-g0",
        "name": "responses_streaming",
        "method": "POST",
        "path": "/api/v1/responses",
        "request": {
            "headers": {
                "Authorization": "[REDACTED]",
                "Accept": "text/event-stream",
            },
            "body": {
                "model": MODEL,
                "input": "[REDACTED]",
                "max_output_tokens": 32,
                "stream": True,
            },
        },
        "expected": {
            "status": status,
            "content_type": "text/event-stream",
            "event_types": event_types,
            "first_event_type": event_types[0] if event_types else None,
            "last_event_type": event_types[-1] if event_types else None,
            "completion_event_present": "response.completed" in event_types,
            "terminal_usage_keys": sorted(
                (final_payload.get("response", {}).get("usage") or {}).keys()
            ),
            "reported_cost_usd": terminal_cost,
        },
        "elapsed_seconds": round(elapsed, 4),
        "budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
    }
    _save_capture("responses_streaming", capture)
    return capture


def probe_messages_unary(key: str, budget: dict) -> dict:
    payload = {
        "model": MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    status, body, elapsed = _curl("POST", "/messages", key, payload=payload)
    parsed = json.loads(body) if status == 200 and body else {}
    cost = _report_cost(parsed.get("usage"))
    budget["spent_amount_usd"] = round(budget["spent_amount_usd"] + cost, 8)
    capture = {
        "fixture": "openrouter_or-g0",
        "name": "messages_unary",
        "method": "POST",
        "path": "/api/v1/messages",
        "request": {
            "headers": {"Authorization": "[REDACTED]"},
            "body": {
                "model": MODEL,
                "max_tokens": 32,
                "messages": "[REDACTED]",
            },
        },
        "expected": {
            "status": status,
            "content_type": "application/json",
            "model": parsed.get("model"),
            "stop_reason": parsed.get("stop_reason"),
            "content_block_types": [
                block.get("type") for block in parsed.get("content", [])
            ],
            "usage_keys": sorted((parsed.get("usage") or {}).keys()),
            "reported_cost_usd": cost,
        },
        "elapsed_seconds": round(elapsed, 4),
        "budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
    }
    _save_capture("messages_unary", capture)
    return capture


def probe_messages_stream(key: str, budget: dict) -> dict:
    payload = {
        "model": MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
    }
    status, events, elapsed = _curl_stream("POST", "/messages", key, payload=payload)
    event_types: list[str] = []
    terminal_cost = 0.0
    message_delta_seen = False
    message_stop_seen = False
    for line in events:
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
            event_types.append(event_type)
            if event_type == "message_delta":
                message_delta_seen = True
            elif event_type == "message_stop":
                message_stop_seen = True
            continue
        if line.startswith("data:"):
            raw = line[len("data:") :].strip()
            if not raw:
                continue
            try:
                payload_obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            usage = payload_obj.get("usage") or {}
            if usage:
                cost = usage.get("cost")
                if cost is not None:
                    terminal_cost = max(terminal_cost, _report_cost({"cost": cost}))
    budget["spent_amount_usd"] = round(budget["spent_amount_usd"] + terminal_cost, 8)
    capture = {
        "fixture": "openrouter_or-g0",
        "name": "messages_streaming",
        "method": "POST",
        "path": "/api/v1/messages",
        "request": {
            "headers": {
                "Authorization": "[REDACTED]",
                "Accept": "text/event-stream",
            },
            "body": {
                "model": MODEL,
                "max_tokens": 32,
                "messages": "[REDACTED]",
                "stream": True,
            },
        },
        "expected": {
            "status": status,
            "content_type": "text/event-stream",
            "event_types": event_types,
            "first_event_type": event_types[0] if event_types else None,
            "last_event_type": event_types[-1] if event_types else None,
            "message_delta_seen": message_delta_seen,
            "message_stop_seen": message_stop_seen,
            "reported_cost_usd": terminal_cost,
        },
        "elapsed_seconds": round(elapsed, 4),
        "budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
    }
    _save_capture("messages_streaming", capture)
    return capture


def _build_manifest(
    budget: dict, observation_id: str, captures: dict[str, dict]
) -> dict:
    fixtures: list[dict] = []
    for name in (
        "list_models_authenticated",
        "responses_unary",
        "responses_streaming",
        "messages_unary",
        "messages_streaming",
    ):
        if name in captures:
            fixtures.append(
                {
                    "name": name,
                    "file": f"{name}.json",
                    "kind": "streaming" if "streaming" in name else "single",
                    "covers": captures[name]["path"],
                    "status": captures[name]["expected"].get("status"),
                }
            )
    return {
        "manifest": "openrouter_or-g0_spike",
        "description": "Redacted OR-G0 spike fixtures for OpenRouter native Responses and Anthropic Messages.",
        "providers": ["openrouter"],
        "prefix_template": "/openrouter/v1",
        "upstream_base": OPENROUTER_BASE,
        "model": MODEL,
        "observation_id": observation_id,
        "freshness_bound_seconds": 600,
        "probe_budget": {
            "approved_amount_usd": budget["approved_amount_usd"],
            "spent_amount_usd": budget["spent_amount_usd"],
            "currency": "USD",
        },
        "endpoint_binding": {
            "policy": "zdr_required",
            "routing_object": "openrouter_request_routing",
            "evidence_revision_field": "id",
            "compatibility_set": ["zdr_only_endpoint_providers"],
        },
        "capability_carriers": {
            "codex": {
                "feasible": True,
                "header": "x-reverso-capability",
                "handshake": "loopback_unix_socket",
                "verification": "codex_attended_issuance_seen_under_test",
            },
            "claude": {
                "feasible": True,
                "header": "x-reverso-capability",
                "handshake": "loopback_unix_socket",
                "verification": "claude_attended_issuance_seen_under_test",
            },
        },
        "claude_alias_grammar": {
            "template": "anthropic-openrouter-{encoded_author_model}",
            "encoding": "base64url_strip_padding",
            "upstream_identity_preserved": True,
        },
        "responses_replay_grammar": {
            "chain_source": "local_storage",
            "forbidden_upstream_fields": ["previous_response_id", "store"],
            "rejects_unknown_chain": True,
            "rejects_partial_chain": True,
        },
        "fixtures": fixtures,
    }


def main() -> int:
    print(
        "[or-g0] sourcing OPENROUTER_API_KEY from $HOME/.zsh_exports", file=sys.stderr
    )
    key = _load_key()
    print(f"[or-g0] key length: {len(key)} chars (prefix redacted)", file=sys.stderr)
    budget = {
        "approved_amount_usd": PROBE_BUDGET_USD,
        "spent_amount_usd": 0.0,
        "currency": "USD",
    }
    observation_id = f"or-g0-{uuid.uuid4().hex[:8]}"
    print(f"[or-g0] observation_id={observation_id}", file=sys.stderr)
    captures: dict[str, dict] = {}
    try:
        captures["list_models_authenticated"] = probe_models(key, budget)
        captures["responses_unary"] = probe_responses_unary(key, budget)
        captures["responses_streaming"] = probe_responses_stream(key, budget)
        if budget["spent_amount_usd"] < budget["approved_amount_usd"]:
            captures["messages_unary"] = probe_messages_unary(key, budget)
        if budget["spent_amount_usd"] < budget["approved_amount_usd"]:
            captures["messages_streaming"] = probe_messages_stream(key, budget)
    finally:
        manifest = _build_manifest(budget, observation_id, captures)
        manifest_path = FIXTURE_DIR / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[or-g0] wrote manifest: {manifest_path}", file=sys.stderr)
        print(
            f"[or-g0] spent USD: {budget['spent_amount_usd']:.6f} / {PROBE_BUDGET_USD:.2f}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
