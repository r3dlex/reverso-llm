---
type: spec
project: reverso
slug: opencode-go-provider
status: active
date: 2026-08-11
---

# OpenCode Go subscription as a Reverso provider

## A → B

**A (now):** The OpenCode Go subscription is reachable only through `ocgo`, a
standalone Go CLI that runs its own proxy, its own credential store
(`~/.config/ocgo/config.json`), its own model catalog and its own launchers for
Claude Code and Codex. None of it is visible to Reverso: no Headroom compression,
no `/usage` accounting, no profile sync, no fail-closed merge with the rest of the
provider set.

**B (target):** OpenCode Go is a first-class Reverso backend named `opencode`,
serving Codex through the Responses gateway and Claude Code through the Anthropic
Messages surface, with credentials in the Keychain, Headroom applied before
dispatch, and every one of its 29 catalog models routable without colliding with
the DeepSeek, Kimi or Codex taxonomies.

## Verified upstream contract

Probed live 2026-08-11, not taken from documentation:

| Endpoint | Auth | Observed |
|---|---|---|
| `GET https://opencode.ai/zen/go/v1/models` | none | `200`, 29 model ids |
| `POST .../v1/messages` | `X-API-Key` + `Anthropic-Version: 2023-06-01` | `401` unauthenticated |
| `POST .../v1/chat/completions` | `Authorization: Bearer` | key required |

Auth is a **static subscription key** (`sk-opencode-...`) - no OAuth, no refresh,
materially simpler than the Copilot, Codex and Kimi paths.

Two corrections to `ocgo`'s README, both taken from its source:
1. A **native Anthropic Messages endpoint exists** (`main.go:36`, `:1090`); the
   README claims everything forwards to `/chat/completions`.
2. `/models` requires **no credential**, so catalog discovery is free.

## Decision: adapt, do not run, ocgo

`ocgo` (one 3,326-line `main.go`) is a client-side launcher plus translating
proxy - the same role Reverso already owns. Running it as a sidecar would add a
second hop with two translation layers, a second credential store, a second
catalog, a fourth supervised process, and a Go toolchain in a Python repo.

It is instead treated as a **contract oracle**. Ported knowledge:

- **Per-model protocol split** (`UsesAnthropicEndpoint`, `main.go:364-390`):
  `minimax-m3`, `minimax-m2.7`, `minimax-m2.5`, `qwen3.7-max` require
  `/messages`; the rest use `/chat/completions`. This table is *hand-maintained
  and covers only 16 of the 29 live ids*, so it is a starting hypothesis to be
  replaced by measurement (G3), not a fact to be copied. **Measured in G3 and
  refuted: see "G3 measurement" below.**
- **Strict-upstream normalization** (`normalizeAnthropicRequestForUpstream`,
  `main.go:1106`): OpenCode's Anthropic endpoint is *stricter than Anthropic's*
  and rejects `thinking`, `reasoning`, `reasoning_effort`, `effort`, `level`,
  `depth`, `output_config` unless stripped; `system` needs normalizing.
- Image-modality validation, oversized tool-result truncation (qwen), strict
  Anthropic web-tool normalization.

**Not ported:** `fallbackModelIDs` (`main.go:297`) is already stale - 29 models
are live and 13 are absent from it, including `kimi-k3`.

## The routing problem this spec must solve

Reverso resolves a bare model id to a backend through one authority,
`surface_registry.resolve_anthropic_backend`, backed by a flat
`{bare_id: backend}` index. OpenCode's catalog **overlaps three existing
backends**:

| OpenCode id | Already claimed by |
|---|---|
| `deepseek-v4-pro`, `deepseek-v4-flash` | DeepSeek (`litellm_config.yaml:72,79`) |
| `kimi-k3` | Kimi OAuth (`_KIMI_MODELS`) |
| `gpt-5.6-luna`, `grok-4.5` | to be checked against Codex/OpenAI family heuristics |

ADR 0008 defines exactly two backend kinds and `_resolve_qualified` implements
them literally:

- **rowless** (`copilot`, `auggie`) - prefix authoritative for *any* bare id,
  including one indexed elsewhere (`copilot/gpt-5.5`);
- **rows-owning** (`codex`, `deepseek`, `claude`, `kimi`) - the bare id **must be
  indexed to itself**; `deepseek/gpt-5.5` fails closed.

Membership is *derived*, not declared:
`_BACKENDS_WITH_ROWS = frozenset(_MODEL_INDEX.values())`. So the two halves of the
desired behaviour are mutually exclusive as implemented:

- seed the 29 ids to gain **bare** routing → `opencode` becomes rows-owning →
  `opencode/kimi-k3` **fails closed**, making the colliding models unreachable
  even when qualified;
- do not seed → rowless → all 29 reachable qualified, but **no bare routing**.

Seeding is additionally unsafe today: `_build_model_index` performs
`index[key] = backend` with **no conflict detection - last writer wins,
silently**. Kimi is seeded last, so `kimi-k3 → kimi`; seeding `opencode`
afterwards would silently move bare `kimi-k3` to a different subscription,
credential and bill with no observable change to the request.

**Resolution (locked):** OpenCode is a genuinely third kind - a *discoverable*
taxonomy (unlike copilot/auggie) that *overlaps* other backends (unlike
deepseek/codex). A new ADR introduces the **catalog-owning backend**: its prefix
is authoritative for any id in its discovered catalog, and it receives bare
routing only for ids unique to it. Incumbency always wins; OpenCode never takes a
bare id already indexed to another backend.

## Locked decisions

| # | Decision |
|---|---|
| D1 | Native Reverso adapter, prefix `opencode`. No `ocgo` process at runtime. |
| D2 | Subscription key is available; live verification is in scope for every slice that needs it. |
| D3 | Qualified always **and** bare where unique, via a new catalog-owning backend kind (ADR). |
| D4 | Expose all 29 ids from `/models`; no curation. |
| D5 | Codex routes through Reverso (not left on ocgo). |
| D6 | Headroom compression is inherited at the default - no per-provider exception. |
| D7 | Quota/429 **fails closed** and surfaces the error; never falls back to another backend, because that would silently change provider, credential and billing. |
| D8 | The bare-exposure set is a **committed artifact** regenerated by `catalog_refresh` and verified by a fail-closed `--check`, so a change in what is reachable bare is a reviewable diff. |
| D9 | Collision transfer fails closed: a newly detected collision errors rather than resolving itself. |

## Risks

- **Double translation on the Anthropic surface.** `anthropic_app` converts
  inbound Anthropic → `ResponsesRequest` → adapter, so an Anthropic-native
  upstream round-trips Anthropic → Responses → Anthropic. Mitigating fact: ocgo
  already strips exactly the fields that would be lost, because the upstream
  rejects them. Codex is landed first (D5, G4) precisely so that any fidelity
  problem later observed on Claude Code is attributable to this round-trip and
  not to the adapter. No passthrough seam is built speculatively.
- **Per-model context windows.** 29 models spanning 128k to 512k+; the Claude Code
  launcher sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS` and the auto-compact window per
  provider (`KIMI_CONTEXT_WINDOW` precedent). Windows come from
  `models.dev/api.json` with a bounded default.
- **Picker mechanics.** Claude Code's gateway discovery ignores `/v1/models` ids
  not beginning `claude`/`anthropic`, so models surface as
  `anthropic-opencode-<id>` aliases (ADR 0010). Rowless-style discovery uses
  curated tuples (`_DISCOVERY_ROWLESS_MODELS`, 5 ids today) and must become
  catalog-driven for 29 dynamic ids.
- **Quota telemetry.** OpenCode documents no quota headers, so `/usage` has
  nothing authoritative to report for this provider. Fail-closed only surfaces
  exhaustion at the moment of failure.
- **Blast radius of D3.** `_resolve_qualified` and `_MODEL_INDEX` are shared by
  every backend. G1 (conflict detection) lands before any semantics change, and
  G2 proves the new category against a synthetic fixture backend before a real
  adapter exists.

## Risk closed by inspection

`count_tokens` was a live concern: `ocgo` returns `0`, which would break Claude
Code's context tracking (no auto-compact, then overflow). It cannot bite here  - 
Reverso answers `POST /v1/messages/count_tokens` itself with a documented
word-count approximation and never delegates it upstream.

## Verification

1. `/models` - public; already verified (29 ids).
2. **Measured protocol split**: DONE, see "G3 measurement" below. The split does
   not exist; the hypothesis was refuted rather than refined.
3. **Strict-normalization proof**: send `thinking`/`output_config` to `/messages`
   and confirm the documented rejection, so each strip has a test proving it is
   required rather than assumed.
4. End-to-end: one tool-heavy Codex turn, then one Claude Code turn, comparing
   tool_use fidelity to quantify the double-translation cost.

## G3 measurement (2026-08-22)

Every catalog id was sent a bounded request on *both* upstreams. The first pass
was invalid and is recorded here because its failure mode is the instructive
part: sending `content` as a plain string made 15 ids return `400`, which reads
exactly like "this model rejects the Anthropic endpoint". The upstream errors
gave it away (`messages must not be empty`, `Input required: specify "prompt" or
"messages"`), pointing at the gateway dropping a string-form body during
translation rather than at the model. Re-run with block-form content, those same
15 ids returned `200`. A table built from the first pass would have been mostly
wrong, and would have looked measured.

Corrected result:

| Outcome | Count | Detail |
|---|---|---|
| Dual-protocol (`200` on both) | 22 | No endpoint restriction of any kind |
| Anthropic format refused | 1 | `grok-4.5`: `Model grok-4.5 is not supported for format anthropic` |
| Workspace opt-in required | 3 | `deepseek-v4-flash`, `deepseek-v4-pro` (`RegionError`, China-hosted), `muse-spark-1.2-contributor` (`DataPolicyError`) |
| Upstream unavailable | 3 | `hy3-preview`, `mimo-v2-omni`, `mimo-v2-pro` |

**Consequences.**

1. There is no per-model protocol split to encode. Endpoint selection is
   dual-protocol by default with a declared deny-list, today exactly
   `{grok-4.5}`. A 29-entry table would encode 22 identical rows plus noise.
2. `ocgo`'s table is wrong in substance, not merely stale. It forces
   `minimax-m3`, `minimax-m2.7`, `minimax-m2.5` and `qwen3.7-max` onto
   `/messages`; all four answer `/chat/completions`. Copying it would have
   pinned four models to a needless endpoint.
3. The opt-in and outage rows are account-scoped or transient and are
   deliberately NOT frozen into the protocol table. All 29 ids stay published and
   the upstream error surfaces verbatim, since a `RegionError` carries the opt-in
   URL that fixes it.
4. `/messages` authenticates by `X-API-Key` only. An `Authorization: Bearer`
   header on that path returns `AuthError: Missing API key`, while
   `/chat/completions` requires the bearer form.
5. The edge rejects a default HTTP client fingerprint with Cloudflare error
   1010, so a User-Agent is a functional requirement. This also explains a
   transient `403` on `GET /models`, which is otherwise public and needs no
   credential.

**Bare exposure.** Against the routing index, 3 of the 29 ids are contested and
deferred to incumbents per ADR 0020 (`deepseek-v4-flash`, `deepseek-v4-pro` to
deepseek; `kimi-k3` to kimi), leaving 26 bare-exposable. Recorded in
`docs/reference/opencode-go-exposure.json` and policed by
`scripts/check-opencode-exposure.py --check`, proven falsifiable by injecting a
collision.
