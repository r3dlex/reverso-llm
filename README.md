---
type: readme
project: reverso
last_updated: 2026-07-30
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
- One executable `rtk` on the host `PATH`, or its exact path for
  `reverso-client-sync --rtk-bin`

Confirm the CLIs and authenticate Claude before installing Reverso:

```bash
uv --version
claude --version
claude auth status || claude auth login
codex --version
```

### Install and start

```bash
ACCOUNT_HOME="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
git clone git@github.com:r3dlex/reverso-llm.git \
  "${ACCOUNT_HOME}/.local/share/reverso"
cd "${ACCOUNT_HOME}/.local/share/reverso"
```

Run the canonical sequence in the next section to install and start Reverso.
The installer accepts only the clean canonical checkout above. It records the
exact Git revision and installer-selected `uv` launcher in
`~/Library/Application Support/reverso/deployment-provenance.json`, renders the
same checkout, revision, and launcher into both user LaunchAgents, validates the
rendered files, reloads the services, and reads back their running checkout,
revision, and launcher before returning. It stores logs under
`~/Library/Logs/reverso/`.

During a forward update, the pre-install gate accepts the previously deployed
revision only when its rendered and running LaunchAgents still agree and that
revision is a Git ancestor of the selected clean checkout. The post-restart
gate then requires the newly loaded jobs to match the selected revision before
the installer returns.

Confirm that the loopback gateway is ready:

```bash
curl -fsS http://127.0.0.1:64946/health/readiness
```

Expected JSON includes:

```json
{"status":"healthy"}
```

### Converge the managed client surfaces

With the gateway running, use this canonical sequence for both a clean install
and an update:

```bash
uv sync --frozen
./scripts/install-launchagents.sh
uv run python scripts/check-deployment-drift.py --phase pre-sync
uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/smoke.sh
./scripts/convergence-acceptance.sh
uv run python scripts/check-deployment-drift.py --phase acceptance
```

The unified sync composes the lower-level Codex and Claude Code sync
implementations and converges the host RTK discovery link. See
[`docs/client-sync.md`](docs/client-sync.md) for modes, lock behavior,
ownership rules, JSON fields, statuses, and exit codes.

The second `apply` proves idempotency. `refresh` updates the machine-readable
catalog status used by `verify`. The installer configures the short-lived
`com.user.reverso-catalog-refresh` job for 06:00 and 18:00 local time; only
`com.user.reverso-proxy` and `com.user.reverso-daemon` are long-lived.
`convergence-acceptance.sh` renders into an isolated temporary home, validates
all 17 manifest surfaces, confirms the RTK discovery symlink without executing
RTK, and reads the schema version 2 Headroom aggregate with profile `coding`
from `http://127.0.0.1:64946/usage/headroom`.

Embedded Headroom is process-local. Its counters reset when the gateway
restarts, it defaults to the `coding` profile when no explicit override is
set, it never invokes RTK, and it never reads standalone Headroom savings
files.

The Codex portion reads each live provider's `/v1/models`, updates
`~/.codex/config.toml`, writes product-scoped profiles such as
`~/.codex/reverso-claude.config.toml` and
`~/.codex/reverso-kimi.config.toml`, and writes provider-scoped catalogs under
`~/.codex/reverso/`. It validates a new managed profile before replacing a
marker-owned file and archives marker-owned legacy bare profiles only after the
new profile is durable. An unmarked `reverso-*` conflict fails closed instead of
being overwritten.

The Claude Code sync keeps global Claude settings free of Reverso routing and
installs these managed launchers under `~/.local/bin`:

| Launcher | Discovery catalog |
| --- | --- |
| `claude-reverso` | All Reverso Anthropic-surface models |
| `claude-claude` | Claude |
| `claude-codex` | Codex |
| `claude-copilot` | Copilot |
| `claude-auggie` | Auggie |
| `claude-deepseek` | DeepSeek |
| `claude-kimi` | Kimi |

Each launcher targets `http://127.0.0.1:64946`, enables gateway model discovery,
and sends `x-reverso-model-catalog` plus the launch directory in
`x-reverso-workspace`. The wrappers use a non-secret loopback placeholder token,
scrub inherited Anthropic credentials, and delegate to the real Claude Code
executable. The sync refuses to overwrite an unmarked launcher.

Now prove the complete Codex to Reverso to Claude path:

```bash
codex exec -p reverso-claude --skip-git-repo-check \
  "Reply with exactly: REVERSO_OK"
```

The managed provider uses Codex's fixed `experimental_bearer_token` setting so
GUI and shell sessions both send a standard Bearer header without requiring an
environment variable. The value is a non-secret loopback placeholder: Reverso
remains loopback-only and does not validate it in v1, while Claude upstream
authentication still uses the local subscription OAuth artifact.

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

### Headroom topology and optional standalone services

Reverso installs `headroom-ai[all]==0.32.1` in its locked runtime. Compression is
embedded in the Reverso process on `127.0.0.1:64946`; it does not open a separate
Headroom port. Verify its process-local metrics through
`http://127.0.0.1:64946/usage/headroom`.

The local operator may also keep standalone Headroom services for clients that
do not use Reverso. That separate topology uses:

| Listener | Purpose |
| --- | --- |
| `127.0.0.1:58787` | Shared Codex and Claude cache-mode proxy |
| `127.0.0.1:58788` | MiniMax token-mode proxy |
| `127.0.0.1:58789` | DeepSeek token-mode proxy |
| `127.0.0.1:8787` | Headroom's upstream default, not used by the local managed services above |

Install or update the standalone Headroom CLI independently of Reverso:

```bash
uv tool install --python 3.13 "headroom-ai[all]"
uv tool upgrade headroom-ai
headroom doctor
```

Standalone services are optional and operator-managed. They do not replace
Reverso's embedded Headroom seam or change Reverso's loopback-only listener.
Run each required service under the local service manager, or in a separate
terminal for an interactive setup:

```bash
# Shared Codex and Claude cache-mode listener.
HEADROOM_NET_COST_POLICY=1 headroom proxy \
  --host 127.0.0.1 --port 58787 --mode cache --no-telemetry

# MiniMax token-mode listener. Requires the local Responses shim on 64947.
OPENAI_TARGET_API_URL=http://127.0.0.1:64947 headroom proxy \
  --host 127.0.0.1 --port 58788 --mode token --no-telemetry

# DeepSeek token-mode listener. Routes through Reverso on 64946.
OPENAI_TARGET_API_URL=http://127.0.0.1:64946/deepseek headroom proxy \
  --host 127.0.0.1 --port 58789 --mode token --no-telemetry
```

These commands contain no provider credential. Reverso and the local MiniMax
shim retain responsibility for their own secret injection.

## Codex model selector rules

`reverso-codex-sync` follows these invariants:

- Built-in Codex GPT model IDs remain bare and selectable, such as `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, and `gpt-4.1`.
- It adds top-level `model = "gpt-5.5"` only when the user has no top-level `model`. It never replaces an existing choice.
- Provider models are additive and live in provider-scoped profiles and catalogs. They do not replace the built-in Codex catalog.
- Collision-prone selector IDs are prefixed: `copilot/<model>`, `auggie/<model>`, and `agy/<model>`.
- MiniMax, DeepSeek, GPT from Codex, and Claude from Claude Code are not prefixed.
- Additional experimental surfaces keep their own namespaces, including `codex-direct/<model>` and `openai-pass-through/<model>`.

The generated provider profile default matches its catalog slug. Collision-prone
profiles therefore send their governed selector ID to their Reverso route, and
the gateway removes the matching prefix before adapter dispatch. This keeps
provider identity visible without leaking one provider's models into another
provider's catalog.

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
- Prompt content may be retained in process memory for response chaining. Reverso does not intentionally persist prompt or compressed text to disk or metrics, but provider CLIs and upstream services retain their own behavior and terms.

## Ollama inventory refresh and opt-out

Ollama uses one prompt-free inventory snapshot for the generated Codex profile,
Codex catalog, and Claude Code launcher. The `provider-ollama` group commits
those marker-owned paths atomically. Background refresh never signs in, reads
Ollama device identity or API keys, manages the Ollama daemon, or pulls models.
An authentication failure, timeout, or malformed Cloud result keeps only prior
marker-owned Cloud rows as stale Cloud state and reports partial freshness.

Use `REVERSO_OLLAMA_CLOUD=0` or `OLLAMA_NO_CLOUD=1` for absolute Cloud opt-out.
This produces current local-only inventory and performs no Cloud discovery. See
[the client convergence guide](docs/client-sync.md) for restore and uninstall
boundaries.

## Update, stop, and uninstall

Update the checkout and refresh dependencies, services, and generated model catalogs:

```bash
git pull --ff-only
uv sync --frozen
./scripts/install-launchagents.sh
uv run python scripts/check-deployment-drift.py --phase pre-sync
uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/smoke.sh
./scripts/convergence-acceptance.sh
uv run python scripts/check-deployment-drift.py --phase acceptance
```

The drift command fails closed when the source checkout, recorded provenance,
rendered or running LaunchAgents, live Kimi discovery, generated profile, or
generated catalog disagree. `pre-sync` requires live discovery to expose only
`kimi-k3`; `acceptance` additionally requires the generated Kimi profile and
catalog to use context window `1048576`.

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
| A Codex provider profile is missing | `uv run reverso-client-sync verify --json` | Start the gateway, confirm `/<provider>/v1/models` responds, then run the unified dry-run and apply sequence. |
| Wrong models appear in a provider picker | Inspect `~/.codex/reverso/<provider>.json` | Do not edit generated catalogs. Run the unified dry-run, apply, and verify sequence; catalogs are surface-scoped. |
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
uv sync --extra dev
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uvx prek run --all-files
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
