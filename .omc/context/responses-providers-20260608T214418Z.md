---
type: deep-interview-context
project: reverso
slug: responses-providers
created_at: 20260608T214418Z
---

# Context Snapshot: Reverso Responses Providers

## Task statement
Extend `reverso` beyond its current LiteLLM-centered gateway so it can serve the OpenAI Responses API to Codex for at least two additional subscription/provider backends:
- Claude Code, using ideas/code from local `../claude-code-openai-wrapper`
- GitHub Copilot, using local `../copilot-openai-api`

The user wants Reverso to remain a modern Python project managed with `uv`.

## Desired outcome
Execution-ready requirements and architecture plan for a Reverso evolution that exposes OpenAI-compatible Responses API surfaces for Claude Code and GitHub Copilot, with Codex integration as the primary client.

## Stated solution
Use ideas from `claude-code-openai-wrapper` and `copilot-openai-api`; go beyond LiteLLM; target OpenAI Responses API compatibility.

## Probable intent hypothesis
The user wants Reverso to become a unified local subscription-backed provider gateway for Codex, reducing dependence on LiteLLM's proxy behaviors where they do not map cleanly to Codex's Responses API expectations.

## Known facts/evidence
- `reverso/AGENTS.md` defines Reverso as a subscription-backed local LLM gateway on `127.0.0.1:64946`.
- Current docs describe a LiteLLM proxy plus a session daemon over UDS, with wrapped Claude Code and Codex subprocesses and HTTP-forwarded DeepSeek.
- Current README says Claude and DeepSeek can be exposed to Codex through Reverso profiles; MiniMax is direct Codex-only.
- Current code already contains Responses-oriented middleware: `codex_responses_normalizer.py`, `responses_sse_completion.py`, `responses_think_stripper.py`, `codex_models_compat.py`, and profile routing for `/deepseek/v1/responses` and `/claude/v1/responses`.
- `claude-code-openai-wrapper` is a local Python project with FastAPI-style OpenAI-compatible endpoints, session continuity, streaming, tool management, parameter validation, and tests. Its README says it uses the official Claude Agent SDK and supports `/v1/chat/completions`, `/v1/messages`, sessions, streaming, and multiple auth modes.
- `copilot-openai-api` is a local Python project with `main.py`, `run.py`, and `pyproject.toml`; it appears smaller than the Claude wrapper and needs deeper inspection before design.
- Official OpenAI docs/search results confirm current OpenAI models expose `v1/responses`, and several coding/reasoning models are Responses API capable. Codex config supports provider `wire_api = "responses"` in current local CLI behavior.

## Constraints
- Reverso repo rules: Python 3.12+/uv, no secrets in repo, bind only to `127.0.0.1:64946`, no em-dashes or en-dashes, frontmatter on every `.md`, docs are source of truth unless intentionally revised.
- Current locked docs say MiniMax direct Codex-only and current architecture is LiteLLM-centered; the requested change likely requires revising docs/architecture before implementation.
- Deep-interview mode must not implement directly.

## Unknowns/open questions
- Whether the first target is a full replacement of LiteLLM or coexistence with a custom Responses-native path.
- Which Responses API subset Codex must support initially: non-streaming only, streaming, tool calls, previous_response_id, reasoning fields, file/image input, cancellation, background mode.
- Whether Claude should use Claude Agent SDK from the wrapper, Claude Code CLI subprocess, or both as adapters.
- Whether Copilot should be treated as a first-class provider equal to Claude, or as a later experimental adapter.
- How provider sessions should map to Codex response/thread/session concepts.
- How to handle auth for GitHub Copilot and Claude without storing secrets in repo.

## Decision-boundary unknowns
- May OMX revise Reverso's locked docs to reflect a new architecture?
- May OMX add dependencies from the wrapper projects if justified, or must it keep dependencies minimal?
- May OMX archive/deprecate LiteLLM paths, or must they remain supported?
- May OMX refactor package layout significantly, or must changes be incremental?

## Likely codebase touchpoints
- `src/reverso/proxy/app.py`
- `src/reverso/proxy/main.py`
- `src/reverso/proxy/profile_routing.py`
- `src/reverso/proxy/anthropic_cli_provider.py`
- `src/reverso/daemon/session_daemon.py`
- `src/reverso/daemon/parsers/claude_code.py`
- `src/reverso/middleware/*responses*`
- `config/litellm_config.yaml`, `config/models.yaml`
- `tests/unit/test_profile_routing.py`, `tests/unit/test_codex_responses_normalizer.py`, new provider adapter tests
- docs: `docs/01-brd.md`, `docs/02-prd.md`, `docs/03-architecture.md`, `docs/04-mvp.md`, `README.md`

## Relevant repo docs/rules/context inspected
- `../AGENTS.md` root workspace guidance
- `reverso/AGENTS.md`
- `reverso/docs/AGENTS.md`
- `reverso/README.md`
- `reverso/docs/03-architecture.md`
- `reverso/pyproject.toml`
- Local reference repo surfaces under `../claude-code-openai-wrapper` and `../copilot-openai-api`

## Terminology or doc/code conflicts found
- Current Reverso docs say LiteLLM is the inbound proxy. User asks to go beyond LiteLLM.
- Current Reverso docs say Claude is wrapped through custom LiteLLM providers and daemon subprocesses. User references `claude-code-openai-wrapper`, which may favor a direct FastAPI/SDK adapter shape.
- Current docs mention OpenAI/Anthropic HTTP APIs, while user specifically names OpenAI Responses API as the target for Codex.

## Prompt-safe initial-context summary status
not_needed
