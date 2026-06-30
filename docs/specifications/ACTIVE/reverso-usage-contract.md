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
  "updated_at": "2026-01-01T00:00:00+00:00"
}
```

When a Codex rollout file provides quota data, `rate_limits` is:

```json
{
  "five_hour": {"used_percent": 0.0, "resets_at": "2026-01-01T00:00:00+00:00"},
  "weekly": {"used_percent": 0.0, "resets_at": "2026-01-01T00:00:00+00:00"},
  "plan_type": "pro"
}
```

## Invariants

- The route reads only the in-process latest-snapshot store.
- Empty store responses are still successful JSON responses with `rate_limits: null`.
- Token counts prefer Codex `turn.completed.usage` values when available.
- Missing rollout quota data keeps the prior `rate_limits` value instead of clearing it.
- `total_tokens` is `input_tokens + output_tokens + reasoning_output_tokens`.
