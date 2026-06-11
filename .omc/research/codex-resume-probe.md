---
title: "A1 Codex resume probe against wiped in-memory ResponseStore"
status: complete
phase: A
gate: A1
decision: NO-PERSIST
codex_version: codex-cli 0.139.0
gateway_bind: 127.0.0.1:64946
generated: 2026-06-10
---

# A1 Codex resume probe

## Decision

**NO-PERSIST.** `codex exec resume` after `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` succeeds for every provider against a freshly wiped in-memory `ResponseStore`. Persistence of `src/reverso/protocols/store.py` to disk is therefore NOT a requirement for `codex resume` to work; the in-memory boundary stays.

## Probe protocol

For each provider profile (`~/.codex/<profile>.config.toml` referencing the four `model_providers.reverso_*` entries in `~/.codex/config.toml`), the probe ran the following sequence inside an empty git scratch repo under `/tmp/reverso-a1-probe/<provider>/`:

1. `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -p <profile> "Remember the secret word <Word>. Reply only: ok."`
2. `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` then poll `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:64946/<provider>/v1/models` until HTTP 200.
3. `codex exec resume --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -c 'model_provider="reverso_<provider>"' -c 'model="<model>"' <session_id> "Reply only with the secret word, no extra text."`

The gateway kickstart wipes the in-memory `ResponseStore` because the process is the long-lived `reverso-proxy` launchd job; `launchctl kickstart -k` kills and respawns it.

The `-c model_provider=...` override on the resume call is the load-bearing detail: `codex exec resume` does NOT accept `-p/--profile` (see `codex exec resume --help`), so without the explicit overrides resume silently falls back to the default `openai` provider rather than the reverso gateway that ran turn 1. See "Why `-p` is not enough" below.

## Recorded turns

| Provider | Profile | Model id sent | Secret | Session id | Turn 1 response | Resume response after kickstart |
|----------|---------|---------------|--------|------------|-----------------|---------------------------------|
| claude | reverso_claude | gpt-5.5 (resolved via profile alias) | Aurora | 019eb366-406a-7510-a175-78a50f458111 | ok | Aurora |
| copilot | reverso_copilot | gpt-5.5 | Borealis | 019eb367-5b38-7bb3-be74-619609adbd6f | ok | Borealis |
| deepseek | reverso_deepseek | gpt-5.5 (resolved via profile alias) | Cyan | 019eb367-ca83-7463-ade1-5ed617a24941 | ok | Cyan |
| auggie | reverso_auggie | prism-a | Delta | 019eb368-3184-73f2-b1f8-d6f504bea1a7 | ok | Delta |

Each resume call followed a fresh `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` with a poll-to-ready loop. Every resume turn returned the secret correctly, against a `ResponseStore` that had been freshly constructed by the respawned gateway process.

## Why resume works without server-side state

`codex` sessions are persisted client-side as JSONL rollouts under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Inspecting any rollout (for example the turn 1 file `rollout-2026-06-10T23-18-03-019eb366-406a-7510-a175-78a50f458111.jsonl`) shows the recorded record types:

- `session_meta` (records `model_provider` for the original session, e.g. `reverso_claude`)
- `turn_context` (per-turn model, cwd, sandbox policy)
- `response_item` (messages, reasoning items with `encrypted_content`, function calls, function call outputs)
- `event_msg` (telemetry, token counts)

A whole-file substring scan of a representative rollout returned no occurrences of `previous_response_id` or `response_id` keys. The rollout never relies on a server-side response id chain; instead it carries the full reconstructed transcript that codex resends every time.

On `codex exec resume <session_id> <prompt>`, codex replays the rollout into a fresh in-process conversation and sends the assembled transcript on the next API call. The gateway's `ResponseStore` is consulted only for in-session `previous_response_id` chaining inside ONE codex run; a resume is a brand-new client run, so the wiped store is irrelevant.

## Why `-p` is not enough on resume

The first resume attempt for the claude profile (session `019eb366-406a-7510-a175-78a50f458111`) ran with just `codex exec resume <id> <prompt>` (no `-c` overrides). Codex printed `provider: openai` in the header, and the answer "Aurora" came from the default OpenAI provider replaying the user-supplied "Remember the secret word Aurora" message in the transcript, NOT from the reverso gateway at all. Adding `-p claude` is rejected with `error: unexpected argument '-p' found` (the `exec resume` subcommand intentionally does not layer profiles). The supported workaround is `-c model_provider="reverso_<provider>" -c model="<id>"`, which the probe used for every successful resume row above.

This is a Codex-side ergonomic constraint, not a reverso defect. It is captured here so the C1 E2E matrix script uses the same `-c` overrides instead of `-p` on the resume call.

## Implications

- **B3 (store persistence) does NOT execute.** A1 decision is NO-PERSIST; the in-memory boundary stays. B3 instead documents the boundary in the C2 parity doc.
- **C1 E2E matrix MUST use `-c model_provider` and `-c model` overrides on `codex exec resume`**, not `-p`.
- The `~/.codex/sessions/...` client-side transcript is the actual mechanism by which multi-turn memory and resume work; the gateway's role is per-call request handling.
- Reasoning items in the rollout carry an `encrypted_content` blob. For non-openai providers (the four reverso adapters), this blob is opaque server data the reverso adapters never decode; the transcript still works because the user/assistant message bodies remain plain text, and the secret word is in the user message.

## Decision line

A1=NO-PERSIST
