---
type: prd
project: reverso
slug: codex-anthropic-oauth
milestone: 2
status: approved
source_interview: rev-m2-gpt-oauth
final_ambiguity: 0.15
threshold: 0.20
context_type: brownfield
generated: 2026-06-21
related:
  - docs/architecture/adr/0005-bounded-cli-spine.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
---

# PRD: Codex GPT models on the Anthropic surface via ChatGPT OAuth (Milestone 2)

## Metadata

- Interview ID: rev-m2-gpt-oauth
- Rounds: 4 (Contrarian challenge used)
- Final ambiguity: 15 percent
- Threshold: 20 percent
- Context type: brownfield (extends the merged Milestone 1 Anthropic surface)
- Status: PASSED (pending execution approval)

## Problem and goal

Claude Code and the Claude Agent SDK can already reach reverso's non-claude backends through the inbound Anthropic Messages API delivered in Milestone 1. They cannot reach OpenAI gpt-* models. Milestone 2 adds a first-party Codex backend that exposes gpt-* models on the Anthropic surface ONLY, authenticated by the ChatGPT/Codex OAuth subscription through the local Codex CLI, with the Codex Responses-shaped output converted to the Anthropic Messages shape by reusing the Milestone 1 translation layer.

This is the symmetric mirror of the Milestone 1 design: the Claude Code CLI backend is Responses-surface-only (consumed by Codex), so the Codex CLI backend is Anthropic-surface-only (consumed by Claude Code). gpt-on-the-Responses-surface is circular (Codex talking to Codex) and is removed by this milestone.

## Resolved decisions (from the interview)

1. **Auth model and access mechanism**: ChatGPT/Codex OAuth subscription, accessed through the local Codex CLI subprocess via the bounded cli_spine (ADR 0005). This stays on the ADR 0006 codex-cli roadmap; reverso does NOT call the OpenAI Platform Responses API over HTTP and does NOT add the openai-python SDK as a runtime dependency. The OpenAI Apps SDK auth, openai-python, and API quickstart references were considered and informed this decision; the chosen path uses the subscription-backed ChatGPT OAuth that the Codex CLI already uses, not an API key.
2. **OAuth ownership**: managed gate plus token injection, mirroring the claude backend. A new CodexOAuthAuth resolver reads and validates the ChatGPT OAuth artifact, fails closed with a structured Anthropic error when the session is missing or expired, and injects the token into the Codex CLI child environment. (The artifact location and format are an explicit discovery spike, see Open questions.)
3. **Scope**: text non-streaming AND Anthropic-native streaming, tool use round-trip, and all five configured gpt models (gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1).
4. **Legacy path**: clean cut. Remove openai_cli_provider.py and the openai_cli gpt rows in this milestone; the first-party CodexAdapter is the sole gpt path.

## Background (current state, codebase-grounded)

- gpt models are served today ONLY through the legacy LiteLLM custom provider openai_cli_provider.py (src/reverso/proxy/openai_cli_provider.py), which runs codex exec as a subprocess and parses newline-delimited Responses-style JSON events. The config rows in config/litellm_config.yaml lines 63-87 use custom_llm_provider: openai_cli.
- There is NO first-party Codex adapter; build_adapters in src/reverso/proxy/compose.py returns claude, copilot, auggie, deepseek only.
- reverso does NOT manage Codex OAuth today; the Codex CLI is expected to be pre-authenticated. main.py injects only DEEPSEEK_API_KEY from Keychain.
- The claude backend (src/reverso/protocols/adapters/claude.py) is the template: ClaudeOAuthAuth reads and validates the OAuth artifact (Keychain or local credentials file), gates on token presence and expiry, and injects CLAUDE_CODE_OAUTH_TOKEN into the CLI child via stream_bounded_cli (cli_spine.py, ADR 0005).
- The Milestone 1 Anthropic surface (src/reverso/protocols/anthropic_app.py, anthropic_translate.py, anthropic_stream.py, anthropic_feature_gate.py, surface_registry.py) already converts the internal Responses contract to and from Anthropic, including streaming, tool use, and capability gating. SURFACE_BACKENDS is the single data-driven exposure point.
- codex_sync.py syncs reverso provider model listings into the Codex CLI config (wire_api responses); its interaction with the removed openai_cli rows must be reconciled (see Open questions).

## Topology (4 components)

| Component | Status | Description |
|-----------|--------|-------------|
| oauth-credential-layer | active | CodexOAuthAuth: read, validate, refresh-aware, and inject the ChatGPT OAuth token; fail closed with a structured error. Mirrors ClaudeOAuthAuth. |
| gpt-upstream-access | active | First-party CodexAdapter invoking codex exec via cli_spine (one-shot and streaming), parsing Codex Responses-style events into the internal Responses contract. |
| openai-responses-to-anthropic-translation | active | Reuse the M1 Anthropic translation and streaming layers; the CodexAdapter produces internal ResponsesRequest, ResponseEnvelope, and SSEEvent values, which the existing surface converts to and from Anthropic Messages. |
| surface-exposure-routing-gating | active | Add codex to SURFACE_BACKENDS anthropic frozenset (Anthropic-surface-only); register CodexAdapter in build_adapters; model to backend mapping for the 5 gpt ids; reuse M1 capability gating and parity harness; remove the legacy openai_cli path. |

## In scope

- A new first-party CodexAdapter implementing the frozen ProviderAdapter Protocol, invoking the Codex CLI through cli_spine for both non-streaming and streaming.
- A CodexOAuthAuth resolver that reads and validates the ChatGPT OAuth artifact, fails closed with a structured Anthropic error, and injects the OAuth token into the Codex CLI child environment.
- Exposure of gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1 on the Anthropic surface ONLY (a single SURFACE_BACKENDS row plus model to backend mapping).
- Anthropic Messages parity for the codex backend: non-streaming body, Anthropic-native SSE streaming, and tool_use round-trip, reusing the M1 translation, streaming mapper, capability gate, and parity harness.
- Clean-cut removal of openai_cli_provider.py and the openai_cli gpt rows; reconciliation of codex_sync.py and litellm_config.yaml.
- A docs-first ADR (next number 0007) describing the codex backend, the OAuth gate, and the openai_cli removal, before implementation.

## Out of scope and non-goals

- No direct OpenAI Platform Responses API calls and no openai-python runtime dependency (the SDK and Platform API references are reference-only; the chosen path is the Codex CLI under ChatGPT OAuth).
- No exposure of gpt models on the Responses surface (that path is circular and is removed, not relocated).
- No new inbound endpoints; M2 reuses the M1 Anthropic surface unchanged.
- No change to the frozen ProviderAdapter Protocol, replay.py, or the other adapters.
- No image input for the codex backend unless the Codex CLI is shown to support it (gate per feature_policy otherwise).

## Constraints

- uv-managed; loopback-only bind on the single composed port; no secrets in version control; OAuth token material never logged (use the existing redaction).
- ChatGPT OAuth subscription only; no OpenAI API key billing path.
- Reuse cli_spine (ADR 0005) for timeout, redaction, and kill-on-abandon; do not introduce a parallel subprocess runner.
- Reuse the frozen ProviderAdapter contract and the M1 Anthropic translation, streaming, and gating layers; the CodexAdapter only produces the internal Responses contract.
- codex is added to SURFACE_BACKENDS as data (Anthropic-surface-only); it must never be reachable on the Responses surface and must follow the same claude-exclusion style negative testing in reverse (no codex on Responses).
- Docs-first: ADR 0007 lands before code. No em-dashes or en-dashes or smart quotes; frontmatter on every markdown file.
- CI gate: uvx prek run --all-files must pass; tests via uv run pytest.

## Acceptance criteria

- [ ] Docs-first: ADR 0007 describes the codex backend, the ChatGPT OAuth gate plus token injection, the Anthropic-surface-only exposure, and the openai_cli removal, committed before implementation.
- [ ] A first-party CodexAdapter implements the frozen ProviderAdapter Protocol and invokes the Codex CLI via cli_spine for non-streaming and streaming, parsing Codex Responses-style events into the internal Responses contract.
- [ ] CodexOAuthAuth reads and validates the ChatGPT OAuth artifact and fails closed with a structured Anthropic error (a falsifiable gate, asserting subscription OAuth and not an API key) when the session is missing or expired; the OAuth token is injected into the CLI child and never logged.
- [ ] POST /v1/messages with a gpt-* model on the Anthropic surface returns a correct non-streaming Anthropic message body.
- [ ] POST /v1/messages with stream true and a gpt-* model returns the Anthropic-native SSE sequence.
- [ ] tool_use and tool_result round-trip correctly for gpt-* through the codex backend.
- [ ] All five gpt models (gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1) resolve to the codex backend on the Anthropic surface and are listed by GET /v1/models.
- [ ] The Anthropic parity suite is extended to cover the codex backend over its supported feature subset (mirroring copilot, deepseek, auggie).
- [ ] A negative test asserts gpt-* models and the codex backend are NOT reachable on the Responses surface (the mirror of the M1 claude-exclusion test).
- [ ] openai_cli_provider.py and the openai_cli gpt rows are removed; codex_sync.py and litellm_config.yaml are reconciled; the full suite stays green and uvx prek passes.

## Risks and mitigations

- OAuth artifact format unknown: the Codex OAuth artifact location and shape are not handled in reverso today. Mitigation: a time-boxed discovery spike before adapter work; model CodexOAuthAuth on ClaudeOAuthAuth once the format is known.
- Clean-cut rollback: removing openai_cli leaves no fallback if the OAuth adapter regresses. Mitigation (accepted by the product owner): the contrarian probe confirmed clean cut; rely on the parity suite plus the loopback smoke test and a fast revert via git if needed.
- Streaming and tool fidelity: the Codex CLI event grammar must map cleanly onto the internal Responses contract so the M1 streaming mapper and gate work unchanged. Mitigation: reuse the openai_cli_provider parsing knowledge; add codex to the parity matrix.
- codex_sync coupling: codex_sync writes reverso provider listings into the Codex config and references the gpt rows. Mitigation: reconcile codex_sync with the removal as part of the clean cut; covered by an acceptance criterion.

## Open questions and spikes

- Exact location and format of the Codex ChatGPT OAuth artifact (Keychain entry name, or a file such as ~/.codex/auth.json), token field names, and expiry, plus refresh behavior. Spike before CodexOAuthAuth implementation.
- Whether the Codex CLI exposes a token injection environment variable analogous to CLAUDE_CODE_OAUTH_TOKEN, or whether reverso must rely on the CLI reading its own stored session while reverso only validates the artifact.
- Precise reconciliation of codex_sync.py after the openai_cli rows are removed.

## Ontology (key entities)

| Entity | Type | Notes |
|--------|------|-------|
| CodexAdapter | core | first-party ProviderAdapter wrapping codex exec via cli_spine |
| CodexOAuthAuth | core | reads, validates, injects the ChatGPT OAuth token; fail closed |
| ChatGPTOAuthToken | core | subscription OAuth credential from codex login |
| CodexOAuthArtifact | supporting | local store for the token (Keychain or file); format is a spike |
| CodexCLI | external | the codex exec subprocess emitting Responses-style events |
| GptModel | supporting | the five gpt model ids exposed Anthropic-surface-only |
| ResponsesContract | existing internal | ResponsesRequest, ResponseEnvelope, SSEEvent |
| AnthropicSurface | existing (M1) | reused inbound surface, translation, streaming, gating |
| SurfaceRegistry | existing (M1) | SURFACE_BACKENDS data; one-row codex addition |
| CliSpine | existing | bounded subprocess runner (ADR 0005) |
| ClaudeCodeClient | external | the consumer of gpt via the Anthropic surface |

## References

- OpenAI Apps SDK auth: https://developers.openai.com/apps-sdk/build/auth (considered; informed the OAuth discussion)
- openai-python: https://github.com/openai/openai-python (considered; not adopted as a runtime dependency)
- OpenAI API quickstart: https://developers.openai.com/api/docs/quickstart (considered; API-key path not chosen)
- ADR 0005 bounded CLI spine; ADR 0006 inbound Anthropic Messages surface
- Template: src/reverso/protocols/adapters/claude.py (ClaudeOAuthAuth plus cli_spine)

## Interview transcript

<details>
<summary>Q and A (4 rounds plus Round 0 topology gate)</summary>

### Round 0: Topology
Q: Is the 4-component topology right? A: Looks right, 4 active.

### Round 1
Q: How should reverso authenticate to and reach gpt-* models?
A: ChatGPT OAuth via the Codex CLI (cli_spine), ADR 0006 roadmap; no direct API, no openai-python.
Ambiguity: 39 percent.

### Round 2
Q: How much should reverso own the OAuth credential vs rely on the CLI login?
A: Managed gate plus token inject (mirror claude).
Ambiguity: 27 percent.

### Round 3
Q: What must M2 include?
A: Text non-streaming plus streaming, tool use, all 5 gpt models, replace the legacy openai_cli path.
Ambiguity: 18 percent.

### Round 4 (Contrarian)
Q: Clean cut now or a safer staged removal of openai_cli?
A: Clean cut (replace now).
Ambiguity: 15 percent.

</details>
