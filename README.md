---
type: readme
project: reverso
last_updated: 2026-05-27
---

# Reverso

A subscription-backed local LLM gateway. Runs on `127.0.0.1:64946`. Wraps Claude Code CLI and Codex CLI as subprocess workers and HTTP-forwards DeepSeek. Claude and DeepSeek can be exposed to Codex through Reverso profiles. MiniMax is direct Codex-only and is not routed through Reverso.

**Personal use only.** Single user, single machine. Not for sharing or resale.

See `docs/01-brd.md` for the full rationale.

---

## Quick start

### Prerequisites

- macOS (Apple Silicon or Intel)
- `uv` installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Claude Code CLI installed: `npm install -g @anthropic-ai/claude-code`
- Codex CLI installed: `npm install -g @openai/codex`
- Both CLIs authenticated under your subscriptions

### 1. Configure Keychain secrets

Reverso reads the DeepSeek API key from macOS Keychain at startup. Store it once:

```bash
./scripts/keychain-set.sh DEEPSEEK_API_KEY "sk-..."
```

To verify the stored secret:
```bash
security find-generic-password -s reverso -a DEEPSEEK_API_KEY -w
```

MiniMax is configured directly in Codex and reads `MINIMAX_ANTHROPIC_API_KEY` from your local shell exports, not from Reverso.

Existing local Codex MiniMax profiles that pointed at Reverso must be replaced or archived. MiniMax profiles should use `model_provider = "minimax"` and `model = "MiniMax-M3"`.

### 2. Install and load the LaunchAgent

```bash
./scripts/install-launchagents.sh
```

This expands the plist template with your local paths, creates `~/Library/Logs/reverso/`, and loads the agent via `launchctl`. The gateway starts automatically at login.

To verify it is running:
```bash
curl http://127.0.0.1:64946/health/readiness
```

### 3. Configure Codex CLI providers

Add the Reverso provider endpoints for DeepSeek and Claude to `~/.codex/config.toml`:

```toml
[model_providers.reverso_deepseek]
name = "Reverso DeepSeek profile"
base_url = "http://127.0.0.1:64946/deepseek/v1"
wire_api = "responses"

[model_providers.reverso_claude]
name = "Reverso Claude profile"
base_url = "http://127.0.0.1:64946/claude/v1"
wire_api = "responses"
```

MiniMax is direct Codex-only. Configure it as a direct Codex provider instead of a Reverso profile:

```toml
[model_providers.minimax]
name = "MiniMax"
base_url = "https://api.minimax.io/v1"
env_key = "MINIMAX_ANTHROPIC_API_KEY"
wire_api = "responses"
```

Example `~/.codex/minimax.config.toml`:

```toml
model = "MiniMax-M3"
model_provider = "minimax"
model_context_window = 512000
```

Reverso profile files keep GPT-level model names. Example `~/.codex/deepseek.config.toml`:

```toml
model_provider = "reverso_deepseek"
model = "gpt-5.5"
```

Example `~/.codex/claude.config.toml`:

```toml
model_provider = "reverso_claude"
model = "gpt-5.5"
```

Reverso profile routing keeps Codex metadata stable for DeepSeek and Claude by letting Codex see GPT model names while Reverso rewrites requests after they enter a provider profile path. Do not put provider model ids in Reverso profile files. Use `model = "gpt-5.5"`, `model = "gpt-5.4"`, `model = "gpt-5.4-mini"`, or `model = "gpt-5.3-codex-spark"` in Reverso profile files and select the provider with `model_provider`. MiniMax is the exception because it is direct Codex-only and should use `model = "MiniMax-M3"`.

| Codex profile model | DeepSeek Reverso profile | Claude Reverso profile | MiniMax direct Codex | Direct Codex /v1 |
|---|---|---|---|---|
| `gpt-5.5` | `deepseek-v4-pro` | `claude-opus-4-8` | `MiniMax-M3` | `gpt-5.5` |
| `gpt-5.4` | `deepseek-v4-pro` | `claude-opus-4-8` | `MiniMax-M3` | `gpt-5.4` |
| `gpt-5.4-mini` | `deepseek-v4-flash` | `claude-sonnet-4-6` | `MiniMax-M3` | `gpt-5.4-mini` |
| `gpt-5.3-codex-spark` | `deepseek-v4-flash` | `claude-sonnet-4-6` | `MiniMax-M3` | `gpt-5.3-codex-spark` |

Use Direct Codex /v1 only for GPT-backed Codex routing. It is intentionally not a Reverso provider profile and Reverso must not rewrite GPT model names there.

---

## Smoke tests

### Test DeepSeek (HTTP-forwarded, no CLI wrapper):

```bash
curl -s http://127.0.0.1:64946/deepseek/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.5", "messages": [{"role": "user", "content": "say hello"}]}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:100])"
```

### Test Claude (subscription-backed, CLI wrapper):

```bash
curl -s http://127.0.0.1:64946/claude/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: placeholder" \
  -d '{"model": "gpt-5.5", "max_tokens": 64, "messages": [{"role": "user", "content": "say hello"}]}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['content'][0]['text'][:100])"
```

Or run the bundled smoke script:

```bash
./scripts/smoke.sh
```

---

## Decommissioning the existing codex-litellm shim

Reverso replaces the existing shim setup (`codex-litellm-responses-shim`) that previously handled MiniMax and DeepSeek on ports 48731/49731 and 48737/49737.

Once Reverso is verified working, decommission the old agents:

```bash
launchctl unload ~/Library/LaunchAgents/com.andres.codex-litellm-minimax.plist
launchctl unload ~/Library/LaunchAgents/com.andres.codex-litellm-deepseek.plist
```

Update `~/.codex/config.toml`: point DeepSeek and Claude profiles at `reverso_deepseek` or `reverso_claude` instead of legacy gateway providers. Configure MiniMax as the direct `minimax` Codex provider with `model = "MiniMax-M3"`. Keep Reverso profile `model` values as GPT names so Codex loads its own model metadata and Reverso handles provider model ids internally.

**Do not remove the old plist files until Reverso has been running stably for at least a week.**

---

## Uninstall

```bash
./scripts/uninstall-launchagents.sh
```

---

## Architecture

Two processes, both managed by launchd:

1. **LiteLLM proxy** (`com.user.reverso-proxy`) - inbound HTTP on `127.0.0.1:64946`, routing, body translation, streaming.
2. **Session daemon** (`com.user.reverso-daemon`) - owns wrapped CLI subprocesses, session table, idle detection. *(Phase 2, not yet active)*

See `docs/03-architecture.md` for the full component diagram.

---

## Development

```bash
uv sync
uv run pytest tests/
```

Start the proxy in the foreground for debugging:

```bash
./scripts/start-fg-proxy.sh
```

---

## License

Personal use. Public repository so others can adapt it for their own subscriptions. Not for commercial use.

<!-- v3-ai-sdlc-init:start -->
## AI SDLC v3
This repo follows the v3 AI-SDLC layout. See `.ai/matrix.json`, `.memory/human-override/`, and `docs/architecture/adr/`. Modules at `r3dlex/skills/ai-sdlc-init/modules/`.
<!-- v3-ai-sdlc-init:end -->
