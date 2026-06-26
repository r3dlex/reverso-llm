---
type: adr
project: reverso
id: 0012
title: Dynamic per-launch workspace on the Anthropic surface
status: Accepted
date: 2026-06-26
related:
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
  - docs/architecture/adr/0009-claude-on-anthropic-surface.md
---

# ADR 0012: Dynamic per-launch workspace on the Anthropic surface

## Status

Accepted, 2026-06-26. Personal-use, loopback-only. Additive to the Anthropic Messages
surface (ADR 0006); changes no routing or translation, only the working directory the
CLI-backed adapters spawn in.

## Context

The codex (`adapters/codex.py`) and claude (`adapters/claude.py`) adapters spawn their
backing CLI with `cwd=CURRENT_PROFILE_WORKSPACE.get()`, a `ContextVar` defined in
`reverso.proxy.profile_routing`. On the Responses gateway the profile-routing middleware
sets that contextvar from the request body / codex turn metadata, but the Anthropic
Messages surface (`anthropic_app.py`) never set it, so it was always `None` and the
spawned CLI inherited the daemon CWD (the reverso repo). A Claude Code session launched
from any other directory therefore ran its tools against the reverso repo, not the
caller's project.

## Decision

The Anthropic surface resolves a per-request workspace from a request header and sets
`CURRENT_PROFILE_WORKSPACE` for the dispatch:

1. **Header `x-reverso-workspace` (explicit override).** `_workspace_from_headers` reads
   the header, decodes it, and returns it ONLY when it is a non-empty ABSOLUTE path that
   exists as a directory (`os.path.isdir`); otherwise None. A non-existent or relative path
   is never passed as a subprocess cwd, because the bounded CLI spine raises if the cwd
   does not exist.

1b. **System-prompt `Primary working directory` (header-less default).** When no header is
   present, `_workspace_from_system_prompt` parses the `- Primary working directory: <path>`
   line Claude Code embeds in the system prompt of every request (the harness environment
   block), validated as an existing absolute directory. This makes the workspace default to
   the caller's launch directory for ANY Claude Code session pointed at reverso, with no
   client config, header, or shell reload. The header takes precedence when both are present.
   The dependency is on a stable Claude Code system-prompt line; if absent it simply falls
   back to None (the daemon CWD), never an error.

2. **Set the contextvar around the whole dispatch.** `AnthropicMessagesApp.__call__` sets
   `CURRENT_PROFILE_WORKSPACE` from the resolved workspace and resets it in a `finally`,
   so the streaming and non-streaming messages paths both see it when they spawn the
   adapter; it is harmless for count_tokens/models (no subprocess). The reset is correct
   on every return path.

3. **The `claude-reverso` launcher passes `$PWD`.** It adds
   `ANTHROPIC_CUSTOM_HEADERS="x-reverso-workspace: $PWD"` alongside the existing
   `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`. Claude Code forwards
   `ANTHROPIC_CUSTOM_HEADERS` to the gateway base URL, so reverso receives the launch
   directory. The builtin `claude`/`claude-code` and `claude-raw-*` launchers are not
   touched.

## Consequences

- A `claude-reverso` session (or any Claude Code session) run from any directory drives
  codex/claude tools in that directory by default, not the daemon CWD: the header sets it
  explicitly and the system-prompt line covers the header-less case.
- Both inputs only ever set a subprocess cwd validated as an existing absolute directory,
  so there is no injection risk, and reverso remains loopback-only.
- The contextvar import (`reverso.proxy.profile_routing`) is the same one the codex/claude
  adapters already import; it pulls in no legacy LiteLLM app or `litellm` module, so the
  ADR 0002 quarantine guard stays green.
