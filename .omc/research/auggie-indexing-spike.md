---
type: research
project: reverso
slug: auggie-indexing-spike
status: complete
goal: G002-auggie-sdk-and-indexing-feasibility
date: 2026-06-09
related:
  - .omc/plans/ralplan-auggie-deepseek-responses.md
  - .omc/plans/test-spec-auggie-deepseek-responses.md
  - docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md
  - .omc/research/auggie-sdk-spike.md
---

# Auggie Indexing Controls Spike

## Purpose

Record the exact indexing controls Auggie exposes, each with an observed value, so the plan's
falsifiable rule can be applied: a global hard-disable claim is valid only if a control that
hard-disables indexing is shown present; otherwise the metadata and docs carry the literal
string `hard-disable unproven` and the default workspace is no-workspace/sandbox, never the
caller workspace.

Method: read-only inspection of `auggie --help`, the bundled `augment.mjs`
(`/opt/homebrew/lib/node_modules/@augmentcode/auggie/augment.mjs`), and the live settings file
`~/.augment/settings.json`. No agent was invoked, so no workspace was indexed by this spike.

## Controls inspected, with observed values

| Control | Kind | Observed value / behavior | Hard-disable? |
|---|---|---|---|
| `--allow-indexing` | CLI flag | "Skip the indexing confirmation screen in interactive mode." Proceeds WITH indexing; it is not a disable. | No |
| `--wait-for-indexing` | CLI flag | "Wait for workspace indexing to complete before codebase retrieval executes." Not a disable. | No |
| `-p/--print` | CLI flag | "the indexing confirmation prompt is skipped in print mode. Only use --print from a workspace root that you want to index." Print mode auto-indexes the workspace root. | No (forces indexing) |
| `-w/--workspace-root <path>` | CLI flag | "Workspace root (auto-detects git root if absent)." Selects WHAT gets indexed. Pointing it at an empty sandbox confines indexing to that sandbox. | Partial (scopes target) |
| `--add-workspace <path>` | CLI flag | "Additional workspace directory to index (repeatable)." Adds, never removes. | No |
| `indexingDenyDirs` | settings.json key (user-settable) | Array of directories denied from indexing. In `augment.mjs` the resolver `r2(indexingAllowDirs, indexingDenyDirs, path)` can return `"never"`, and `o.indexingEnabled===false` then emits `Warning: Indexing disabled. Your working director...`. Listed in the user-settable settings allow-set `Set(["removedTools","indexingAllowDirs","indexingDenyDirs"])`. | Per-directory yes |
| `indexingAllowDirs` | settings.json key (user-settable) | Array of allow-listed directories. Code comment: "to revoke indexing permission, see the `indexingAllowDirs` field in the Augment settings file." | Per-directory scoping |
| `indexingEnabled` | internal runtime state (NOT user-settable) | Derived from workspace presence and codebase-retrieval support: set false when `workspaceManager` is absent (no workspace) and when a denied path resolves to `"never"`. Not in the user-settable settings allow-set; there is no documented flag or settings boolean to force it false globally. | Not a user control |

Observed live datapoint: `~/.augment/settings.json` `indexingDenyDirs` already contains
`/Users/andreburgstahler/Ws/Personal`, which is the parent of this repository
(`/Users/andreburgstahler/Ws/Personal/AiTool/reverso`). So this workspace currently resolves
to denied indexing under the user's own settings.

## Verdict: hard-disable unproven (global), per-directory deny proven

- There is no global, per-invocation hard-disable flag (no `--no-index` / `--disable-indexing`
  / settings boolean). `--allow-indexing` proceeds with indexing; `--print` forces it.
- A per-directory hard-deny IS provable: a path under `indexingDenyDirs` resolves to `"never"`
  and the CLI reports `indexingEnabled=false` with a `Warning: Indexing disabled` message.
- `indexingEnabled` is an internal, state-derived flag, not a user-settable kill switch.

Because no global per-invocation kill switch is exposed, the plan's literal applies: Auggie
capability metadata (via `list_models`/`models_with_codex_refresh`) and docs MUST carry the
literal string `hard-disable unproven`. The weaker word `disabled` must not be substituted
(test asserts the literal and FAILS on `disabled`).

## Decision: default workspace = no-workspace / sandbox, never the caller workspace

Reverso defaults the Auggie invocation to a sandbox workspace root, never the caller's real
workspace, because:

- `--print` auto-indexes whatever `--workspace-root` resolves to, and there is no per-call
  global disable. The only portable, per-invocation suppression is to make that root an empty
  sandbox so no real code is indexed.
- `indexingEnabled` resolves false when there is no real workspace manager, which is the
  closest observable "indexing off" state available without mutating the user's global
  settings.
- Mutating the user's `~/.augment/settings.json` `indexingDenyDirs` as a side effect is
  rejected as the default: it is global, persistent, and outside Reverso's process boundary.
  The sandbox-workspace approach is per-invocation and leaves user settings untouched.

Best-effort suppression layering for the adapter default:

1. Run with `--workspace-root <ephemeral sandbox dir>` (empty, non-git), never the caller path.
2. Prefer `-a/--ask` posture where execution is not desired (confirmed against the
   no-hidden-execution test in G004).
3. Document the `hard-disable unproven` caveat in `/models` metadata and docs.

This satisfies "never the caller workspace" and gives the strongest indexing suppression that
the CLI actually supports per invocation.

## Falsifiability hooks for later phases

- A test asserts the literal `hard-disable unproven` appears in Auggie capability metadata and
  docs, and FAILS if `disabled` is used instead (test-spec architectural contract).
- A test asserts the default Auggie workspace root is a sandbox/no-workspace path, never the
  caller-provided workspace.
- "Proof unavailable" for a global hard-disable is valid precisely because this artifact shows
  the global control is absent while documenting the per-directory deny and sandbox controls
  that are present.
