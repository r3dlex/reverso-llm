---
type: agent-guide
project: reverso
stack: python-asgi-asyncio
last_updated: 2026-06-10
---

<!-- Generated: 2026-05-27 | Updated: 2026-06-10 -->

# AGENTS.md - Reverso

Read this before touching anything in this repo.

## What this project is

Reverso is a subscription-backed local LLM gateway. It runs on `127.0.0.1:64946` and serves the OpenAI Responses API natively through a first-party gateway app (`reverso.protocols.responses_app.ResponsesGatewayApp`) with one adapter per provider: claude (subscription OAuth via the claude CLI), copilot (direct upstream forward), auggie (auggie CLI), and deepseek (first-party HTTP). See ADR 0002 and ADR 0003 under `docs/architecture/adr/`. The legacy LiteLLM app remains composed behind the first-party app as fallthrough only for paths the gateway does not own. Wrapped CLI session management (Claude Code CLI, Codex CLI) is handled by the session daemon. MiniMax is direct Codex-only and is not routed through Reverso.

**Why it exists:** The developer pays flat-rate for Claude Max and ChatGPT Pro subscriptions. Those subscriptions include CLI tools with unlimited use at the margin. Reverso lets any HTTP-speaking tool (agents, IDE plugins, scripts) consume those subscriptions instead of requiring separate metered API accounts.

**Personal use only.** Single user, single machine. Not for sharing or resale.

## Specification documents

All design decisions live in `docs/`. Read before writing code.

| Doc | Content |
|---|---|
| `docs/01-brd.md` | Business requirements, rationale, constraints, glossary, Q1-Q18 locked decisions |
| `docs/02-prd.md` | Product requirements: all functional requirements by area |
| `docs/03-architecture.md` | Component architecture, runtime topology, request flow, failure modes |
| `docs/04-mvp.md` | Phased implementation plan: Phase 0 (spike) through Phase 4 (hardening) |

## Stack

- **Language:** Python 3.12+, managed with `uv` (run tests as `uv run pytest`)
- **HTTP server:** first-party ASGI Responses gateway (`reverso.protocols.responses_app`, no web framework) composed in front of the legacy LiteLLM app (fallthrough only), port 64946
- **Async:** asyncio throughout
- **Service manager:** launchd (macOS), two LaunchAgents
- **Dependencies:** httpx, psutil, LiteLLM (legacy fallthrough path only)
- **Frozen interface:** the `ProviderAdapter` Protocol in `src/reverso/protocols/adapter.py` (create_response, stream_response, list_models, get_response, list_input_items) is frozen per ADR 0002 section 11.3; never modify it

## Runtime topology (current, ADR 0002/0003)

Two long-lived processes:

```
launchd
  |-- Gateway process (:64946, 127.0.0.1 only)
  |     |-- ResponsesGatewayApp (src/reverso/protocols/responses_app.py)
  |     |     |-- /claude/v1   -> ClaudeAdapter   (subscription OAuth, claude CLI)
  |     |     |-- /copilot/v1  -> CopilotAdapter  (direct upstream forward)
  |     |     |-- /auggie/v1   -> AuggieAdapter   (auggie CLI completions)
  |     |     |-- /deepseek/v1 -> DeepSeekAdapter (first-party HTTP, ADR 0003)
  |     |     |-- replay seam (protocols/replay.py): canonical SSE sequence +
  |     |     |   store-before-drain for buffered providers (claude/auggie/deepseek)
  |     |     |-- in-memory ResponseStore (protocols/store.py)
  |     |-- legacy LiteLLM app: fallthrough ONLY for paths the first-party app
  |         does not own (legacy PROVIDER_PREFIXES is intentionally not mutated)
  |
  |-- Session daemon process (UDS only, no TCP)
        |-- Internal HTTP API over ~/Library/Application Support/reverso/daemon.sock
        |-- Session table: (machine, workspace, provider) -> Session
        |-- Wrapped CLI subprocesses: claude, codex
        |-- Recycle sweeper (asyncio task, 60-min tick; pure decide_recycle policy)
        |-- Output parsers: parsers/claude_code.py, parsers/codex_cli.py
```

The two processes talk over a Unix-domain socket. The daemon has no TCP listener and cannot be exposed to the network even by misconfiguration.

### Runtime topology (legacy, superseded 2026-06-10)

Deprecated, kept for history per the never-delete rule. This described the pre-ADR-0002 LiteLLM-fronted topology; do not use it for routing decisions.

```
launchd
  |-- LiteLLM proxy process (:64946)
  |     |-- anthropic_cli_provider.py   (custom LiteLLM provider)
  |     |-- openai_cli_provider.py      (custom LiteLLM provider)
  |     |-- x_gateway middleware        (response envelope)
  |     |-- HTTP-forwarded: DeepSeek (standard LiteLLM, no custom code)
  |
  |-- Session daemon process (UDS only, no TCP)
```

## Repository layout

```
reverso/
  docs/                    # Spec docs (01-brd, 02-prd, 03-architecture, 04-mvp)
  src/
    reverso/
      __init__.py
      protocols/           # First-party Responses gateway (ADR 0002/0003)
        responses_app.py   # ResponsesGatewayApp, owns /claude|copilot|auggie|deepseek /v1
        adapter.py         # FROZEN ProviderAdapter Protocol (ADR 0002 11.3)
        adapters/          # claude.py, copilot.py, auggie.py, deepseek.py
        replay.py          # Canonical SSE replay seam (store-before-drain)
        store.py           # In-memory ResponseStore
        auth.py            # AuthResolution / ProviderAuth (subscription OAuth)
        middleware.py
      proxy/               # Legacy LiteLLM process code (fallthrough only)
        anthropic_cli_provider.py
        openai_cli_provider.py
        compose.py         # Composes ResponsesGatewayApp in front of LiteLLM
      daemon/              # Session daemon process code
        main.py
        session_daemon.py
        session_table.py
        recycler.py
        parsers/
          claude_code.py
          codex_cli.py
      middleware/          # Response envelope + Codex compat shims
  config/
    models.yaml            # Model registry for Reverso-supported models
    config.yaml            # Runtime config (port, timeouts, paths)
    litellm_config.yaml    # LiteLLM-specific config
    tool_mappings.yaml     # Inbound-tool to CLI-tool mappings
  launchd/
    com.user.reverso-daemon.plist.tmpl
    com.user.reverso-proxy.plist.tmpl
  scripts/                 # install-launchagents.sh, keychain-set.sh, smoke.sh
  tests/
    integration/
    unit/
  pyproject.toml
  README.md
```

## Routing rules for agents

**Before making changes:**
1. Read the relevant spec doc section first. Design decisions in `01-brd.md` Q1-Q18 are locked.
2. Check `03-architecture.md` Section 8 (failure modes) before touching session lifecycle or provider code.
3. Check `04-mvp.md` for the current phase and its exit criteria before adding new features.

**Routing by area:**

| Area | Location | Key constraint |
|---|---|---|
| Model registry edits | `config/models.yaml` | Loaded at startup only; changes require restart |
| Responses providers | `src/reverso/protocols/` | `adapter.py` Protocol is FROZEN (ADR 0002 11.3); buffered adapters must stream via `replay.py` (store-before-drain) |
| CLI provider logic (legacy) | `src/reverso/proxy/` | Fallthrough only; do not add new providers here, use `protocols/adapters/` |
| Session management | `src/reverso/daemon/` | In-memory only; no persistence; restart loses all sessions |
| Tool-use parsing | `src/reverso/daemon/parsers/` | Most fragile component; upstream CLIs control output format |
| Response envelope | `src/reverso/middleware/x_gateway.py` | Must be present on every response, all providers |
| Config files | `config/` | Committed to git; secrets are NOT here |
| Secrets | macOS Keychain under `reverso/<KEY_NAME>` | Never in repo; read via `security` CLI at startup |
| LaunchAgent plists | `launchd/` | Templates; expanded by install script |

## Phases and current scope

See `docs/04-mvp.md` for full exit criteria per phase.

| Phase | Goal | Key deliverable |
|---|---|---|
| Phase 0 (spike) | Answer 6 open questions about CLI non-interactive behavior | `docs/spike-notes.md` + go/no-go decision |
| Phase 1 (skeleton) | Stateless single-turn gateway, Reverso providers | Working gateway, smoke tests, launchd agent for LiteLLM |
| Phase 2 (sessions) | Multi-turn sessions, session daemon | Session daemon, second launchd agent |
| Phase 3 (interception) | Tool-use observation parsing | `x_gateway.observations` populated |
| Phase 4 (hardening) | Production reliability | Timeouts, log rotation, polished README |

Do not skip Phase 0 deliverables. Architecture details in Phase 1+ depend on Phase 0 findings.

## Hard rules (apply everywhere)

- **Bind to `127.0.0.1:64946` only.** No other bind address. This is the only security boundary. Reject config that changes this.
- **No secrets in version control.** Secrets live in macOS Keychain. Use `security find-generic-password` to read them.
- **No em-dashes (U+2014).** Use hyphens (-) in all text files including docs, comments, and commit messages.
- **No en-dashes (U+2013).** Use hyphens (-) for all structural breaks.
- **Frontmatter on every `.md`.** Every markdown file in this repo needs valid YAML frontmatter.
- **Never submit Workday.** No agent clicks Submit on any Workday form without explicit user approval.
- **Never delete.** Move stale files to `Archive/` or mark them deprecated in place.

## Existing infrastructure being replaced

Reverso replaces the existing `codex-litellm-responses-shim` setup. The following files are the source of ported logic:

- `~/.local/bin/codex-litellm-responses-shim` - normalization functions to port: `normalize_function_call_arguments`, `sanitize_input_tool_sequence`, `compact_input_items`, `strip_think_blocks`, `normalize_responses_payload`
- `~/.config/litellm/minimax-codex.yaml` - legacy MiniMax shim config; do not absorb into Reverso because MiniMax is direct Codex-only
- `~/.config/litellm/deepseek-codex.yaml` - DeepSeek model aliases to absorb
- `~/.codex/config.toml` - update `model_providers` and `profiles` to point at `http://127.0.0.1:64946`

The existing LaunchAgents (`com.andres.codex-litellm-minimax.plist`, `com.andres.codex-litellm-deepseek.plist`) are decommissioned after Reverso's LaunchAgents are verified working. Do not remove them manually; the install script handles decommission.

## Testing

- Run `uv run pytest tests/unit -q` and `uv run pytest tests/integration -q` from the repo root (uv-managed Python; `python -m pytest` is deprecated here).
- Smoke tests: `./scripts/smoke.sh` (requires gateway running).
- Phase-specific integration tests are under `tests/integration/`.
- For Phase 0, results go in `docs/spike-notes.md`.

## Key design decisions (locked, from Q1-Q18 in `docs/01-brd.md`)

- Session key is `(machine, workspace, provider)`. Machine dimension reserved for v2.
- Idle = no API in flight AND no live child processes. `psutil` walks the process tree.
- No max age, no max turn count. Infinite session lifetime is a feature.
- Gateway restart is full reset. No persistence layer.
- `x_gateway` envelope is always present on all responses (even HTTP-forwarded).
- `127.0.0.1` bind is the only security boundary. No auth in v1.
- Tool-use interception is IV-pragmatic: CLIs execute their own tools, gateway reports observations after the fact.

<!-- MANUAL: Add manually curated notes below this line. They are preserved on regeneration. -->

- Reverso Codex profile files should use GPT model names. Provider slugs are resolved by `/deepseek/v1` and `/claude/v1` profile routing so Codex keeps its own model metadata. MiniMax is direct Codex-only and should use `model = "MiniMax-M3"` with `model_provider = "minimax"`.

<!-- v3-ai-sdlc-init:start -->
## AI SDLC v3
This repo follows the v3 AI-SDLC layout. See `.ai/matrix.json`, `.memory/human-override/`, and `docs/architecture/adr/`. Modules at `r3dlex/skills/ai-sdlc-init/modules/`.
<!-- v3-ai-sdlc-init:end -->
