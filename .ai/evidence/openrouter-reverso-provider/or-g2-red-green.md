---
title: OR-G2 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g2
focused_command: uv run pytest tests/unit/test_openrouter_responses.py tests/integration/test_openrouter_responses_unary.py -q
verdict: GO
---

# OR-G2 red-green evidence

## Focused command

```bash
uv run pytest tests/unit/test_openrouter_responses.py tests/integration/test_openrouter_responses_unary.py -q
```

Equivalent in the worktree:

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_responses.py -q
```

## Red phase

The collection error before the transport module existed:

```text
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.transport'
```

## Green phase

```text
.......                                                                  [100%]
7 passed in 0.02s
```

Tests cover:

- `test_create_response_omits_previous_response_id_and_store`
- `test_create_response_uses_bearer_authorization`
- `test_create_response_reconciles_actual_cost`
- `test_create_response_does_not_call_store_when_upstream_fails`
- `test_create_response_carries_instructions_and_tools`
- `test_create_response_does_not_leak_extra_provider_overrides`
- `test_list_models_returns_openrouter_model_list`

## Architecture

- `src/reverso/protocols/adapters/openrouter/transport.py` strips
  `previous_response_id` and `store` before dispatch and forwards
  `Authorization: Bearer` and `X-OpenRouter-Title: Reverso` headers.
- `src/reverso/protocols/adapters/openrouter/adapter.py` keeps the
  translate→envelope pipeline, attaches an optional local store, and refuses
  to invoke `get_response`/`list_input_items` without one.
- `src/reverso/proxy/compose.py` mounts the
  `HttpOpenRouterTransport` and reuses it as the adapter's credentials.

## Decision

OR-G2 verdict is GO. The native unary Responses tracer bullet is admit-gated
through the OR-G1 runtime and persists locally without leaking upstream cost.
OR-G3 (Responses streaming and stateless continuation) may proceed.
