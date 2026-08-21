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
    "schema_version": 2,
    "enabled": true,
    "profile": "coding",
    "requests_seen": 0,
    "requests_compressed": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
    "compression_ratio": 0.0,
    "fail_open_count": 0,
    "failure_reasons": {
      "worker_busy": 0,
      "timeout": 0,
      "exception": 0,
      "inflation_guard": 0,
      "retrieval_marker": 0,
      "unsafe_output": 0,
      "other": 0
    },
    "error_types": {
      "timeout": 0,
      "worker_busy": 0,
      "dependency_exception": 0,
      "inflation_guard": 0,
      "retrieval_marker": 0,
      "unsafe_output": 0,
      "other": 0
    },
    "updated_at": null,
    "process_started_at": "2026-01-01T00:00:00+00:00",
    "measurement_started_at": "2026-01-01T00:00:00+00:00",
    "requests_passed_through": 0,
    "compression_success_rate": 0.0,
    "average_tokens_saved": 0.0,
    "outcome_counts": {
      "compressed": 0,
      "passed_through": 0,
      "fail_open": 0,
      "other": 0
    },
    "provider_counts": {
      "claude": 0,
      "copilot": 0,
      "auggie": 0,
      "deepseek": 0,
      "kimi": 0,
      "ollama": 0,
      "codex-direct": 0,
      "openai-pass-through": 0,
      "other": 0
    },
    "surface_counts": {
      "responses": 0,
      "anthropic_messages": 0,
      "other": 0
    },
    "timeout_seconds": 2.0,
    "model_limit": 200000,
    "last_success_at": null,
    "last_failure_at": null,
    "reset_reason": "process_start"
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
    "schema_version": 2,
    "enabled": true,
    "profile": "coding",
    "requests_seen": 0,
    "requests_compressed": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
    "compression_ratio": 0.0,
    "fail_open_count": 0,
    "failure_reasons": {
      "worker_busy": 0,
      "timeout": 0,
      "exception": 0,
      "inflation_guard": 0,
      "retrieval_marker": 0,
      "unsafe_output": 0,
      "other": 0
    },
    "error_types": {
      "timeout": 0,
      "worker_busy": 0,
      "dependency_exception": 0,
      "inflation_guard": 0,
      "retrieval_marker": 0,
      "unsafe_output": 0,
      "other": 0
    },
    "updated_at": null,
    "process_started_at": "2026-01-01T00:00:00+00:00",
    "measurement_started_at": "2026-01-01T00:00:00+00:00",
    "requests_passed_through": 0,
    "compression_success_rate": 0.0,
    "average_tokens_saved": 0.0,
    "outcome_counts": {
      "compressed": 0,
      "passed_through": 0,
      "fail_open": 0,
      "other": 0
    },
    "provider_counts": {
      "claude": 0,
      "copilot": 0,
      "auggie": 0,
      "deepseek": 0,
      "kimi": 0,
      "ollama": 0,
      "codex-direct": 0,
      "openai-pass-through": 0,
      "other": 0
    },
    "surface_counts": {
      "responses": 0,
      "anthropic_messages": 0,
      "other": 0
    },
    "timeout_seconds": 2.0,
    "model_limit": 200000,
    "last_success_at": null,
    "last_failure_at": null,
    "reset_reason": "process_start"
  }
}
```

Headroom metrics are aggregate-only and never include prompt text, input item
content, compressed text, response text, or per-request identifiers. They reset on
gateway process restart. `enabled` and `profile` reflect the current environment
configuration; setting `REVERSO_HEADROOM_ENABLED=0` disables compression and is
reported by this surface after restart.

The Headroom object is the additive schema-version-2 authority. All counters are
nonnegative integers. Ratios are finite numbers from zero through one.
`compression_ratio` is `tokens_saved / tokens_before`, or zero when
`tokens_before` is zero. `compression_success_rate` is
`requests_compressed / requests_seen`, or zero when `requests_seen` is zero.
`average_tokens_saved` is `tokens_saved / requests_compressed`, or zero when
`requests_compressed` is zero. `requests_passed_through` is
`max(requests_seen - requests_compressed - fail_open_count, 0)`.

The bounded map keys are:

- `outcome_counts`: `compressed`, `passed_through`, `fail_open`, `other`
- `failure_reasons`: `worker_busy`, `timeout`, `exception`,
  `inflation_guard`, `retrieval_marker`, `unsafe_output`, `other`
- `error_types`: `timeout`, `worker_busy`, `dependency_exception`,
  `inflation_guard`, `retrieval_marker`, `unsafe_output`, `other`
- `provider_counts`: `claude`, `copilot`, `auggie`, `deepseek`, `kimi`, `ollama`,
  `codex-direct`, `openai-pass-through`, `other`
- `surface_counts`: `responses`, `anthropic_messages`, `other`

Unknown dimensions accumulate only under `other`. Timestamp fields are RFC3339
UTC or null. `reset_reason` is `process_start`, except an explicit test reset
uses `manual_test_reset`. The embedded default profile is `coding`; an explicit
environment override remains visible on the route. Embedded metrics do not
invoke RTK, read standalone Headroom savings files, or persist across gateway
process restarts.

## Invariants

- The route reads only in-process latest-snapshot and aggregate-metrics stores.
- Empty store responses are still successful JSON responses with `rate_limits: null`.
- `GET /usage/headroom` is successful even before any compressed request.
- Usage routes must not spawn `codex`, Headroom, providers, or any subprocess.
- Token counts prefer Codex `turn.completed.usage` values when available.
- Missing rollout quota data keeps the prior `rate_limits` value instead of clearing it.
- `total_tokens` is `input_tokens + output_tokens + reasoning_output_tokens`.
