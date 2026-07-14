---
type: readme
project: reverso
last_updated: 2026-07-14
---

# Reverso

Reverso is a personal, local LLM gateway for using provider subscriptions and credentials through standard HTTP clients. It binds only to `127.0.0.1:64946` and serves OpenAI Responses-compatible provider routes plus an inbound Anthropic Messages surface for Claude Code and the Claude Agent SDK.

Claude runs through the authenticated Claude Code CLI, Copilot forwards through the logged-in GitHub Copilot surface, Auggie runs through its CLI, and DeepSeek uses its API directly. A first-party gateway owns these routes; LiteLLM remains only as fallthrough for paths Reverso does not own. MiniMax is direct Codex-only and never routes through Reverso.

> Reverso is for one user on one macOS machine. It has no inbound authentication because loopback binding is its security boundary. Do not expose port `64946` to a network.

## Recommended path: Claude from Codex

This path uses an authenticated Claude subscription, Reverso's `/claude/v1` Responses endpoint, and a managed Codex profile. The observable success result is `REVERSO_OK` from a real model call.

### Prerequisites

- macOS
- Git and `curl`
- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with an active subscription login
- [Codex CLI](https://github.com/openai/codex) for the recommended client path

Confirm the CLIs and authenticate Claude before installing Reverso:

```bash
uv --version
claude --version
claude auth status || claude auth login
codex --version
```

### Install and start

```bash
git clone https://github.com/r3dlex/reverso-llm.git
cd reverso-llm
uv sync --frozen
./scripts/install-launchagents.sh
```

The installer writes and loads two user LaunchAgents, then stores logs under `~/Library/Logs/reverso/`. It is safe to rerun after an update.

Confirm that the loopback gateway is ready:

```bash
curl -fsS http://127.0.0.1:64946/health/readiness
```

Expected JSON includes:

```json
{"status":"healthy"}
```

### Create the managed Codex profiles

With the gateway running:

```bash
uv run reverso-codex-sync --dry-run
uv run reverso-codex-sync
```

The sync reads each live provider's `/v1/models`, updates `~/.codex/config.toml`, writes provider profiles such as `~/.codex/claude.config.toml`, and writes provider-scoped catalogs under `~/.codex/reverso/`.

Now prove the complete Codex to Reverso to Claude path:

```bash
codex exec -p claude --skip-git-repo-check \
  "Reply with exactly: REVERSO_OK"
```

Success ends with:

```text
REVERSO_OK
```

If the readiness probe passes but the model call fails, start with [Troubleshooting](#troubleshooting) and `~/Library/Logs/reverso/proxy.stderr.log`.

## How routing works

Reverso owns one loopback port and chooses a backend from the request surface and model:

| Client surface | Route | Backend |
| --- | --- | --- |
| OpenAI Responses | `/claude/v1` | Claude Code CLI with subscription OAuth |
| OpenAI Responses | `/copilot/v1` | GitHub Copilot upstream |
| OpenAI Responses | `/auggie/v1` | Auggie CLI |
| OpenAI Responses | `/deepseek/v1` | Direct DeepSeek HTTP API |
| Anthropic Messages | `/v1/messages` | Model-routed to Claude, Codex, Copilot, Auggie, or DeepSeek |

The OpenAI-compatible provider base URL ends at `/<provider>/v1`; clients append `/responses` or `/models`. The inbound Anthropic surface is translation-only: it accepts Messages requests but does not proxy them to `api.anthropic.com`. The Claude worker scrubs Reverso-related Anthropic environment variables before spawning the CLI so it cannot loop back into the gateway.

The experimental local-only Codex Direct route and opt-in OpenAI pass-through route are documented in [ADR 0016](docs/architecture/adr/0016-experimental-codex-direct-oauth-provider.md) and the [archived implementation specification](docs/specifications/ARCHIVED/openai-pass-through-oauth-api-key.md). They are not the recommended onboarding path.

## Codex model selector rules

`reverso-codex-sync` follows these invariants:

- Built-in Codex GPT model IDs remain bare and selectable, such as `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, and `gpt-4.1`.
- It adds top-level `model = "gpt-5.5"` only when the user has no top-level `model`. It never replaces an existing choice.
- Provider models are additive and live in provider-scoped profiles and catalogs. They do not replace the built-in Codex catalog.
- Collision-prone selector IDs are prefixed: `copilot/<model>`, `auggie/<model>`, and `agy/<model>`.
- MiniMax, DeepSeek, GPT from Codex, and Claude from Claude Code are not prefixed.
- Additional experimental surfaces keep their own namespaces, including `codex-direct/<model>` and `openai-pass-through/<model>`.

The catalog slug controls what Codex displays. The provider profile still sends the backend's bare model ID to its own Reverso route. This keeps provider identity visible without leaking one provider's models into another provider's catalog.

Hand-written Reverso profile files keep GPT-level model names when using the documented alias routing. For example, a DeepSeek or Claude profile may send `gpt-5.5`; the provider-specific route resolves it to the configured backend model. Generated profiles instead pin a live provider model and a matching provider-scoped catalog.

| Codex alias | DeepSeek target | Claude target | MiniMax direct |
| --- | --- | --- | --- |
| `gpt-5.5` or `gpt-5.4` | `deepseek-v4-pro` | `claude-opus-4-8` | `MiniMax-M3` |
| `gpt-5.4-mini` or `gpt-5.3-codex-spark` | `deepseek-v4-flash` | `claude-sonnet-4-6` | `MiniMax-M3` |

MiniMax is direct Codex-only. A direct profile uses `model_provider = "minimax"`, `model = "MiniMax-M3"`, `model_context_window = 512000`, and the `MINIMAX_ANTHROPIC_API_KEY` environment variable. It is not a Reverso provider.

## Managed configuration and safety

`reverso-codex-sync` owns only the regions and generated files marked as managed. It validates TOML before writing, preserves user content outside managed regions, makes timestamped backups when the base config changes, retains a bounded backup set, and archives stale managed profiles under `~/.codex/Archive/reverso-codex-sync/`. Unmarked direct OpenAI and MiniMax profiles are not overwritten.

Use `--dry-run` to inspect live discovery without writing. Custom locations are available through `--config`, `--catalog-dir`, and `--base-url`, or their `REVERSO_CODEX_CONFIG`, `REVERSO_CODEX_CATALOG_DIR`, and `REVERSO_CODEX_BASE_URL` environment variables.

Security boundaries:

- Reverso rejects any `REVERSO_HOST` other than `127.0.0.1`.
- DeepSeek secrets belong in macOS Keychain, not in this repository:

  ```bash
  ./scripts/keychain-set.sh DEEPSEEK_API_KEY "sk-..."
  ```

- Claude and Codex subscription credentials remain in their CLIs' normal local credential stores.
- Auggie runs in an ephemeral sandbox workspace because a global indexing hard-disable has not been proven. Do not treat indexing as disabled.
- Gateway and usage metrics do not intentionally store prompt or compressed text, but provider CLIs and upstream services retain their own behavior and terms.

## Update, stop, and uninstall

Update the checkout and refresh dependencies, services, and generated model catalogs:

```bash
git pull --ff-only
uv sync --frozen
./scripts/install-launchagents.sh
uv run reverso-codex-sync
```

Restart without changing files:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.reverso-proxy.plist
launchctl load ~/Library/LaunchAgents/com.user.reverso-proxy.plist
```

Uninstall the LaunchAgents:

```bash
./scripts/uninstall-launchagents.sh
```

The uninstall script removes generated user LaunchAgent files. It does not delete the repository, logs, Keychain secrets, or Codex profile files.

## Troubleshooting

| Symptom | Check | Resolution |
| --- | --- | --- |
| Readiness connection refused | `launchctl list | grep reverso` | Rerun `./scripts/install-launchagents.sh`, then inspect `~/Library/Logs/reverso/proxy.stderr.log`. |
| Gateway exits at startup | `tail -n 100 ~/Library/Logs/reverso/proxy.stderr.log` | Run `uv sync --frozen`; confirm the checkout has not moved since the LaunchAgents were installed. |
| Claude returns an auth error | `claude auth status` | Run `claude auth login` in the same user account, then retry. |
| A Codex provider profile is missing | `uv run reverso-codex-sync --dry-run` | Start the gateway, confirm `/<provider>/v1/models` responds, then rerun the sync without `--dry-run`. |
| Wrong models appear in a provider picker | Inspect `~/.codex/reverso/<provider>.json` | Do not edit generated catalogs. Rerun `uv run reverso-codex-sync`; catalogs are surface-scoped. |
| DeepSeek returns 503 | `security find-generic-password -s reverso/DEEPSEEK_API_KEY -w` | Store the key with `./scripts/keychain-set.sh`, then restart the proxy LaunchAgent. |
| A managed config edit is unexpected | Inspect `~/.codex/config.toml.reverso-sync.*` | Restore the newest backup if needed, then use `--dry-run` before syncing again. |

For a foreground traceback, stop the proxy LaunchAgent temporarily and run:

```bash
./scripts/start-fg-proxy.sh
```

The bundled `./scripts/smoke.sh` checks readiness, discovery, usage, and a live DeepSeek response. It requires a running gateway and a configured DeepSeek key, so it is an additional operator check rather than the recommended Claude onboarding smoke.

## Advanced documentation

- [Business requirements and locked decisions](docs/01-brd.md)
- [Product requirements](docs/02-prd.md)
- [Architecture and failure modes](docs/03-architecture.md)
- [MVP phases and exit criteria](docs/04-mvp.md)
- [Responses-native provider gateway](docs/architecture/adr/0002-responses-native-provider-gateway.md)
- [Single-port composition](docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md)
- [Provider-qualified Anthropic routing](docs/architecture/adr/0008-provider-qualified-model-routing.md)
- [Claude on the Anthropic surface](docs/architecture/adr/0009-claude-on-anthropic-surface.md)
- [Codex Responses parity matrix](docs/architecture/codex-responses-parity-matrix.md)
- [Anthropic surface verification](docs/anthropic-surface-verification.md)
- [Copilot picker surface separation](docs/learning/copilot-picker-surface-separation.md)

## Development and community

Install the development environment and run the local quality gates:

```bash
uv sync --frozen
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run ruff check src tests
uv run ruff format --check src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Use [GitHub Issues](https://github.com/r3dlex/reverso-llm/issues) for reproducible bugs and focused proposals, and [GitHub Pull Requests](https://github.com/r3dlex/reverso-llm/pulls) for reviewed changes. Do not include credentials, prompt content, local config, or logs containing sensitive data.

## License

This repository does not currently include a license file. No permission to copy, modify, or redistribute is granted beyond applicable law. The project policy is personal use only, for one user on one machine, and not for sharing or resale.

<!-- v3-ai-sdlc-init:start -->
## AI SDLC v3

This repo follows the v3 AI-SDLC layout (`topology_type: standalone`, depth 0). `AGENTS.md` is the single source of truth for the agent operating contract; `CLAUDE.md` and `GEMINI.md` are thin pointers to it (ADR-0014).

- Operating contract: [`AGENTS.md`](AGENTS.md)
- Workflow doc: [`.ai/workflows/repo-workflow.md`](.ai/workflows/repo-workflow.md)
- Workflow manifest: [`.ai/workflows/repo-workflow.json`](.ai/workflows/repo-workflow.json)

See `.ai/matrix.json`, `.memory/human-override/`, and `docs/architecture/adr/`. Modules at `r3dlex/skills/init-ai-repo/modules/`.
<!-- v3-ai-sdlc-init:end -->
