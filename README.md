---
type: readme
project: reverso
last_updated: 2026-05-27
---

# Reverso

A subscription-backed local LLM gateway. Runs on `127.0.0.1:64946`. Serves four first-party, path-prefixed Responses endpoints on that single port: `/claude/v1`, `/copilot/v1`, `/auggie/v1`, and `/deepseek/v1`. Claude, Copilot, and Auggie are CLI subprocess workers; DeepSeek is a first-party adapter that calls the DeepSeek API directly (it is no longer routed through LiteLLM). All four can be exposed to Codex through Reverso provider profiles. MiniMax is direct Codex-only and is not routed through Reverso.

The composition root (`reverso.proxy.compose`) owns port 64946: it routes the four first-party prefixes to the Responses gateway and delegates every other path to the legacy LiteLLM stack as a fallthrough. See `docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md`.

Reverso also serves an inbound Anthropic Messages API surface (`/v1/messages`) on the same port for clients like Claude Code and the Claude Agent SDK pointed at Reverso via `ANTHROPIC_BASE_URL`. It is inbound only: Reverso does not call `api.anthropic.com`. Messages requests are model-routed by default to the copilot, deepseek, and auggie backends through a single first-party authority (with optional per-profile prefixes such as `/deepseek/v1/messages`); the claude backend is excluded because Claude Code talking to its own backend is circular. See `docs/architecture/adr/0006-anthropic-messages-api-surface.md`.

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

Add the Reverso provider endpoints to `~/.codex/config.toml`. Each is a first-party Responses prefix on the single port `127.0.0.1:64946`:

```toml
[model_providers.reverso_deepseek]
name = "Reverso DeepSeek profile"
base_url = "http://127.0.0.1:64946/deepseek/v1"
wire_api = "responses"

[model_providers.reverso_claude]
name = "Reverso Claude profile"
base_url = "http://127.0.0.1:64946/claude/v1"
wire_api = "responses"

[model_providers.reverso_auggie]
name = "Reverso Auggie profile"
base_url = "http://127.0.0.1:64946/auggie/v1"
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

The DeepSeek and Claude profiles keep GPT-level names because Reverso resolves them to the concrete provider model id for that prefix. On the first-party `/deepseek/v1` path the DeepSeek adapter performs this resolution itself (it no longer goes through the legacy `ProfileRoutingMiddleware`), so existing `model = "gpt-5.5"` profiles keep working unchanged. Real DeepSeek ids (for example `deepseek-v4-pro`) also pass through unchanged if you prefer to set them directly.

Auggie does not use GPT-level aliases. Its models come from `auggie model list`, so set the Auggie profile `model` to a real Auggie model id. Discover the available ids with `curl http://127.0.0.1:64946/auggie/v1/models`. Example `~/.codex/auggie.config.toml`:

```toml
model_provider = "reverso_auggie"
model = "<id from auggie model list>"
```

Auggie indexing caveat: the Phase 1 spike could not prove a global per-invocation hard-disable for `auggie --print` indexing, so Reverso defaults every Auggie turn to an ephemeral sandbox workspace root (never your caller workspace) and the `/auggie/v1/models` metadata carries the literal caveat `hard-disable unproven`. Do not rely on indexing being disabled; rely on the sandbox isolation.

DeepSeek first-party modes: because `/deepseek/v1` no longer inherits the legacy LiteLLM `drop_params` stripping, `response_format` (JSON mode) reaches DeepSeek unchanged and `reasoning_content` (thinking mode) is preserved on the response and carried forward across a `previous_response_id` chain.

Reverso profile routing keeps Codex metadata stable for DeepSeek and Claude by letting Codex see GPT model names while Reverso rewrites requests after they enter a provider profile path. Do not put provider model ids in Reverso profile files. Use `model = "gpt-5.5"`, `model = "gpt-5.4"`, `model = "gpt-5.4-mini"`, or `model = "gpt-5.3-codex-spark"` in Reverso profile files and select the provider with `model_provider`. MiniMax is the exception because it is direct Codex-only and should use `model = "MiniMax-M3"`.

`reverso-codex-sync` also feeds Codex's static `/model` picker. It always keeps built-in GPT (Codex) defaults selectable as bare model ids and inserts top-level `model = "gpt-5.5"` only when the user has not already selected another model. Reverso provider models are additive: possible collisions are selector/catalog-qualified as `copilot/<model>`, `auggie/<model>`, or `agy/<model>`, while MiniMax, DeepSeek, GPT (Codex), and Claude (Claude Code) remain bare. This prevents Reverso-discovered models from superseding the built-in Codex provider names.

| Codex profile model | DeepSeek Reverso profile | Claude Reverso profile | MiniMax direct Codex | Direct Codex /v1 |
|---|---|---|---|---|
| `gpt-5.5` | `deepseek-v4-pro` | `claude-opus-4-8` | `MiniMax-M3` | `gpt-5.5` |
| `gpt-5.4` | `deepseek-v4-pro` | `claude-opus-4-8` | `MiniMax-M3` | `gpt-5.4` |
| `gpt-5.4-mini` | `deepseek-v4-flash` | `claude-sonnet-4-6` | `MiniMax-M3` | `gpt-5.4-mini` |
| `gpt-5.3-codex-spark` | `deepseek-v4-flash` | `claude-sonnet-4-6` | `MiniMax-M3` | `gpt-5.3-codex-spark` |

Use Direct Codex /v1 only for GPT-backed Codex routing. It is intentionally not a Reverso provider profile and Reverso must not rewrite GPT model names there.

---

## Smoke tests

All four providers are Responses-native: smoke them at `/<provider>/v1/responses` with an `input` field, not at `/chat/completions` or `/messages`.

### Test DeepSeek (first-party, direct API):

```bash
curl -s http://127.0.0.1:64946/deepseek/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.5", "input": "say hello"}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['output'][0]['content'][0]['text'][:100])"
```

### Test Claude (subscription-backed, CLI wrapper):

```bash
curl -s http://127.0.0.1:64946/claude/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.5", "input": "say hello"}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['output'][0]['content'][0]['text'][:100])"
```

### Test Auggie (subscription-backed, CLI wrapper):

```bash
# Discover available Auggie model ids first.
curl -s http://127.0.0.1:64946/auggie/v1/models \
  | python3 -c "import json,sys; print([m['id'] for m in json.load(sys.stdin)['data']])"
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

1. **Gateway proxy** (`com.user.reverso-proxy`) - inbound HTTP on `127.0.0.1:64946`. The composition root (`reverso.proxy.compose`) routes the first-party prefixes `/claude`, `/copilot`, `/auggie`, and `/deepseek` to the Responses gateway (`reverso.protocols.responses_app`) and delegates every other path to the legacy LiteLLM stack. The gateway handles routing, body translation, and streaming for first-party providers.
2. **Session daemon** (`com.user.reverso-daemon`) - owns wrapped CLI subprocesses, session table, idle detection. *(Phase 2, not yet active)*

See `docs/03-architecture.md` and `docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md` for the full component diagram and the composition decision.

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
