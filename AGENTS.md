---
type: agent-guide
project: reverso
stack: python-litellm-asyncio
last_updated: 2026-05-27
---

<!-- Generated: 2026-05-27 | Updated: 2026-05-27 -->

# AGENTS.md - Reverso

Read this before touching anything in this repo.

## What this project is

Reverso is a subscription-backed local LLM gateway. It runs on `127.0.0.1:64946`, wraps Claude Code CLI and Codex CLI as session-managed subprocess workers, HTTP-forwards DeepSeek and MiniMax, and exposes standard OpenAI and Anthropic HTTP APIs to any local tool that wants them.

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

- **Language:** Python 3.12+, managed with `uv`
- **HTTP server:** LiteLLM proxy (inbound, port 64946)
- **Async:** asyncio throughout
- **Service manager:** launchd (macOS), two LaunchAgents
- **Dependencies:** LiteLLM, httpx, asyncio, psutil (Phase 2+)

## Runtime topology

Two long-lived processes:

```
launchd
  |-- LiteLLM proxy process (:64946)
  |     |-- anthropic_cli_provider.py   (custom LiteLLM provider)
  |     |-- openai_cli_provider.py      (custom LiteLLM provider)
  |     |-- x_gateway middleware        (response envelope)
  |     |-- HTTP-forwarded: DeepSeek, MiniMax (standard LiteLLM, no custom code)
  |
  |-- Session daemon process (UDS only, no TCP)
        |-- Internal HTTP API over ~/Library/Application Support/reverso/daemon.sock
        |-- Session table: (machine, workspace, provider) -> Session
        |-- Wrapped CLI subprocesses: claude, codex
        |-- Recycle sweeper (asyncio task, 60-min tick)
        |-- Output parsers: parsers/claude_code.py, parsers/codex_cli.py
```

The two processes talk over a Unix-domain socket. LiteLLM uses `httpx.HTTPTransport(uds=...)`. The daemon has no TCP listener and cannot be exposed to the network even by misconfiguration.

## Repository layout

```
reverso/
  docs/                    # Spec docs (01-brd, 02-prd, 03-architecture, 04-mvp)
  src/
    reverso/
      __init__.py
      proxy/               # LiteLLM process code
        anthropic_cli_provider.py
        openai_cli_provider.py
      daemon/              # Session daemon process code
        main.py
        session.py
        recycler.py
        api.py
        parsers/
          claude_code.py
          codex_cli.py
      middleware/
        x_gateway.py       # Response envelope injector
  config/
    models.yaml            # Model registry (all 8 entries)
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
| CLI provider logic | `src/reverso/proxy/` | Stateless in Phase 1; session-routed in Phase 2+ |
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
| Phase 1 (skeleton) | Stateless single-turn gateway, all 4 providers | Working gateway, smoke tests, launchd agent for LiteLLM |
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
- `~/.config/litellm/minimax-codex.yaml` - MiniMax model aliases to absorb into `config/models.yaml`
- `~/.config/litellm/deepseek-codex.yaml` - DeepSeek model aliases to absorb
- `~/.codex/config.toml` - update `model_providers` and `profiles` to point at `http://127.0.0.1:64946`

The existing LaunchAgents (`com.andres.codex-litellm-minimax.plist`, `com.andres.codex-litellm-deepseek.plist`) are decommissioned after Reverso's LaunchAgents are verified working. Do not remove them manually; the install script handles decommission.

## Testing

- Run `python -m pytest tests/` from the repo root.
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
