---
type: deep-interview-context
project: reverso
slug: auggie-deepseek-responses
status: draft
ported_from: .omx/context/auggie-deepseek-responses-20260608T223519Z.md
ported_at: 20260609
---

# Deep Interview Context: Auggie and DeepSeek Responses API for Codex

## Task statement
Clarify how to support Auggie via `auggie-sdk` and implement an OpenAI Responses API surface for Codex. Indexing should be disabled by default. DeepSeek planning should consider `https://chat-deep.ai/docs/openai-sdk-to-deepseek/`.

## Desired outcome
A requirements-ready specification for a later ralplan or execution handoff. No implementation is started in this interview.

## Stated solution
Use the Python `auggie-sdk` for Auggie integration and expose an OpenAI Responses API compatible surface for Codex. Include DeepSeek compatibility guidance.

## Probable intent hypothesis
This likely extends the existing Reverso Responses gateway/provider direction with an Auggie provider and more explicit DeepSeek Responses behavior, but it may instead target `oh-my-auggie` as a separate Auggie-side gateway or plugin. That boundary must be confirmed.

## Known facts and evidence
- Reverso is a local subscription-backed gateway on `127.0.0.1:64946` that exposes OpenAI and Anthropic HTTP APIs and currently routes DeepSeek plus Claude-oriented profiles.
- Reverso docs say design decisions live under `docs/` and require docs-first changes before code.
- Reverso hard rules include loopback-only bind, no repository secrets, no en or em dash characters, frontmatter on Markdown, and no deletion.
- Reverso README already configures Codex profile routes with `wire_api = "responses"` for DeepSeek and Claude.
- `oh-my-auggie` is an orchestration layer for Augment Code's `auggie` CLI, not currently documented as an OpenAI Responses gateway.
- PyPI `auggie-sdk` 0.1.12 was released on Mar 25, 2026, requires Python >=3.10, and provides Python access to Augment CLI agent workflows, typed responses, function calling, event listeners, sessions, model selection, and mock ACP clients for tests.
- `auggie-sdk` prerequisites include the Augment CLI `auggie` installed and authenticated with `auggie login`.
- Chat-Deep.ai DeepSeek guidance shows OpenAI SDK compatibility patterns, including DeepSeek base URLs, streaming, JSON output, tool calls, thinking mode, and strict tool-call mode caveats.
- Chat-Deep.ai warns tool-call execution must validate arguments and avoid unrestricted file, shell, database, payment, email, or network actions without authorization and logging.

## Constraints
- Indexing disabled by default for Auggie integration.
- Must integrate with Codex through OpenAI Responses API semantics.
- Must remain local and safe if implemented in Reverso.
- No implementation during deep interview.

## Unknowns and open questions
- Whether Auggie belongs as a new Reverso provider path, an oh-my-auggie feature, or a separate bridge package.
- Whether DeepSeek should be part of the same first milestone or a follow-up after Auggie.
- Required route shape, such as `/auggie/v1/responses` and `/deepseek/v1/responses`.
- Whether Auggie tool calls should be observed only, translated into Responses tool calls, or blocked from Codex.
- How indexing-disabled default should be enforced and tested.

## Decision-boundary unknowns
- Repo boundary and product ownership.
- Provider parity expectations versus Auggie-specific behavior.
- Auth and credential handling for Auggie.
- Scope relationship to the already-approved Claude/Copilot Responses provider plan.

## Likely codebase touchpoints
- Reverso docs: `docs/01-brd.md`, `docs/02-prd.md`, `docs/03-architecture.md`, `docs/04-mvp.md`.
- Reverso provider/profile routing and middleware under `src/reverso/`.
- Reverso tests under `tests/`.
- If oh-my-auggie is selected instead: `oh-my-auggie/README.md`, `SPEC.md`, plugin and CLI files under `plugins/oma/` and `cli/`.

## Relevant repo docs and rules inspected
- `reverso/AGENTS.md`
- `reverso/README.md`
- `oh-my-auggie/AGENTS.md`
- `oh-my-auggie/README.md`

## Prompt-safe initial-context summary status
not_needed

## User clarification 1
All different providers should be served as different endpoint prefixes under the same port.

## Updated settled boundary
The likely route topology is one local Reverso server port, with provider-specific endpoint prefixes such as `/auggie/v1`, `/deepseek/v1`, `/claude/v1`, and `/copilot/v1`. This points the work toward Reverso's local gateway architecture rather than a separate per-provider server.

## Interview round 1
Question: For this interview, what is the first-milestone target now that providers should be endpoint prefixes on the same port?
Answer: Reverso Auggie+DeepSeek - Add Auggie and DeepSeek as same-port Reverso provider endpoints for Codex Responses.

## Updated ambiguity score
0.55. Scope ownership is clearer. Non-goals, indexing semantics, DeepSeek mode, and Auggie tool/session behavior remain unresolved.

## User clarification 2
Claude Code and Copilot should also be provider endpoint prefixes under the same local Reverso port.

## Updated settled topology
Reverso should expose provider-prefixed endpoints under one local port, including Claude Code, Copilot, Auggie, and DeepSeek. Existing and future Codex profiles should select providers by endpoint prefix, not by separate server processes or ports.

## Interview round 2
Question: Which items must stay out of scope for the first Auggie+DeepSeek Reverso endpoint milestone?
Answer: no-new-port-process, no-oh-my-auggie-edits, no-claude-copilot-replan.

## Updated ambiguity score
0.42. Topology and major non-goals are clearer. Indexing semantics, tool execution behavior, launchd scope, and provider success criteria remain unresolved.

## Interview round 3
Question: For Auggie, what must indexing disabled by default mean in the first Reverso endpoint milestone?
Answer: hard-disabled.

## Updated ambiguity score
0.32. Indexing is a hard safety gate. Need to resolve the fallback if `auggie-sdk` cannot prove indexing suppression, plus DeepSeek tool/thinking behavior and success criteria.

## Interview round 4
Question: If `auggie-sdk` cannot prove indexing is hard-disabled, what should the first milestone do?
Answer: document and skip hard disable.

## Tension found
This may conflict with the prior hard-disabled answer. It could mean ship Auggie with a documented caveat, or skip the Auggie endpoint until hard-disable evidence exists. Needs clarification before planning.

## Interview round 5
Question: When SDK-level hard-disable proof is missing, should Reverso still ship the Auggie endpoint with documentation, or skip the Auggie endpoint?
Answer: ship-with-caveat.

## Updated indexing decision
Auggie indexing is disabled by default as a hard intent, but if the SDK cannot prove hard-disable support, Reverso may ship with documented caveat and best-effort indexing suppression.

## Updated ambiguity score
0.27. Remaining high-impact ambiguity: DeepSeek Responses behavior, tool/thinking mode defaults, and acceptance criteria.

## Interview round 6
Question: For DeepSeek, what should the first Reverso Responses endpoint support by default?
Answer: full-deepseek-modes.

## Updated DeepSeek decision
DeepSeek first milestone should support full documented modes where applicable: text, streaming, JSON output, tool calls, thinking mode, and strict mode. Tool execution safety remains a readiness gate.

## Updated ambiguity score
0.21. Remaining ambiguity is primarily tool execution safety and final acceptance criteria.
