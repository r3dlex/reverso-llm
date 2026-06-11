---
title: "Deep Interview Spec: Codex full integration with the four reverso providers"
status: pending-approval
interview_id: di-codex-reverso-20260610
type: brownfield
generated: 2026-06-10
rounds: 7
final_ambiguity: 0.18
threshold: 0.2
threshold_source: default
initial_context_summarized: yes
result: PASSED
---

# Deep Interview Spec: Codex full integration with the four reverso providers

## Metadata

- Interview ID: di-codex-reverso-20260610
- Rounds: 7 (plus Round 0 topology gate)
- Final Ambiguity Score: 18%
- Type: brownfield
- Generated: 2026-06-10
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: yes (session knowledge condensed into prompt-safe summary)
- Status: PASSED

## Clarity Breakdown

| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.82 | 0.35 | 0.287 |
| Constraint Clarity | 0.78 | 0.25 | 0.195 |
| Success Criteria | 0.83 | 0.25 | 0.208 |
| Context Clarity | 0.84 | 0.15 | 0.126 |
| **Total Clarity** | | | **0.816** |
| **Ambiguity** | | | **0.18** |

## Topology

| Component | Status | Description | Coverage / Deferral Note |
|-----------|--------|-------------|--------------------------|
| Context integration | active | Codex conversation context works per provider: multi-turn memory, workspace context, context window accounting, session resume | Goals set in Round 1; store policy in Round 6; criteria in Round 7 |
| /model command listing | active | Codex can list and select each provider's dynamically fetched models | Mechanism ladder set in Round 3; criteria in Round 7 |
| SSE/streaming integration | active | Streaming verified for all four providers; incremental where feasible | Bar set in Round 4 (Contrarian); criteria in Round 7 |
| Full Responses parity | active | The entire OpenAI Responses surface implemented or translated per provider, with explicit unsupported errors where physically infeasible | Bar set in Round 2; policy in Round 5; criteria in Round 7 |

## Goal

Make the Codex CLI a fully integrated client of the reverso gateway (127.0.0.1:64946) for all four providers (claude, copilot, auggie, deepseek), meaning:

1. **Context integration** per provider covers all four confirmed behaviors:
   - Multi-turn memory: a follow-up turn in the same Codex session remembers earlier turns, streaming and non-streaming.
   - Workspace context: Codex-injected project context (AGENTS.md, file contents) reaches each provider intact and is used in answers.
   - Context window accounting: accurate usage/token reporting per provider so the Codex context-left indicator and auto-compaction trigger correctly.
   - Session resume: `codex resume` works against the gateway, including the GET /responses/{id} and input_items retrieval paths.
2. **Model listing**: the right models, still obtained dynamically per provider via the live /v1/models endpoints, become listable/selectable from Codex following the mechanism ladder in Constraints.
3. **SSE/streaming**: incremental streaming where feasible. Claude gains true incremental deltas via its CLI stream-json output; auggie stays buffered with a documented limitation if its CLI cannot stream; copilot and deepseek are verified as already incremental.
4. **Full Responses parity ("SDK to SDK")**: implement or translate the entire OpenAI Responses API surface per provider, even features Codex does not exercise today. Features a provider spine physically cannot serve return a structured Responses-shaped 400 error with code `unsupported_feature` naming the provider and feature.

## Constraints

- Bind 127.0.0.1:64946 only; no other ports or interfaces.
- NO secrets in version control or logs; token material never printed.
- uv-managed Python; verification via `uv run pytest tests/unit -q` and `uv run pytest tests/integration -q`.
- The frozen ProviderAdapter Protocol (create_response, stream_response, list_models, get_response, list_input_items) is never modified.
- Claude adapter never consumes ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN from env (subscription OAuth only).
- /model mechanism ladder (Round 3): first research current Codex support for feeding the /model picker from custom providers and implement it if it exists; else build a reverso-side config sync tool that fetches live /v1/models and writes per-model profiles/entries into ~/.codex/config.toml; else document selection via -m and profile TOMLs.
- Response store policy (Round 6): verify real `codex resume` behavior against a restarted gateway FIRST. Only if resume demonstrably breaks, add disk persistence scoped per provider/repo with a 7-day TTL and a size cap.
- Parity infeasibility policy (Round 5): structured 400 `unsupported_feature` errors; no silent dropping of semantic features.
- Streaming policy (Round 4): incremental where feasible; buffered replay acceptable only where the provider spine cannot stream, and then documented.
- Every new .md carries YAML frontmatter; no em or en dash characters anywhere (`rg -nP '[\x{2013}\x{2014}]'` must stay clean); never delete spec content (augment or deprecate in place).
- No commits or PRs without explicit user approval; PR merge requires reviewer-loop APPROVE plus green GitHub CI plus green local CI.

## Non-Goals

- Forking or patching the Codex CLI itself (Round 3: upstream patch option explicitly not selected).
- Silent passthrough or undocumented best-effort degradation of unsupported features.
- Modifying the legacy LiteLLM fallthrough path (/v1) beyond what parity work strictly requires.
- Provider-side feature emulation that fabricates results (for example pretending to run web_search on a spine that has none).

## Acceptance Criteria

Composed done gate (Round 7): all three of the following.

- [ ] **E2E Codex matrix**: scripted real `codex` checks per provider, each cell pass/fail:
  - [ ] Multi-turn memory ("my name is Ada" then "what is my name?") per provider, streaming and non-streaming.
  - [ ] Workspace context reaches the provider (answer references injected file content) per provider.
  - [ ] Context window accounting: usage fields present and plausible so Codex compaction triggers (input_tokens, output_tokens, total_tokens) per provider.
  - [ ] `codex resume` works per provider; tested against a restarted gateway to settle the store-persistence question.
  - [ ] Streaming: incremental deltas observed for claude, copilot, deepseek; auggie incremental if feasible, else documented buffered behavior.
  - [ ] Tool-call loop end-to-end where the spine supports it (copilot native; deepseek translated); explicit documented behavior for claude/auggie.
  - [ ] Model selection works per provider via the chosen mechanism from the ladder.
- [ ] **Parity matrix doc**: a docs/ page (YAML frontmatter, no en/em dashes) recording the per-provider Responses feature support matrix, including every `unsupported_feature` entry and the streaming/buffering status per provider.
- [ ] **Test suites green**: `uv run pytest tests/unit -q` and `uv run pytest tests/integration -q` pass, with new fixture-backed tests for every added behavior (incremental claude deltas, unsupported_feature errors, store TTL/cap if built, parity surface coverage).

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| "Context integration" means one thing | Round 1 decomposed it into four candidate behaviors | All four selected: multi-turn memory, workspace context, window accounting, session resume |
| "Full SDK to SDK" might mean only what Codex uses | Round 2 offered Codex-critical subset vs full parity | Full Responses parity chosen, beyond Codex's used subset |
| /model picker can be fixed gateway-side | Round 3 cited evidence the limitation is in Codex | Mechanism ladder: research native support, then config sync tool, then documented -m/profiles; no Codex fork |
| True token streaming is required everywhere | Round 4 Contrarian: buffered replay already passes the SSE contract | Incremental where feasible; auggie may stay buffered with documentation |
| Full parity is achievable on every spine | Round 5: text-only CLI spines physically cannot serve some features | Structured 400 `unsupported_feature` errors |
| The response store must persist | Round 6 Simplifier: Codex keeps its own transcript locally | Verify `codex resume` against a restarted gateway first; persist (per provider/repo, 7-day TTL, size cap) only if resume breaks |
| "Done" is subjective | Round 7 composed an explicit gate | E2E Codex matrix plus parity matrix doc plus green test suites |

## Technical Context

- Gateway: composition root `reverso.proxy.compose:app` on 127.0.0.1:64946; first-party Responses prefixes /claude /copilot /auggie /deepseek; launchd agent com.user.reverso-proxy; logs at ~/Library/Logs/reverso/proxy.*.log.
- Adapters: claude (one-shot `claude` CLI subprocess per turn, subscription OAuth from claudeAiOauth artifact, live Anthropic /v1/models with alias fallback), copilot (direct forward to api.githubcopilot.com, Responses-native, live /models), auggie (one-shot `auggie` CLI subprocess, sandbox workspace root, `--ask` read-only posture, live `auggie model list --json` keyed by shortName), deepseek (chat-completions translation: tools/tool_choice conversion, usage renaming to input_tokens/output_tokens/total_tokens, live upstream /models with static fallback).
- Multi-turn memory rides the in-memory ResponseStore via previous_response_id and build_prompt/replay (src/reverso/protocols/replay.py); a gateway restart currently wipes it.
- Claude and auggie adapters buffer the full CLI completion and emit one SSE delta; copilot forwards upstream SSE; deepseek translates chat SSE into the Responses event grammar.
- Codex client facts established this session: profile TOMLs work (~/.codex/config.toml, copilot.config.toml, auggie.config.toml); `-m <id>` selects models; the TUI /model picker does not populate from custom provider endpoints in the installed version; Codex strictly parses terminal response.completed usage fields.
- 258 unit/integration tests green as of 2026-06-10 after the dynamic models work (ULTRAQA complete).

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Codex | external system | profiles, -m flag, /model picker, rollout files, compaction | Codex calls Reverso Gateway per provider prefix |
| Reverso Gateway | core domain | host 127.0.0.1, port 64946, provider prefixes | Gateway routes to Provider Adapters |
| Provider Adapter | core domain | claude, copilot, auggie, deepseek; frozen Protocol | Adapter serves Responses Feature Surface; uses Response Store |
| Response Store | supporting | in-memory, previous_response_id, input_items; candidate TTL/cap persistence | Store backs Context integration and Codex Session resume |
| Model Listing | supporting | live /v1/models per provider, dynamic | Feeds Codex model selection via mechanism ladder |
| SSE Stream | supporting | event grammar, deltas, [DONE], buffered vs incremental | Emitted by Adapters; consumed by Codex |
| Codex Session | external | resume, transcript, compaction triggers | Depends on Response Store and usage accounting |
| Profile/Config | supporting | ~/.codex/config.toml, per-provider profile TOMLs | Selects provider and model for Codex |
| Responses Feature Surface | core concept | full OpenAI Responses API feature set | Target of parity per Provider Adapter |
| Config Sync Tool | supporting (candidate) | fetches live models, writes Codex config entries | Bridge between Model Listing and Profile/Config |
| Unsupported-Feature Error | supporting | HTTP 400, code unsupported_feature, provider + feature named | Policy outcome of parity infeasibility |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|--------------|-----|---------|--------|-----------------|
| 1 | 8 | 8 | 0 | 0 | N/A |
| 2 | 9 | 1 | 0 | 8 | 89% |
| 3 | 10 | 1 | 0 | 9 | 90% |
| 4 | 10 | 0 | 0 | 10 | 100% |
| 5 | 11 | 1 | 0 | 10 | 91% |
| 6 | 11 | 0 | 0 | 11 | 100% |
| 7 | 11 | 0 | 0 | 11 | 100% |

The domain model converged: three consecutive rounds with full stability.

## Interview Transcript

<details>
<summary>Full Q&A (Round 0 + 7 rounds)</summary>

### Round 0 (Topology)
**Q:** Four top-level components: context integration, /model listing, SSE/streaming, full-integration gap audit. Right?
**A:** Looks right.

### Round 1
**Q:** What concrete behavior must "context integration" mean? (multi-turn memory, workspace context, window accounting, session resume)
**A:** All four.
**Ambiguity:** 49% (Goal 0.55, Constraints 0.35, Criteria 0.42, Context 0.82)

### Round 2
**Q:** What is the bar for "full SDK-to-SDK integration"?
**A:** Full Responses parity (entire surface, beyond what Codex uses today).
**Ambiguity:** 45% (Goal 0.65, Constraints 0.36, Criteria 0.45, Context 0.82)

### Round 3
**Q:** Which mechanisms are acceptable for /model listing given the Codex-side picker limitation?
**A:** Ladder: research native Codex support first, then config sync tool, then accept -m/profiles. No Codex fork.
**Ambiguity:** 38% (Goal 0.72, Constraints 0.49, Criteria 0.49, Context 0.84)

### Round 4 (Contrarian)
**Q:** Buffered replay already passes the SSE contract: is true token streaming actually needed?
**A:** Incremental where feasible: claude via stream-json; auggie buffered if its CLI cannot stream; copilot/deepseek verified as-is.
**Ambiguity:** 31% (Goal 0.82, Constraints 0.57, Criteria 0.55, Context 0.84)

### Round 5
**Q:** What must the gateway do when a feature is physically infeasible on a spine?
**A:** Explicit structured 400 `unsupported_feature` error naming provider and feature.
**Ambiguity:** 27% (Goal 0.82, Constraints 0.68, Criteria 0.60, Context 0.84)

### Round 6 (Simplifier)
**Q:** Does the gateway response store need to survive restarts at all?
**A:** Verify real `codex resume` behavior first; otherwise persist with per-provider/repo state, 7-day TTL, size cap.
**Ambiguity:** 23% (Goal 0.82, Constraints 0.78, Criteria 0.64, Context 0.84)

### Round 7
**Q:** What is the acceptance gate proving full integration?
**A:** E2E Codex matrix + parity matrix doc + test suites green.
**Ambiguity:** 18% (Goal 0.82, Constraints 0.78, Criteria 0.83, Context 0.84)

</details>
