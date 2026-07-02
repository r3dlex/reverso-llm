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

## Headroom compression seam

The Reverso-owned gateway boundary that applies Headroom context compression
before ProviderAdapter dispatch: after raw feature gating and normalization on
the Responses surface, and after capability gating and translation on the
Anthropic Messages surface.
It is enabled by default when Reverso is installed with the base runtime
dependency `headroom-ai`, uses Headroom `agent-90` as the default configurable
profile, runs stateless by
default, keeps a documented kill switch, preserves request structure that is not
plain text, and fails open to the original request when compression cannot be
applied safely.
Public input item retrieval remains client-facing and returns the original client
input, not the compressed provider-dispatch form.

## Headroom usage metrics surface

The loopback-only `GET /usage/headroom` Reverso HTTP endpoint that reports
in-memory aggregate-only Headroom token savings and compression health without
exposing raw prompt content. It is the canonical metrics surface for the
Headroom compression seam, not part of provider response translation. By default
the seam applies to all routed backends reached through Reverso-owned inbound
surfaces. Metrics reset on gateway restart. Existing `GET /usage` includes an
additive top-level `headroom` summary block for discoverability, but normal
provider responses stay unchanged.

## Model selector

A Codex visible model name used by the local Codex picker and catalog. The
Model selector Module owns the selector/catalog Interface for Codex visible
names. Its Implementation keeps built in Codex GPT selectors bare, adds Reverso
provider selectors without replacing built in names, and prefixes only collision
prone provider families such as `copilot/`. This deepens the Seam across Codex
sync, routing, and surface registry code while improving Leverage and Locality.

## Profile sync Seam

The Interface where Model selector policy crosses into Codex config files.
`src/reverso/protocols/model_exposure.py` owns which provider prefixes are
Reverso-routed, which provider profiles are direct Codex profiles, how provider
catalog slugs are exposed, and which generated variant profile names are safe to
archive. `src/reverso/codex_sync.py` is the file-writing Adapter at this Seam:
it fetches live models, writes profile files and catalogs, and strips old base
config clutter, but it does not own provider-prefix semantics. Runtime request
routing remains with `surface_registry`; profile sync only prepares Codex's
local selector files.
