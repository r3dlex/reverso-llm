---
title: Reverso Codex Usage Contract
status: active
---

# Reverso Codex Usage Contract

## Purpose

`GET /usage` exposes the latest completed Codex turn telemetry for local HUDs and
agent status surfaces. The route is read-only and must not spawn `codex` or any
other subprocess.

## Response shape

The response body is JSON with `schema_version: 1`.

```json
{
  "schema_version": 1,
  "model_id": "gpt-5.5",
  "provider": "codex",
  "tokens": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "context": {
    "used_tokens": 0,
    "window_tokens": 0,
    "used_percent": 0.0
  },
  "rate_limits": null,
  "updated_at": "2026-01-01T00:00:00+00:00",
  "headroom": {
    "enabled": true,
    "profile": "agent-90",
    "requests_seen": 0,
    "requests_compressed": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
    "compression_ratio": 0.0,
    "fail_open_count": 0,
    "failure_reasons": {},
    "error_types": {},
    "updated_at": null
  }
}
```

For a completed Codex turn whose context window is not known,
`context.window_tokens` and `context.used_percent` are `null`. Consumers must
render that case as unknown (for example `n/a`) rather than treating it as zero
usage. Empty-store responses still use numeric zero values for the context
block.

When a Codex rollout file provides quota data, `rate_limits` is:

```json
{
  "five_hour": {"used_percent": 0.0, "resets_at": "2026-01-01T00:00:00+00:00"},
  "weekly": {"used_percent": 0.0, "resets_at": "2026-01-01T00:00:00+00:00"},
  "plan_type": "pro"
}
```

## Headroom usage

`GET /usage` includes a top-level `headroom` object with process-local aggregate
compression metrics. The same aggregate is available directly at
`GET /usage/headroom`:

```json
{
  "schema_version": 1,
  "provider": "headroom",
  "headroom": {
    "enabled": true,
    "profile": "agent-90",
    "requests_seen": 0,
    "requests_compressed": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
    "compression_ratio": 0.0,
    "fail_open_count": 0,
    "failure_reasons": {},
    "error_types": {},
    "updated_at": null
  }
}
```

Headroom metrics are aggregate-only and never include prompt text, input item
content, compressed text, response text, or per-request identifiers. They reset on
gateway process restart. `enabled` and `profile` reflect the current environment
configuration; setting `REVERSO_HEADROOM_ENABLED=0` disables compression and is
reported by this surface after restart.

## Invariants

- The route reads only in-process latest-snapshot and aggregate-metrics stores.
- Empty store responses are still successful JSON responses with `rate_limits: null`.
- `GET /usage/headroom` is successful even before any compressed request.
- Usage routes must not spawn `codex`, Headroom, providers, or any subprocess.
- Token counts prefer Codex `turn.completed.usage` values when available.
- Missing rollout quota data keeps the prior `rate_limits` value instead of clearing it.
- `total_tokens` is `input_tokens + output_tokens + reasoning_output_tokens`.
