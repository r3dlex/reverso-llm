# Work Item: Copilot picker completeness (drop 11/30 → all 30)

- **Traceability node:** `issue:reverso-root:copilot-picker-completeness`
- **Spec:** [`docs/specifications/ACTIVE/copilot-picker-completeness.md`](../../docs/specifications/ACTIVE/copilot-picker-completeness.md)
- **State:** `shipped` (S1, S2, S3 all merged to main on 2026-07-05; PRs #75, #76, #77)
- **Owner:** unassigned
- **Spans repos:** `r3dlex/reverso-llm` (source + tests)
- **Surface scope:** standalone (reverso only)
- **Hosted reconciliation:** none required (no hosted tracker configured for `r3dlex/reverso-llm`; local-first per `init-ai-repo` host-policy).

## Summary

The Codex `/model` picker for the `reverso_copilot` provider drops 11 of 30 live Copilot models because the sync-time filter (`model_exposure.codex_responses_compatible_model_ids`) was written before commit `4507019` / ADR 0011 added Claude/Gemini serving on the Copilot **chat-completions** route. The 11 dropped models include `claude-fable-5` (the user's current-session model) and the 10 other chat-route IDs.

A second-order symptom: an OMX session in a different repo (`rib-workspace`) produced a manual `copilot.json` patch that leaked three Claude-surface model IDs (`claude-sonnet-5`, `claude-opus-4-8`, `claude-haiku-4-5-20251001`) into the Copilot catalog with the wrong slug format (no `copilot/` prefix). The IDs are real — they live on the `/claude` surface — but the OMX session had no surface-separation invariant to keep them apart.

## Decisions (northstar interview scope-confirmation)

- **Scope selected (default / Recommended):** source fix + live-pinning test + hand-patch doc. Surgical, three PRs.
- **Excluded (would have been Option 2 / 3):** function-name rename (`codex_responses_compatible_model_ids` → `codex_picker_visible_model_ids`); all-in-one mega-PR.

## Acceptance criteria (mirrored from the spec)

1. Copilot picker contains all live chat-route models with `copilot/` slug prefix.
2. `claude-fable-5` is selectable in the picker.
3. Two unit tests cover the broadened semantic + unsafe-id rejection.
4. A live-pinning integration test (gated) catches future drift.
5. The hand-patch antipattern is documented with the symptom and recovery.
6. No regression: 631/631 unit tests pass.

## Sliced goals

| Slice | Title | Type | Status |
|---|---|---|---|
| S1 | `fix(codex-sync): include copilot chat-route models in picker` | code | shipped (PR #75, commit 154ee215) |
| S2 | `test(codex-sync): live-pinning test for copilot picker completeness` | test | shipped (PR #76, commit 9f0af4e8) |
| S3 | `docs: surface-separation invariant for per-provider catalogs` | docs | shipped (PR #77, commit 410eb260) |

## Open questions

- None blocking. S1 is unblocked. S2 + S3 are follow-ups that can land in any order.

## Post-mortem hooks (for the diagnose skill)

- **What would have prevented this?** An integration test that pins the picker against the live `/copilot/v1/models` (covered by S2).
- **Architectural hand-off:** function name `codex_responses_compatible_model_ids` is now misleading; rename to `codex_picker_visible_model_ids` is the right follow-up but was explicitly excluded from this scope.

## Shipped via

- PR #75 -- S1 source fix (squash merge, commit 154ee215)
- PR #76 -- S2 live-pinning integration test (squash merge, commit 9f0af4e8)
- PR #77 -- S3 surface-separation doc (squash merge, commit 410eb260)

All three ran with peer-review (architect + code-reviewer lanes), 631/631
unit tests green at every gate, the live-pinning test verified 2/2 pass
against the live reverso gateway, and all 5 GitHub Actions checks
(prek, prek self-hosted, test 3.12, test gate, test self-hosted) were
green on main.
