---
type: specification
project: reverso
title: Installation and client catalog convergence
status: active
date: 2026-07-25
---

# Installation and client catalog convergence

## Goal

A clean Reverso installation or update must converge the gateway runtime,
Codex profiles and catalogs, Claude Code launchers, and embedded Headroom
dependency onto one documented, verifiable revision without overwriting
unmanaged user files.

## Requirements

### Reverso runtime

- The canonical clean checkout is the only source accepted by the LaunchAgent
  installer.
- Deployment provenance, rendered LaunchAgents, and running processes must
  agree on the exact merged Git revision.
- Reverso remains loopback-only on `127.0.0.1:64946`.

### Codex profiles and catalogs

- Generated Reverso-routed profiles use the
  `~/.codex/reverso-<provider>.config.toml` naming contract.
- Direct OpenAI and MiniMax profiles keep their existing unprefixed names.
- Provider catalogs remain under `~/.codex/reverso/<provider>.json`.
- The sync validates and atomically writes a canonical prefixed profile before
  archiving a marker-owned legacy bare profile.
- An unmarked canonical prefixed file fails closed and is never overwritten.
- Kimi discovery and generated artifacts expose only `kimi-k3` with context
  window `1048576`.

### Claude Code launchers

- `reverso-claude-code-sync` owns the marker-managed launchers
  `claude-reverso`, `claude-claude`, `claude-codex`, `claude-copilot`,
  `claude-auggie`, `claude-deepseek`, and `claude-kimi` under
  `~/.local/bin`.
- Every launcher pins the resolved real Claude Code executable, targets
  `http://127.0.0.1:64946`, enables gateway model discovery, and sends the
  scoped `x-reverso-model-catalog` plus `x-reverso-workspace: $PWD`.
- Launchers use a non-secret loopback placeholder token, scrub inherited
  Anthropic credentials, and refuse caller settings overrides that could
  replace the managed gateway configuration.
- Writes are atomic, mode `0755`, idempotent, and limited to marker-owned files.

### Headroom

- The locked Reverso environment installs `headroom-ai[all]==0.32.1`.
- Reverso uses Headroom in-process on its existing `64946` listener and opens
  no additional listener.
- Optional standalone Headroom services are documented separately at the local
  operator-assigned ports `58787`, `58788`, and `58789`; upstream Headroom's
  default `8787` is not a Reverso listener.
- The standalone examples specify cache mode for `58787`, token mode plus the
  local MiniMax Responses shim on `64947` for `58788`, and token mode plus the
  Reverso DeepSeek surface on `64946` for `58789`.

## Installation and update contract

After the gateway is installed or restarted, documentation runs both sync
commands in dry-run mode and then in apply mode:

```bash
uv run reverso-codex-sync --dry-run
uv run reverso-codex-sync
uv run reverso-claude-code-sync --dry-run
uv run reverso-claude-code-sync
```

## Acceptance

- Unit tests prove managed migration, unmarked conflict preservation, wrapper
  content, wrapper permissions, dry-run behavior, and idempotency.
- Integration tests prove deployment drift checks use the canonical prefixed
  Kimi profile.
- Unit and integration suites, Ruff, Prek, compileall, architecture checks,
  diff checks, secret scans, and debug-marker scans pass.
- Independent architecture and code reviews approve the exact commit.
- Hosted checks are green and all review threads are resolved.
- Merge occurs only under a fresh run-specific host-policy verdict that
  explicitly authorizes it.
- The exact merged revision is deployed and live Codex, Claude Code, Kimi K3,
  and Headroom acceptance passes.
