---
type: deep-interview-spec
project: reverso
slug: anthropic-api-surface
final_ambiguity: 0.18
threshold: 0.20
context_type: brownfield
status: PASSED
---

# Deep Interview Spec: Inbound Anthropic Messages API Surface

## Metadata

- Slug: anthropic-api-surface
- Type: brownfield
- Final Ambiguity Score: 0.18
- Threshold: 0.20 (default)
- Status: PASSED
- Rounds: 5

## Clarity Breakdown

| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.84 | 0.35 | 0.294 |
| Constraint Clarity | 0.80 | 0.25 | 0.200 |
| Success Criteria | 0.82 | 0.25 | 0.205 |
| Context Clarity | 0.82 | 0.15 | 0.123 |
| **Total Clarity** | | | **0.822** |
| **Ambiguity** | | | **0.18** |

## Goal

Add an inbound Anthropic Messages API surface to Reverso that coexists with the existing OpenAI
Responses surface on the same loopback port. The primary client is Claude Code and the Claude
Agent SDK pointed at Reverso via `ANTHROPIC_BASE_URL`; the fidelity target is
Claude-Code-observed parity (what Claude Code actually sends and consumes).

Non-claude backends are model-routed by default through a single first-party authority, with
optional per-profile path prefixes for explicit pinning. The full Messages feature surface is
gated per backend by a feature policy, so each backend serves the subset it can serve honestly
and rejects the rest with structured errors. The design is symmetric across frontends with
Milestone 2: where Claude Code CLI is Responses-surface-only, the Milestone 2 codex-cli backend
is Anthropic-surface-only.

## Constraints

- `uv`-managed Python; loopback `127.0.0.1:64946` only; no secrets in version control or logs.
- Docs-first gate: ADR plus companion docs before implementation; implementation via per-goal PRs.
- No em or en dash characters anywhere (`rg -nP '[\x{2013}\x{2014}]'` must stay clean); YAML
  frontmatter on every markdown file; never delete spec content (augment or deprecate in place).
- Reuse the FROZEN `ProviderAdapter` Protocol (create_response, stream_response, list_models,
  get_response, list_input_items) and the canonical replay seam (`protocols/replay.py`); the
  Protocol is never modified.
- Surface-scoped backend exposure is data-driven (a `SURFACE_BACKENDS` table), not per-surface
  code branches.
- Anthropic-surface backends (Milestone 1) are `copilot`, `deepseek`, and `auggie`. `claude` is
  excluded (Claude Code talking to a claude backend is circular).

## Non-Goals

- No reverso-as-Anthropic-client: Reverso does not call `api.anthropic.com` upstream.
- No Responses-surface regression: the existing OpenAI Responses surface is untouched in behavior.
- No Batches or Files API in Milestone 1.
- The claude backend is not exposed on the Anthropic surface.
- codex-cli is Milestone 2, not Milestone 1.

## Roadmap and Milestone 2

Milestone 2 adds `codex-cli` (gpt models) as an Anthropic-surface-only backend: a single
`SURFACE_BACKENDS` row, mirroring how the Claude Code CLI is Responses-surface-only. No new
surface, no Protocol change, no per-surface conditionals.

## Acceptance Criteria

Fourteen testable criteria. AC1 is the docs-first gate; the rest are implementation-PR gates.

- [ ] **AC1 (docs-first)**: ADR 0006 plus companion sections in README, `docs/03-architecture.md`,
  and `docs/04-mvp.md` exist, frontmattered, and dash-clean before any source change.
- [ ] **AC2**: `POST /v1/messages` non-streaming returns a well-formed Anthropic Messages
  response for a text turn on each Milestone 1 backend.
- [ ] **AC3**: `POST /v1/messages` with `stream: true` emits a well-formed Anthropic SSE sequence
  (message_start, content_block_start/delta/stop, message_delta, message_stop).
- [ ] **AC4**: a tool round-trip (assistant `tool_use` then client `tool_result`) completes on
  copilot (native) and deepseek (translated); auggie returns the documented text-only gated error.
- [ ] **AC5**: vision (image input), streamed thinking, and honored `cache_control` are gated per
  the capability matrix; gated cells return structured `invalid_request_error`.
- [ ] **AC6**: the `anthropic_translate` layer maps Messages to and from the frozen Responses
  contract with no change to the `ProviderAdapter` Protocol.
- [ ] **AC7**: `POST /v1/messages/count_tokens` returns a documented word-count approximation
  labeled as an estimate, not a real tokenizer result.
- [ ] **AC8**: `GET /v1/models` on the Anthropic surface lists exactly the Milestone 1
  Anthropic-surface backends' models and excludes claude.
- [ ] **AC9**: default auto-routing resolves a requested model to a backend through the single
  `surface_registry` authority.
- [ ] **AC10**: per-profile path prefixes (`/deepseek/v1/messages`, `/copilot/v1/messages`,
  `/auggie/v1/messages`) pin the backend and bypass auto-resolution.
- [ ] **AC11**: inbound auth and `anthropic-version` handling: a missing `anthropic-version`
  defaults to `"2023-06-01"` and is echoed; it is never a 400.
- [ ] **AC12**: a parity suite runs the Claude-Code-observed subset over copilot, deepseek, and
  auggie and records each cell pass/fail/gated.
- [ ] **AC13**: a negative claude-exclusion test asserts a claude model returns HTTP 404
  `not_found_error` on the Anthropic surface.
- [ ] **AC14**: an unknown non-claude model returns HTTP 404 `not_found_error` (no silent
  fallthrough), with the Anthropic error envelope shape.

## Topology

| Component | Status | Description | Coverage Note |
|-----------|--------|-------------|---------------|
| AnthropicMessagesApp | active | Pure-ASGI app mounted in compose; owns `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models` | Goal Round 1; routing Round 3 |
| anthropic_translate | active | Stateless Messages-to-Responses and Responses-to-Messages translation, reusing replay.py | Bar Round 2 |
| surface_registry | active | Single first-party model-to-backend authority; reads litellm_config.yaml as data; SURFACE_BACKENDS exposure | Round 3; claude exclusion Round 4 |
| feature_policy | active | Per-(feature x backend) capability gating with structured errors | Round 5 |

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Claude Code / Agent SDK | external system | ANTHROPIC_BASE_URL, anthropic-version | Calls AnthropicMessagesApp |
| AnthropicMessagesApp | core domain | /v1/messages, count_tokens, /v1/models | Mounted in compose; uses anthropic_translate |
| anthropic_translate | core domain | stateless map functions | Translates to/from frozen Responses contract |
| surface_registry | core domain | SURFACE_BACKENDS, model-to-backend map | Reads litellm_config as data; resolves backend |
| feature_policy | supporting | capability matrix, buckets | Gates features per backend |
| ProviderAdapter | frozen seam | 5-method Protocol | Backend contract; never modified |
| ResponseStore | supporting | in-memory state | Backs multi-turn via Responses contract |
| Anthropic error envelope | supporting | {type: error, error: {type, message}} | Returned on gated/404 cases |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|--------------|-----|---------|--------|-----------------|
| 1 | 6 | 6 | 0 | 0 | N/A |
| 2 | 7 | 1 | 0 | 6 | 86% |
| 3 | 8 | 1 | 0 | 7 | 88% |
| 4 | 8 | 0 | 0 | 8 | 100% |
| 5 | 8 | 0 | 0 | 8 | 100% |

The domain model converged: three consecutive rounds with full stability.

## Interview Transcript

<details>
<summary>Full Q&A (5 rounds)</summary>

### Round 1
**Q:** Who is the primary client and what is the parity bar?
**A:** Claude Code and the Claude Agent SDK via ANTHROPIC_BASE_URL; Claude-Code-observed parity,
not the full theoretical Messages surface.

### Round 2
**Q:** How do non-claude backends get selected, and is this reverso-as-Anthropic-client?
**A:** All non-claude backends are model-routed by default; inbound only, Reverso never calls
api.anthropic.com.

### Round 3
**Q:** Full Messages scope or a subset, and how is selection authoritative?
**A:** Full Messages feature surface, gated per backend by feature_policy; default auto-routing
through a single authority plus optional per-profile prefixes.

### Round 4
**Q:** What is the done-bar and is claude on this surface?
**A:** Done-bar is all backends pass their capability subset; claude is excluded (circular);
codex-cli is Milestone 2.

### Round 5
**Q:** How symmetric must Milestone 1 and Milestone 2 be?
**A:** Symmetric cross-frontend design: codex-cli is Anthropic-surface-only in M2, mirroring
Claude Code CLI being Responses-surface-only. Final ambiguity 0.18.

</details>
