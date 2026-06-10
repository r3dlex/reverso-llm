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

## Store before drain

The invariant that a buffered turn's response envelope is stored BEFORE the
first replay event is yielded, so a client disconnect mid stream never loses
the response for `previous_response_id` chaining or `get_response` and
`input_items` lookups. Enforced once, inside `replay_turn`.

## Response store

The in memory, per adapter store (`src/reverso/protocols/store.py`) keyed by
response id. In memory only is an explicit ADR 0002 milestone decision.
