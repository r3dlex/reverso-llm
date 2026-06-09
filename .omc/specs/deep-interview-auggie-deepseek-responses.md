---
type: deep-interview-spec
project: reverso
slug: auggie-deepseek-responses
status: ready-for-ralplan
ported_from: .omx/specs/deep-interview-auggie-deepseek-responses.md
ported_at: 20260609
---

# Deep Interview Spec: Auggie and DeepSeek Responses Providers

## Objective
Plan a Reverso milestone that adds Auggie and DeepSeek as same-port provider endpoints for Codex using the OpenAI Responses API shape.

## Required provider topology
- Reverso serves all providers under one local port.
- Provider selection is by endpoint prefix, for example:
  - `/auggie/v1/responses`
  - `/deepseek/v1/responses`
  - existing or planned `/claude/v1/...`
  - existing or planned `/copilot/v1/...`
- Do not introduce a new port or separate provider server process for this milestone.

## First milestone scope
- Add Auggie provider endpoint support in Reverso.
- Add DeepSeek Responses endpoint support in Reverso.
- Keep this as Reverso work, not oh-my-auggie work.
- Do not replan Claude Code or Copilot except to align the endpoint topology statement.

## Auggie requirements
- Use `auggie-sdk` as the primary candidate integration surface.
- The SDK requires the Augment CLI `auggie` to be installed and authenticated with `auggie login`.
- The SDK supports sessions, typed results, function calling, event listeners, model listing, and mock ACP clients for tests.
- Indexing must be disabled by default as a hard intent.
- If SDK-level proof of hard-disable behavior is unavailable, the endpoint may still ship with best-effort suppression plus explicit documentation of the caveat.
- The plan must include a research or spike step to determine whether the SDK or underlying Auggie CLI exposes a reliable indexing-disable control.

## DeepSeek requirements
- DeepSeek first milestone should support full documented modes where practical:
  - text
  - streaming
  - JSON output
  - tool calls
  - thinking mode
  - strict tool-call mode where supported
- The plan must consider the OpenAI SDK compatibility guidance from Chat-Deep.ai.
- DeepSeek tool-call execution should follow provider-native behavior with documentation and minimal Reverso restrictions.
- Even with provider-native behavior, the plan must call out validation and authorization risks from the DeepSeek guidance.

## Non-goals
- No new port or separate provider server process.
- No edits to `oh-my-auggie/` in this milestone.
- No Claude Code or Copilot replan, except endpoint topology alignment.

## Decisions for ralplan to preserve
- Same-port provider endpoint architecture is mandatory.
- Auggie and DeepSeek are both in the first milestone.
- Auggie indexing default is hard-disabled in intent, but may ship with documented caveat if proof is unavailable.
- DeepSeek should target full documented mode support, not text-only parity.
- Tool behavior is provider-native, with documented risk boundaries.

## Acceptance criteria shape
A future ralplan should produce testable criteria for:
- `/auggie/v1/responses` and `/deepseek/v1/responses` on the same Reverso port.
- Codex-compatible non-streaming and streaming Responses objects.
- Provider-specific model listing.
- Auggie auth and unavailable-Auggie failure behavior.
- Indexing suppression attempt and caveat documentation.
- DeepSeek JSON output, tool calls, thinking mode, and strict mode coverage where feasible.
- No new port/process.
- No oh-my-auggie repo changes.
- No source changes to Claude/Copilot plan except topology wording if needed.

## Evidence sources
- PyPI Auggie SDK: https://pypi.org/project/auggie-sdk/
- DeepSeek OpenAI SDK compatibility guide: https://chat-deep.ai/docs/openai-sdk-to-deepseek/
- Reverso context snapshot: `.omc/context/auggie-deepseek-responses-20260608T223519Z.md`
- Transcript: `.omc/interviews/auggie-deepseek-responses-20260608T224423Z.md`

## Recommended next step
Run `$ralplan` against this spec before implementation. (This OMC port already carries the resulting plan at `.omc/plans/ralplan-auggie-deepseek-responses.md`; satisfy the consensus gate recorded there before execution.)
