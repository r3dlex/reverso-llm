---
title: Claude Code with the Kimi provider
status: active
---

# Claude Code with the Kimi provider

Use the provider-pinned launcher when Claude Code should send Messages requests
to Kimi through Reverso. It sets process-local environment variables and does
not write `~/.claude/settings.json` or any other global Claude Code setting.

## Prerequisites

1. Install and log in with the Kimi CLI so its OAuth artifact is available.
2. Install and start Reverso on `127.0.0.1:64946`.
3. Install Claude Code and keep its normal Anthropic login intact.
4. Confirm authenticated live Kimi discovery:

   ```bash
   curl -fsS http://127.0.0.1:64946/kimi/v1/models
   ```

   Continue only when the response has `model_discovery_source` set to `live`.
   Choose a bare model id from `data[].id`; do not use `kimi/<model>` or an
   `anthropic-kimi-*` discovery alias as the provider-pinned model argument.

## Run Claude Code

The default model is `kimi-k2.5`:

```bash
./scripts/claude-kimi.sh
```

Select a different live bare Kimi model for one invocation:

```bash
REVERSO_KIMI_MODEL=kimi-k2-thinking ./scripts/claude-kimi.sh
```

The launcher rejects forwarded `--model` and `--fallback-model` options so the
validated `REVERSO_KIMI_MODEL` remains the single model authority.

The launcher sets:

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:64946/kimi
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
```

Claude Code appends `/v1/messages`, producing the final request URL
`http://127.0.0.1:64946/kimi/v1/messages`. The launcher also sends the current
working directory through the established `x-reverso-workspace` header and
uses a non-secret loopback auth placeholder because Reverso performs Kimi OAuth
resolution itself. It removes any inherited `ANTHROPIC_API_KEY` or
`CLAUDE_CODE_OAUTH_TOKEN` from the child environment so a real Anthropic secret
is never sent to the loopback gateway.

The root Anthropic surface continues to support `kimi/<model>` and the
`anthropic-kimi-<model>` discovery alias. Those are secondary routing forms;
the launcher intentionally uses the path-pinned provider URL and a bare model.

## Update

After updating Reverso, restart its gateway and repeat the live discovery check
before changing `REVERSO_KIMI_MODEL`. The launcher is repository-owned and needs
no installation into the Claude settings file.

## Rollback

Exit the provider-pinned Claude Code session and run the normal `claude` command.
No settings restore is required because `scripts/claude-kimi.sh` does not write
global settings. If historical Reverso overrides are present in
`~/.claude/settings.json`, preview the existing cleanup-only repair and then
apply it:

```bash
uv run reverso-claude-code-sync --dry-run
uv run reverso-claude-code-sync
```

The cleanup tool makes a timestamped backup before changing an existing settings
file. It remains cleanup-only and is not used to install the Kimi launcher.
