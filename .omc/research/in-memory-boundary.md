---
title: "B3 in-memory ResponseStore boundary documentation"
status: complete
phase: B
gate: B3
decision: NO-PERSIST
sourced_from: .omc/research/codex-resume-probe.md
generated: 2026-06-10
---

# B3 in-memory ResponseStore boundary

## Why this document exists

A1 (see `.omc/research/codex-resume-probe.md`) recorded **A1=NO-PERSIST**: `codex exec resume` succeeds for every reverso provider after a `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` that wipes the gateway's in-memory `ResponseStore`. B3's plan branch therefore activates the documentation arm, not the disk-backed store arm. No code in `src/reverso/protocols/store.py` changes under this branch. This file captures the boundary so the C2 parity matrix doc can fold it in and so future maintainers do not relitigate the persistence question.

## What the ResponseStore actually holds

`src/reverso/protocols/store.py` defines `ResponseStore` as a thread-safe in-memory map. The current surface is:

* `_responses: dict[str, ResponseEnvelope]` keyed by `response_id`, populated by `put_response` from every reverso adapter on a successful turn.
* `_input_items: dict[str, list[dict]]` keyed by the same `response_id`, populated alongside the envelope when the adapter records the request's input items via `record_input_items`.
* A single `threading.Lock` guarding both dicts.
* `get_response(response_id)`, `get_input_items(response_id)`, and `clear()` accessors. `clear()` is used by tests; production code never calls it.

Each reverso adapter (`adapters/claude.py`, `adapters/copilot.py`, `adapters/auggie.py`, `adapters/deepseek.py`) constructs its own `ResponseStore` instance unless one is injected. The store is therefore per-adapter, per-process, and per-gateway-lifetime. There is no cross-process sharing, no on-disk mirror, and no TTL or size cap because the entries die with the process.

## What is lost on gateway restart

A `launchctl kickstart -k` or any other process restart drops the entire in-memory map. Concretely, after a restart the following Responses-API affordances stop working for response ids that were stored by the prior process:

1. `previous_response_id` chaining inside a single non-codex client run. If a Responses-API client recorded a `response_id` from turn 1 and then submitted turn 2 with `previous_response_id` pointing at it across the restart, the adapter's `get_response` lookup returns `None` and the chain breaks. The client must replay the conversation in full input items, the same way codex already does.
2. `/v1/responses/{id}/input_items` lookups for prior ids. The endpoint goes through `adapter.get_input_items(response_id)`, which consults `_input_items` and returns `None` for any id the new process never observed. Callers that depend on `input_items` retrieval to reconstruct prior turns must record the items client-side themselves.
3. `/v1/responses/{id}` envelope retrieval for prior ids. The Responses app exposes `await adapter.get_response(response_id)` for direct fetches; this is similarly cleared on restart.

The store is the only authoritative server-side record of these mappings. Nothing else in the gateway persists `response_id` to `input_items` or `response_id` to `ResponseEnvelope` relationships.

## Why codex resume still works

The probe in `.omc/research/codex-resume-probe.md` walked four reverso provider profiles through a kickstart-then-resume sequence and every resume returned the correct secret word. The mechanism is entirely client-side:

* Codex persists each session as a JSONL rollout under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The rollout carries `session_meta`, `turn_context`, `response_item` (user and assistant messages, reasoning items with opaque `encrypted_content`, function calls, function call outputs), and `event_msg` records.
* A full-file substring scan of a representative rollout returned zero occurrences of `previous_response_id` or `response_id`. Codex's rollout format does not encode a server-side response id chain.
* On `codex exec resume <session_id> <prompt>`, codex replays the rollout into a fresh in-process conversation and submits the assembled transcript on the next API call. The gateway sees a brand new request whose input items contain the full prior conversation. The wiped `ResponseStore` is irrelevant because codex never asks for a `previous_response_id` lookup on the resume request.

Codex's `ResponseStore` consultation is limited to in-session `previous_response_id` chaining that may occur within ONE codex run. A resume is a different client run with a fresh in-process state, so it never reaches into the prior process's store.

There is also a Codex-side ergonomic constraint captured by A1: `codex exec resume` does not accept `-p`. The supported invocation is `-c model_provider="reverso_<provider>" -c model="<id>"`. This is unrelated to the persistence question but is the load-bearing detail that makes the resume probe reach the reverso gateway at all. It binds B5 and C1 to use `-c` overrides on the resume path.

## Implication for non-codex Responses-API clients

`previous_response_id` chains do NOT survive a gateway restart for clients that rely on the server-side store. This is the boundary every non-codex client must respect:

* Treat `response_id` issued by the reverso gateway as valid only for the lifetime of the gateway process that issued it. There is no contract that a `response_id` from before a restart can be referenced after.
* Clients that need durable multi-turn memory across gateway restarts must persist the conversation client-side, the way codex does with rollouts, and resend the assembled input items each turn rather than passing `previous_response_id`.
* The gateway does not advertise a restart event or a process generation token on `/v1/models` or elsewhere. A client cannot detect a restart from the response; it can only observe a failed `previous_response_id` lookup and fall back to a full replay.

This is the exact contract the codex integration already satisfies, which is why A1 returned NO-PERSIST. The same contract is now the documented expectation for every other Responses-API surface the gateway serves.

## What this branch does not change

* `src/reverso/protocols/store.py` keeps the in-memory implementation. No `Path`, `os.replace`, `~/.local/state/reverso`, TTL field, or size cap is added.
* `src/reverso/protocols/adapter.py` is the frozen ProviderAdapter Protocol and is not touched.
* No new tests under `tests/unit/protocols/` for restart survival, TTL, atomic write, or directory mode are added, because there is no disk surface to assert against.
* No new dependency lands in `pyproject.toml`.

The C2 parity matrix doc folds this section's "what is lost on restart" list and "implication for non-codex clients" paragraph into the cross-provider parity matrix as a single shared row that applies to all four reverso adapters.

## Decision line

B3=NO-PERSIST, documented.
