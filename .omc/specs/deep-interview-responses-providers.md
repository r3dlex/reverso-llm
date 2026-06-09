---
type: deep-interview-spec
project: reverso
slug: responses-providers
profile: standard
final_ambiguity: 0.17
threshold: 0.20
context_type: brownfield
---

# Execution Spec: Reverso Responses Providers

## Metadata
- Profile: standard
- Rounds: 7
- Final ambiguity: 17 percent
- Threshold: 20 percent
- Context type: brownfield
- Context snapshot: `.omx/context/responses-providers-20260608T214418Z.md`

## Intent
Rework Reverso from a LiteLLM-centered local gateway into a first-party Python gateway that serves OpenAI Responses API endpoints for Codex. The motivation is to integrate subscription-backed Claude Code and GitHub Copilot backends with Codex through a Responses-native surface instead of relying on LiteLLM behavior where it is a poor fit.

## Desired outcome
A modern `uv` managed Reverso architecture where:
- Reverso owns the HTTP app and `/v1/responses` behavior directly.
- Claude Code is integrated by porting useful ideas or modules from `../claude-code-openai-wrapper`.
- GitHub Copilot is integrated by porting useful ideas or modules from `../copilot-openai-api`.
- Codex can use both providers through Reverso with Codex-observed Responses parity.

## In scope
- Replace LiteLLM as the core Reverso gateway architecture.
- Use FastAPI or an equivalent first-party Python ASGI app as the direct HTTP server surface.
- Serve `/v1/responses` for both Claude and Copilot provider routes.
- Provide Codex-compatible `/v1/models` behavior as needed for the provider profiles.
- Implement Codex-observed Responses parity for both providers.
- Port selected modules, patterns, and tests from the two local reference repos into Reverso, adapted to Reverso conventions.
- Update canonical Reverso docs before implementation, because `docs/AGENTS.md` says docs are the source of truth.
- Keep dependency management `uv` native.

## Out of scope and non-goals for the first milestone
- No Codex CLI provider reimplementation.
- No DeepSeek migration away from current behavior.
- No launchd productionization or decommissioning of existing LaunchAgents.

## Decision boundaries
OMX may decide without further confirmation:
- How to structure Reverso-native provider adapter modules.
- Which helper modules or patterns to port from the reference repos, as long as they are adapted with tests and `uv` dependency management.
- How to name internal adapter abstractions and tests.
- How to define the initial Codex-observed parity test harness from recorded or discoverable Codex request shapes.
- How to preserve local-only bind and secret handling rules.

OMX must not decide without further confirmation:
- To drop Claude or Copilot from the first deliverable.
- To migrate DeepSeek in the first milestone.
- To reimplement the Codex CLI provider in the first milestone.
- To productionize launchd or decommission old services in the first milestone.
- To store provider secrets in repository files.

## Constraints
- Reverso must remain a modern Python project managed with `uv`.
- Reverso repo hard rules apply: bind only to `127.0.0.1:64946`, no secrets in version control, no em-dashes or en-dashes in text files, frontmatter on every markdown file.
- Canonical Reverso docs must be updated before implementation.
- Reference project code should be ported selectively, not blindly vendored.
- First deliverable is not successful unless both Claude and Copilot pass the same Codex-observed Responses parity tests.

## Testable acceptance criteria
1. Docs-first gate:
   - BRD, PRD, architecture, MVP plan, or an ADR is updated to describe the LiteLLM replacement and Responses-native provider architecture before code implementation.
   - The docs explicitly state first milestone non-goals.
2. Gateway ownership:
   - Reverso can start a first-party ASGI server without LiteLLM as the core request router for the new provider paths.
3. Claude provider:
   - Claude route accepts Codex-observed `/v1/responses` payloads.
   - Claude route returns Codex-compatible non-streaming Responses output.
   - Claude route returns Codex-compatible SSE streaming output when requested.
   - Claude route handles session or `previous_response_id` behavior required by Codex-observed traffic.
4. Copilot provider:
   - Copilot route accepts the same Codex-observed `/v1/responses` payload suite.
   - Copilot route returns compatible non-streaming and streaming Responses output.
   - Copilot auth/token refresh follows the local token-file pattern or a Reverso-safe adaptation without repository secrets.
5. Shared parity suite:
   - A provider-independent test suite runs the same Codex-observed fixtures against Claude and Copilot adapters.
   - Unsupported Responses fields outside Codex-observed behavior are either compatibility stubs or explicit OpenAI-style errors.
6. Regression safety:
   - Existing Reverso unit tests continue passing or are intentionally updated after docs reflect the architecture change.
   - No first-milestone work migrates DeepSeek, Codex CLI provider, or launchd productionization.

## Assumptions exposed and resolutions
- Assumption: Going beyond LiteLLM might mean coexistence or experiment. Resolution: Replace LiteLLM core.
- Assumption: Full Responses parity means universal parity. Resolution: Codex-observed parity is the first milestone rule, with stubs or explicit errors beyond that subset.
- Assumption: One provider can lead while another lags. Resolution: Both Claude and Copilot must complete for the first deliverable.
- Assumption: Existing docs can lag implementation. Resolution: Docs first, then code.

## Brownfield evidence versus inference
Evidence:
- `reverso/AGENTS.md` and `reverso/README.md` currently define Reverso as LiteLLM-centered.
- Current code contains Responses compatibility middleware and profile routing tests.
- `../claude-code-openai-wrapper` has richer OpenAI-compatible server code, session management, streaming, tool, and test surfaces.
- `../copilot-openai-api/main.py` exposes `/responses`, `/chat/completions`, `/embeddings`, and `/models` by proxying GitHub Copilot API with local token refresh.

Inference:
- Reverso will likely need a provider adapter layer and a shared Responses normalization/streaming layer.
- Copilot feasibility risk is mainly auth, token refresh, and exact Responses compatibility.
- Claude feasibility risk is choosing how much `claude-agent-sdk` behavior to port versus wrapping CLI behavior.

## Docs and terminology ledger
Inspected:
- `reverso/AGENTS.md`
- `reverso/docs/AGENTS.md`
- `reverso/README.md`
- `reverso/docs/03-architecture.md`
- `reverso/pyproject.toml`
- `../claude-code-openai-wrapper/README.md`
- `../claude-code-openai-wrapper/pyproject.toml`
- `../copilot-openai-api/main.py`
- `../copilot-openai-api/pyproject.toml`

Terminology decisions:
- Use Responses-native gateway for the new target architecture.
- Use Codex-observed Responses parity for first milestone success, not universal parity.
- Use provider adapter for Claude and Copilot implementation slices.

Doc/code conflicts to resolve before implementation:
- Current docs state LiteLLM is the inbound proxy and source architecture. The replacement requires docs updates before code.

## Optional durable documentation recommendations
- Add an ADR for replacing LiteLLM core with a Reverso-owned Responses gateway.
- Update `docs/03-architecture.md` with the new runtime topology and provider adapter layer.
- Update `docs/04-mvp.md` with a new docs-first milestone for Claude and Copilot Responses parity.
- Update README profile instructions after implementation changes are verified.

## Recommended handoff
Use `$ralplan` next because this is a broad architecture and test-shape change with docs-first requirements. The plan should produce:
- Docs update plan or ADR draft.
- Adapter architecture.
- Shared Codex-observed Responses parity fixture plan.
- Provider-specific implementation sequence for Claude and Copilot.

After `$ralplan`, use `$ultragoal` for durable implementation goals. Use `$team` only if parallel provider lanes are needed after docs and core architecture are settled.
