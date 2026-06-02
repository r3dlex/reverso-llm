---
type: readme
project: reverso
last_updated: 2026-05-27
---

# Reverso

A subscription-backed local LLM gateway. Runs on `127.0.0.1:64946`. Wraps Claude Code CLI and Codex CLI as subprocess workers and HTTP-forwards DeepSeek and MiniMax. Exposes standard OpenAI and Anthropic HTTP APIs on the loopback interface.

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

Reverso reads API keys for DeepSeek and MiniMax from macOS Keychain at startup. Store them once:

```bash
./scripts/keychain-set.sh DEEPSEEK_API_KEY   "sk-..."
./scripts/keychain-set.sh MINIMAX_API_KEY     "your-minimax-key"
```

To verify stored secrets:
```bash
security find-generic-password -s reverso -a DEEPSEEK_API_KEY -w
security find-generic-password -s reverso -a MINIMAX_API_KEY -w
```

### 2. Install and load the LaunchAgent

```bash
./scripts/install-launchagents.sh
```

This expands the plist template with your local paths, creates `~/Library/Logs/reverso/`, and loads the agent via `launchctl`. The gateway starts automatically at login.

To verify it is running:
```bash
curl http://127.0.0.1:64946/health/live
```

### 3. Configure Codex CLI to use Reverso

Add the provider endpoints to `~/.codex/config.toml`:

```toml
[model_providers.reverso_minimax]
name = "Reverso MiniMax profile"
base_url = "http://127.0.0.1:64946/minimax/v1"
wire_api = "responses"

[model_providers.reverso_deepseek]
name = "Reverso DeepSeek profile"
base_url = "http://127.0.0.1:64946/deepseek/v1"
wire_api = "responses"

[model_providers.reverso_claude]
name = "Reverso Claude profile"
base_url = "http://127.0.0.1:64946/claude/v1"
wire_api = "responses"
```

Codex 0.134+ profile files can then keep GPT-level model names. Example `~/.codex/minimax.config.toml`:

```toml
model_provider = "reverso_minimax"
model = "gpt-5.5"
```

Example `~/.codex/deepseek.config.toml`:

```toml
model_provider = "reverso_deepseek"
model = "gpt-5.5"
```

Example `~/.codex/claude.config.toml`:

```toml
model_provider = "reverso_claude"
model = "gpt-5.5"
```

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

### Test MiniMax (HTTP-forwarded):

```bash
curl -s http://127.0.0.1:64946/minimax/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.5", "messages": [{"role": "user", "content": "say hello"}]}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:100])"
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

Update `~/.codex/config.toml`: point your active profiles at `reverso_minimax`, `reverso_deepseek`, or `reverso_claude` instead of the legacy `minimax_gateway` / `deepseek_gateway` providers.

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
