---
title: OR-G5 + OR-G6 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g5-or-g6
verdict: GO
---

# OR-G5 + OR-G6 red-green evidence

## Focused commands

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_messages_stream.py -q
.venv/bin/python -m pytest tests/unit/test_openrouter_client_sync.py -q
```

## Red phase

```text
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.messages_stream'
ModuleNotFoundError: No module named 'reverso.protocols.adapters.openrouter.client_sync'
```

## Green phase

```text
.........                                                                [100%]
9 passed in 0.03s
```

- `test_build_stream_payload_forwards_stream_true`
- `test_normalize_stream_event_extracts_event_type`
- `test_normalize_stream_event_passes_through_unknown_types`
- `test_normalize_stream_event_rejects_non_dict_inputs`
- `test_encode_decode_claude_alias_round_trip`
- `test_build_claude_launcher_uses_exact_alias_grammar`
- `test_build_codex_profile_uses_exact_provider`
- `test_claude_alias_preserves_provider_qualified_identity`
- `test_claude_alias_rejects_lowercased_reconstruction`

## Architecture

- `src/reverso/protocols/adapters/openrouter/messages_stream.py` adds the
  OpenRouter Messages streaming client that drops ``ping`` keepalives and
  validates each event payload before yielding.
- `src/reverso/protocols/adapters/openrouter/client_sync.py` builds the
  Codex profile (`reverso-openrouter`, `openrouter/<author>/<model>`) and the
  Claude launcher with the exact
  ``anthropic-openrouter-{base64url(author/model)}`` alias. Neither artifact
  carries the key.

## Decision

OR-G5 and OR-G6 verdicts are GO. OR-G7 (observability and attended proof)
may proceed.
