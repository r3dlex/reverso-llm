---
type: readme
project: reverso
last_updated: 2026-05-27
---

# Reverso

A subscription-backed local LLM gateway. Runs on `127.0.0.1:64946`. Serves four first-party, path-prefixed Responses endpoints on that single port: `/claude/v1`, `/copilot/v1`, `/auggie/v1`, and `/deepseek/v1`. Claude, Copilot, and Auggie are CLI subprocess workers; DeepSeek is a first-party adapter that calls the DeepSeek API directly (it is no longer routed through LiteLLM). All four can be exposed to Codex through Reverso provider profiles. MiniMax is direct Codex-only and is not routed through Reverso.

The composition root (`reverso.proxy.compose`) owns port 64946: it routes the four first-party prefixes to the Responses gateway and delegates every other path to the legacy LiteLLM stack as a fallthrough. See `docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md`.

Reverso also serves an inbound Anthropic Messages API surface (`/v1/messages`) on the same port for clients like Claude Code and the Claude Agent SDK pointed at Reverso via `ANTHROPIC_BASE_URL`. It is inbound only: Reverso does not call `api.anthropic.com`. Messages requests are model-routed by default to the claude, copilot, deepseek, and auggie backends through a single first-party authority (with optional per-profile prefixes such as `/deepseek/v1/messages`); the claude backend is served first-party via the local `claude` CLI under subscription OAuth (ADR 0009, superseding ADR 0006 D2). The spawned CLI has `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` scrubbed from its environment so it reaches `api.anthropic.com` directly and never loops back into Reverso. See `docs/architecture/adr/0009-claude-on-anthropic-surface.md`.

Milestone 2 also serves gpt-* models (gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1) on this Anthropic surface through a first-party Codex backend. The Codex backend invokes the local Codex CLI under the ChatGPT/Codex OAuth subscription (not an OpenAI API key) and converts its Responses-shaped output to Anthropic Messages by reusing the Milestone 1 translation layer. It is Anthropic-surface-only, the symmetric mirror of the claude backend being Responses-surface-only; gpt-* is never reachable on the Responses surface. See `docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`.

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

Example direct `~/.codex/minimax.config.toml`:

```toml
model = "MiniMax-M3"
model_provider = "minimax"
model_context_window = 512000
```

`reverso-codex-sync` writes one provider-name profile file per Reverso-routed
provider beside `~/.codex/config.toml`: `claude.config.toml`,
`copilot.config.toml`, `auggie.config.toml`, and `deepseek.config.toml`. Each
file pins the Reverso provider and points Codex at a provider-scoped catalog. Reverso profile files keep GPT-level model names where those names are the Codex-facing contract for a provider.
Example generated `~/.codex/deepseek.config.toml`:

```toml
model = "deepseek-v4-pro"
model_provider = "reverso_deepseek"
model_catalog_json = "/Users/you/.codex/reverso/deepseek.json"
```

Example generated `~/.codex/claude.config.toml`:

```toml
model = "claude-fable-5"
model_provider = "reverso_claude"
model_catalog_json = "/Users/you/.codex/reverso/claude.json"
```

The DeepSeek and Claude adapters still accept GPT-level names because Reverso
resolves them to the concrete provider model id for that prefix. The generated
provider-name profile files may pin real provider model ids so the provider
catalog remains scoped to that profile. On the first-party `/deepseek/v1` path
the DeepSeek adapter performs this resolution itself (it no longer goes through
the legacy `ProfileRoutingMiddleware`), so existing hand-written `model =
"gpt-5.5"` profiles keep working unchanged.

Auggie does not use GPT-level aliases. Its models come from `auggie model list`, so set the Auggie profile `model` to a real Auggie model id. Discover the available ids with `curl http://127.0.0.1:64946/auggie/v1/models`. Example `~/.codex/auggie.config.toml`:

```toml
model_provider = "reverso_auggie"
model = "<id from auggie model list>"
```

Auggie indexing caveat: the Phase 1 spike could not prove a global per-invocation hard-disable for `auggie --print` indexing, so Reverso defaults every Auggie turn to an ephemeral sandbox workspace root (never your caller workspace) and the `/auggie/v1/models` metadata carries the literal caveat `hard-disable unproven`. Do not rely on indexing being disabled; rely on the sandbox isolation.

DeepSeek first-party modes: because `/deepseek/v1` no longer inherits the legacy LiteLLM `drop_params` stripping, `response_format` (JSON mode) reaches DeepSeek unchanged and `reasoning_content` (thinking mode) is preserved on the response and carried forward across a `previous_response_id` chain.

Reverso profile routing keeps Codex metadata stable for hand-written DeepSeek
and Claude profiles by accepting GPT-level names after they enter a provider
profile path. The generated sync profiles use provider-scoped catalogs and pin
their default to a model id returned by that provider's `/v1/models`. MiniMax
is direct Codex-only and should use `model = "MiniMax-M3"`.

`reverso-codex-sync` also feeds Codex's static `/model` picker. It always keeps
built-in GPT (Codex) defaults selectable as bare model ids and inserts
top-level `model = "gpt-5.5"` into the base config only when the user has not
already selected another model. Reverso provider models are isolated to their
provider profile files and provider-scoped catalogs; the base `config.toml`
does not get generated `[profiles.*]`, root `model_catalog_json`, NUX, or
global Reverso model-list entries. The sync tool also preserves direct
`openai.config.toml` and `minimax.config.toml` as Codex-provider profiles, not
Reverso routes.

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


### Inspect Headroom savings and rollback

Headroom compression is enabled by default for Reverso-owned Responses and
Anthropic Messages dispatch. Runtime usage is aggregate-only and prompt-free:

```bash
curl -s http://127.0.0.1:64946/usage/headroom \
  | python3 -m json.tool

curl -s http://127.0.0.1:64946/usage \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['headroom'])"
```

To disable compression, set `REVERSO_HEADROOM_ENABLED=0` in the gateway process
environment and restart the LaunchAgent. The `/usage/headroom` response reports
`"enabled": false` after the restart. Re-enable by removing the variable or
setting it to `1` and restarting again. Metrics reset on process restart and never
store prompt text or compressed text.

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

Update `~/.codex/config.toml`: point DeepSeek and Claude profiles at `reverso_deepseek` or `reverso_claude` instead of legacy gateway providers. Configure MiniMax as the direct `minimax` Codex provider with `model = "MiniMax-M3"`. Hand-written DeepSeek and Claude Reverso profiles may keep GPT alias `model` values so Codex loads its own model metadata and Reverso resolves provider ids internally; generated provider-name profile files may pin provider-scoped model ids because their catalogs are scoped to that profile.

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

This repo follows the v3 AI-SDLC layout (`topology_type: standalone`, depth 0). `AGENTS.md` is the single source of truth for the agent operating contract; `CLAUDE.md` and `GEMINI.md` are thin pointers to it (ADR-0014).

- Operating contract: [`AGENTS.md`](AGENTS.md)
- Workflow doc: [`.ai/workflows/repo-workflow.md`](.ai/workflows/repo-workflow.md)
- Workflow manifest: [`.ai/workflows/repo-workflow.json`](.ai/workflows/repo-workflow.json)

See `.ai/matrix.json`, `.memory/human-override/`, and `docs/architecture/adr/`. Modules at `r3dlex/skills/init-ai-repo/modules/`.
<!-- v3-ai-sdlc-init:end -->
