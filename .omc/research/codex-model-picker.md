---
title: "A2 Codex /model picker mechanism research"
status: complete
phase: A
gate: A2
decision: SYNC-TOOL
codex_version: codex-cli 0.139.0
generated: 2026-06-10
---

# A2 Codex /model picker mechanism

## Decision

**SYNC-TOOL.** Codex 0.139.0 has no native mechanism that lets a custom `model_provider` feed the TUI `/model` picker from a `/v1/models` endpoint. Live per-provider model listings must be brought into Codex's view by a separate `reverso-codex-sync` console script (B5) that idempotently writes per-model profile entries into `~/.codex/config.toml` and per-model `~/.codex/<provider>-<model>.config.toml` overlays.

## What Codex actually supports

`codex --help`, `codex exec --help`, and `codex exec resume --help` (all captured against codex-cli 0.139.0) document exactly three places where a model id reaches Codex:

1. `-m, --model <MODEL>` flag on `codex`, `codex exec`, `codex exec resume`. The string is sent verbatim to the resolved provider. The reverso gateway then resolves GPT-level profile aliases (`gpt-5.5`) via `resolve_profile_model` per provider, or accepts real provider ids unchanged.
2. `-c model="<id>"` config override (same path).
3. `~/.codex/<profile>.config.toml` `model = "<id>"` key, layered onto the base `~/.codex/config.toml` via `-p <profile>`.

There is NO `--list-models`, no `models discover` subcommand, and no automatic refresh from a `model_provider.base_url + "/models"` endpoint. The reverso gateway DOES serve `/v1/models` per provider (verified live: `curl http://127.0.0.1:64946/claude/v1/models` returns claude ids; same for copilot/auggie/deepseek). Codex 0.139.0 does not consult it.

The TUI `/model` picker is fed by the top-level `[tui.model_availability_nux]` table in `~/.codex/config.toml`, which is a static, hand-curated list (current value: `"gpt-5.5" = 4`). Adding new ids to that list requires a TOML edit. No code in Codex 0.139.0 calls into a `model_provider`'s base url for the picker.

## Why NATIVE was eliminated

The plan permits a NATIVE outcome if Codex supports a `/v1/models` poll or analogous mechanism for custom providers. Three signals together rule that out:

1. `codex doctor` (0.139.0) does not enumerate provider models. Its output for the reverso scratch run reports only environment and install state; there is no `models` section per provider.
2. `~/.codex/config.toml` profile-style overlays (`~/.codex/claude.config.toml` etc., the canonical reverso integration shape) hardcode `model = "<id>"`. There is no `model_source = "endpoint"` or `model_discovery = ...` knob in `codex --help` or in the live config.
3. `codex resume` and `codex exec resume` accept `-m <model>`. If the picker truly used `/v1/models`, the resume help would document a refresh trigger; it documents only the explicit `-m` override.

## Why DOCUMENT-ONLY was eliminated

DOCUMENT-ONLY would leave the user reading per-provider model lists by hand and typing `-m <id>` or hand-editing TOMLs. The user explicitly chose the full integration bar in deep interview Round 2 ("live per-provider models become selectable from Codex"). A pure docs page does not meet that bar.

The fallthrough in the plan is therefore SYNC-TOOL, which is what A2 records.

## What SYNC-TOOL must do (handoff to B5)

The B5 lane (blocked by A2) builds `reverso-codex-sync` per the plan's discipline:

- New console script entry in `pyproject.toml` (`reverso-codex-sync = "reverso.codex_sync:main"`).
- New module `src/reverso/codex_sync.py`.
- For each of the four reverso prefixes, GET `http://127.0.0.1:64946/<prefix>/v1/models` and merge each returned id as a per-model profile under `~/.codex/<prefix>-<id>.config.toml` (the established naming pattern: see existing `~/.codex/deepseek-spark.config.toml`, `~/.codex/minimax-mini.config.toml`).
- Optionally update the top-level `[tui.model_availability_nux]` block in `~/.codex/config.toml` to expose the ids in the TUI picker, with idempotent merging that never deletes user entries.
- Write discipline (plan-mandated): UTC-timestamped backup before write, rotation keeping the 5 newest backups, compose the full new TOML in a temp file IN THE SAME DIRECTORY as the target and `os.replace` into place (atomic on the same filesystem), never touch unrelated keys, no secrets ever written.
- Unit tests (against fixture model payloads): TOML rewrite idempotency, backup creation, unrelated-key preservation, atomic-write behavior.

## Implications

- B5 builds the sync tool (not the docs-only fallback).
- B5 work is unblocked by A2 alone; it does NOT depend on B1.
- C1 E2E matrix tests B5 by running `reverso-codex-sync` once and then verifying that a per-model profile (e.g. `~/.codex/claude-claude-sonnet-4-6.config.toml`) is present, with no unrelated key corruption.
- The C2 parity doc records the limitation that the picker itself remains static in Codex 0.139.0; the sync tool is the workaround the project ships.

## Decision line

A2=SYNC-TOOL
