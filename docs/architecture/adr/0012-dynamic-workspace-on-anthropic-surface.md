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

1. **Header `x-reverso-workspace`.** `_workspace_from_headers` reads the header, decodes
   it, and returns it ONLY when it is a non-empty ABSOLUTE path that exists as a directory
   (`os.path.isdir`); otherwise None. A non-existent or relative path is never passed as a
   subprocess cwd, because the bounded CLI spine raises if the cwd does not exist.

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

- A `claude-reverso` session run from any directory drives codex/claude tools in that
  directory, not the daemon CWD.
- The header is the only input; it merely sets a subprocess cwd validated as an existing
  absolute directory, so there is no injection risk, and reverso remains loopback-only.
- The contextvar import (`reverso.proxy.profile_routing`) is the same one the codex/claude
  adapters already import; it pulls in no legacy LiteLLM app or `litellm` module, so the
  ADR 0002 quarantine guard stays green.
