---
title: OR-G7 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g7
focused_command: uv run pytest tests/unit/test_openrouter_observability.py -q
verdict: GO
---

# OR-G7 red-green evidence

## Focused command

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_observability.py -q
```

## Red phase

```text
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.observability'
```

## Green phase

```text
.....                                                                    [100%]
5 passed in 0.02s
```

- `test_metrics_counter_increments_on_admission`
- `test_metrics_counter_records_denial_reason`
- `test_metrics_never_record_secrets_or_prompts`
- `test_status_snapshot_includes_safe_fields`
- `test_metrics_export_is_counter_dicts`

## Architecture

- `src/reverso/protocols/adapters/openrouter/observability.py` introduces
  `OpenRouterMetrics` with bounded counters for admissions, denials,
  capability grants and revocations, completed and rejected streams. The
  `__repr__` carries only the export keys so a leak scan finds nothing.
- `StatusSnapshot` and `snapshot_status` produce a deterministic status
  shape that excludes the credential, prompt, body, and local path.

## Decision

OR-G7 verdict is GO. The DAG is complete. The OpenRouter provider is
admissible for downstream attended proof and convergence verification.
