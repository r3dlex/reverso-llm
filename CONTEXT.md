---
title: Reverso domain glossary
description: Names for the load bearing concepts in the Reverso gateway so code, docs, and reviews use one vocabulary.
status: active
---

# Reverso domain glossary

Terms used by the gateway code and its documentation. Architecture decisions
live in `docs/architecture/adr/`; this file only names concepts.

## Provider adapter

A concrete implementation of the frozen ProviderAdapter Protocol
(`src/reverso/protocols/adapter.py`, ADR 0002 11.3). Adapters own provider
specific transport and credential handling behind the five method surface:
`create_response`, `stream_response`, `list_models`, `get_response`,
`list_input_items`.

## Buffered turn

A provider interaction whose complete assistant output exists BEFORE any SSE
event is emitted to the client. Claude, Auggie, and DeepSeek produce buffered
turns (one CLI run or one upstream HTTP call per turn). Copilot does not: it
forwards upstream SSE blocks as they arrive.

## Turn replay

Re-emitting a buffered turn as the canonical Responses SSE sequence. Owned by
`src/reverso/protocols/replay.py` (`replay_turn`). The canonical sequence is
the nine events from `response.created` through `response.completed` listed in
`CANONICAL_EVENT_SEQUENCE`.

## Incremental replay

Streaming a turn as deltas arrive instead of replaying a buffered turn. Owned
by `src/reverso/protocols/replay.py` (`replay_incremental`, ADR 0004): the
adapter contributes only a chunk iterator and a finalize callable; the helper
owns canonical event emission and the finalize-time store write. Store before
drain is RELAXED on this path (write at finalize, before
`response.completed`). DeepSeek and Claude stream through this seam.

## Store before drain

The invariant that a buffered turn's response envelope is stored BEFORE the
first replay event is yielded, so a client disconnect mid stream never loses
the response for `previous_response_id` chaining or `get_response` and
`input_items` lookups. Enforced once, inside `replay_turn`.

## Response store

The in memory, per adapter store (`src/reverso/protocols/store.py`) keyed by
response id. In memory only is an explicit ADR 0002 milestone decision.

## Bounded CLI spine

The single module (`src/reverso/protocols/adapters/cli_spine.py`, ADR 0005)
that runs a CLI backed provider turn as a subprocess, one-shot
(`run_bounded_cli`) or streaming line by line (`stream_bounded_cli`): wall
clock bound (default 300s), stderr redacted before logging, the nonzero-exit
cause suppressed so raw stderr never rides a traceback, and (streaming only)
the child killed when the consumer abandons the iterator so a client
disconnect never leaks a running CLI. CLI backed provider adapters (claude,
auggie) contribute only argv and stdout parsing.

## Model selector

A Codex visible model name used by the local Codex picker and catalog. The
Model selector Module owns the selector/catalog Interface for Codex visible
names. Its Implementation keeps built in Codex GPT selectors bare, adds Reverso
provider selectors without replacing built in names, and prefixes only collision
prone provider families such as `copilot/`. This deepens the Seam across Codex
sync, routing, and surface registry code while improving Leverage and Locality.
