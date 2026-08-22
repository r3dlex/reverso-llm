---
title: OpenRouter Northstar plan and Autobahn implementation prompt
status: ready-for-use
slug: openrouter-autobahn-implementation-prompt
source_handoff: .ai/handoff/northstar-openrouter-reverso-provider.md
---

# OpenRouter Northstar plan and Autobahn implementation prompt

## Northstar plan

### Goal

Add OpenRouter as a first-party Reverso-routed provider for Codex and Claude Code.

- Initial model: `stealth/ox-alpha`.
- Future models: explicitly added to a curated user allowlist.

### Architecture

- Codex uses native OpenRouter Responses through `http://127.0.0.1:64946/openrouter/v1/responses`.
- Claude Code uses native Anthropic Messages through `http://127.0.0.1:64946/openrouter/v1/messages`.
- Use direct `httpx`; add no OpenAI or Anthropic SDK dependency.
- Preserve the frozen `ProviderAdapter`.
- Use one composition-owned runtime with shared safety state.
- Run Headroom exactly once per request.
- Add no listener, daemon, LiteLLM route, or model substitution.

### Safety boundaries

- Default to required parameters, denied data collection, and ZDR.
- Weaker privacy requires attended consent for the exact model and launcher.
- Grants are memory-only, revoked on launcher exit or gateway restart, with a maximum 24-hour crash backstop.
- Require fresh endpoint-specific policy and pricing evidence before every request.
- Cached metadata is discovery-only.
- Require both user-defined spending limits: estimated per-request maximum and cumulative launcher-session maximum.
- Accepted streams finish after later policy, allowlist, consent, or budget changes; subsequent requests block.
- Read `OPENROUTER_API_KEY` from `$HOME/.zsh_exports`. Never expose it through generated artifacts, logs, errors, process arguments, or telemetry.

### Delivery DAG

```text
OR-G0 -> OR-G1 -> {OR-G2, OR-G4}
OR-G2 -> OR-G3
OR-G4 -> OR-G5
{OR-G3, OR-G5} -> OR-G6 -> OR-G7
```

1. **OR-G0 - Attended compatibility spike**
   - Prove native endpoints, SSE shapes, endpoint-bound policy/pricing evidence, client capability transport, Claude alias grammar, and complete Responses replay.
   - Produce an explicit `GO` or `REVISE` decision.
2. **OR-G1 - Deny-first runtime and live catalog**
   - Add the shared runtime, credential resolution, discovery, privacy grants, and atomic budgets.
3. **OR-G2 - Codex unary Responses**
   - Add native unary forwarding, storage, usage, cost reconciliation, and bounded errors.
4. **OR-G3 - Codex streaming and stateless continuation**
   - Add SSE and complete typed replay for multi-turn tool loops.
5. **OR-G4 - Claude unary Messages**
   - Add the native Messages facet, tools, images/PDFs, thinking, usage, and exact model authority.
6. **OR-G5 - Claude streaming Messages**
   - Add native SSE, streamed tools/thinking, cancellation, and accepted-stream accounting.
7. **OR-G6 - Managed client exposure**
   - Add the allowlist CLI, Codex profile/catalog, Claude launcher/catalog, ephemeral grants, and convergence.
8. **OR-G7 - Observability and attended proof**
   - Add safe metrics, documentation, secret scans, full gates, and live Codex/Claude proof.

### Authoritative artifacts

- `docs/specifications/ACTIVE/openrouter-reverso-provider.md`
- `.ai/handoff/northstar-openrouter-reverso-provider.md`
- `.ai/work-intake/openrouter-reverso-provider.md`
- `.ai/work-intake/openrouter-reverso-provider-or-g0.md` through `.ai/work-intake/openrouter-reverso-provider-or-g7.md`
- `.omx/plans/prd-openrouter-reverso-provider.md`
- `.omx/plans/test-spec-openrouter-reverso-provider.md`

## Autobahn implementation prompt

```text
$ai-catapult:autobahn

Implement the complete OpenRouter Northstar handoff in the Reverso repository:

  /Users/andreburgstahler/Ws/Personal/AiTool/reverso

Authoritative handoff:

  .ai/handoff/northstar-openrouter-reverso-provider.md

Use the existing isolated worktree if valid:

  /Users/andreburgstahler/Ws/Personal/AiTool/.worktrees/reverso-openrouter-g0

Execution requirements:

1. Use native role routing when the execution surface exposes `agent_type`,
   always selecting an installed OMX role for specialized work. Run
   `omx ralplan preflight --json` only if the native surface reports
   `role_routing_unavailable` and adapted Ralplan authority is required. Fail
   closed on `unsupported_documented_leader_proof`; never fabricate leader or
   consensus authority from session, thread, pointer, transcript, or cwd state.

2. Execute OR-G0 through OR-G7 in dependency order, exactly one goal and one PR
   at a time. Do not collapse goals into a mega-PR.

3. OR-G0 and OR-G7 are attended. OR-G1 through OR-G6 are AFK after their
   dependencies pass.

4. For OR-G0:
   - Read `OPENROUTER_API_KEY` by sourcing `$HOME/.zsh_exports`.
   - Never print, copy, persist, or pass the key through command arguments.
   - Use `stealth/ox-alpha` initially.
   - Propose a maximum US$0.10 total probe budget and obtain explicit attended
     approval before any inference request.
   - Revalidate pricing immediately before each probe.
   - Use non-sensitive synthetic prompts only.
   - Record only redacted schemas, request IDs, routing evidence, and reported
     cost.
   - Produce an explicit GO or REVISE verdict.
   - Stop the DAG on REVISE.

5. Preserve all settled architecture:
   - direct `httpx` native Responses and Messages;
   - frozen `ProviderAdapter`;
   - one composition-owned runtime;
   - one loopback listener;
   - one Headroom pass;
   - curated allowlist;
   - per-surface model exposure;
   - no model substitution;
   - default ZDR and denied data collection;
   - attended exact-model privacy grants;
   - immediate launcher-exit and gateway-restart revocation;
   - maximum 24-hour crash backstop;
   - mandatory per-request and launcher-session spend limits;
   - accepted streams finish while later requests block.

6. Use TDD for every goal:
   - run the goal's focused command and record the failing result;
   - implement only that goal;
   - run the same command green;
   - write evidence under
     `.ai/evidence/openrouter-reverso-provider/or-gN-red-green.md`.

7. Run Autobahn gate drivers rather than manually skipping gates:
   - pre-commit gates before every commit;
   - Architect, Code Reviewer, and Executor review loop;
   - pre-merge gates and remote CI;
   - resolve every review comment.

8. Preserve unrelated work. Do not edit or reset the dirty
   `feat/ollama-cloud-catalog-authority` checkout.

9. Do not expose secrets, capability handles, prompts, response bodies, local
   paths, or usernames in files, logs, errors, snapshots, or metrics.

10. Default merge authority is ready-for-human. Merge only when host policy
    provides a valid authorized verdict and all local and hosted checks are
    green.

Continue through OR-G7 unless a declared fail-closed gate blocks. Report the
exact blocker with evidence rather than bypassing it.
```
