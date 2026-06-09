---
type: research
project: reverso
slug: auggie-sdk-spike
status: complete
goal: G002-auggie-sdk-and-indexing-feasibility
date: 2026-06-09
related:
  - .omc/plans/ralplan-auggie-deepseek-responses.md
  - .omc/plans/prd-auggie-deepseek-responses.md
  - .omc/plans/test-spec-auggie-deepseek-responses.md
  - docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md
  - .omc/research/auggie-indexing-spike.md
---

# Auggie SDK Feasibility Spike

## Headline finding

There is no Python `auggie-sdk` package. Auggie ships as the npm CLI
`@augmentcode/auggie`, a single bundled Node script (`augment.mjs`). The plan's assumed
`auggie_sdk.Auggie` Python import does not exist on this machine and is not published as a
Python distribution. The Reverso Auggie adapter must therefore use the bounded subprocess
spine (precedent `src/reverso/protocols/adapters/claude.py:26`), not a Python SDK. The
subprocess-fallback trigger has fired on the primary condition: no usable Python SDK exists.

## Observed environment

| Item | Observed value | How observed |
|---|---|---|
| CLI binary | `/opt/homebrew/bin/auggie` -> `../lib/node_modules/@augmentcode/auggie/augment.mjs` | `ls -l`, `readlink` |
| Package name | `@augmentcode/auggie` | `package.json` `name` |
| Pinned version | `0.28.0` (commit `63537d73`) | `package.json` `version`, `auggie --version` |
| Runtime | Node `/usr/bin/env node` script (`augment.mjs`) | `file` |
| Bundled deps | none declared (single bundled mjs) | `package.json` `dependencies` empty |
| License | Custom Proprietary License for Augment CLI; requires an active Augment subscription | `LICENSE.md` |
| Python `auggie-sdk` | not found | `uv pip show auggie-sdk`, `import auggie_sdk` -> ModuleNotFoundError |
| Auth artifact | `~/.augment/session.json` (OAuth), or `AUGMENT_SESSION_AUTH` env, or `--augment-session-json <json|path>` | `auggie --help` Authentication Options, `~/.augment/` listing |

## Capability matrix (CLI subprocess spine)

Reframed from "Python SDK" to "CLI subprocess + structured-output surfaces", because the
Python SDK does not exist. PASS means a documented, observed control exists; UNKNOWN means
the surface exists but end-to-end behavior was not exercised in this read-only spike (no agent
was invoked); FAIL means the capability is absent as posed.

| Capability | Status | Evidence (observed) |
|---|---|---|
| Python SDK construction (`auggie_sdk.Auggie`) | FAIL | No PyPI/installed package; `import auggie_sdk` raises ModuleNotFoundError. Auggie is an npm CLI. |
| One-shot invocation | PASS | `auggie -p/--print` "Print mode (one-shot)"; positional or `-i/--instruction`/`--instruction-file`. |
| Structured output | PASS | `--output-format json` (only with `--print`): "text (default) or json". |
| Model listing | PASS | `auggie model list --json` "Output full model metadata as JSON for downstream tooling". |
| Sessions / continuity | PASS | `-c/--continue`, `-r/--resume [sessionId]`, `--dont-save-session`; `auggie session` subcommand; sessions stored under `~/.augment/sessions`. |
| Event streaming | UNKNOWN | `--acp` (ACP mode) and `--mcp` (MCP tool server) exist as structured surfaces; whether ACP emits incremental assistant-message-chunk events suitable for Responses SSE mapping was not exercised here. Surface exists; behavior unverified. |
| Function calling / tools | PASS | `--permission tool-name:policy` (allow, deny, ask-user, webhook-policy(url), script-policy(path)), `--remove-tool`, `auggie tools` subcommand. Provider-native. |
| Mock testability (no real Auggie) | UNKNOWN (subprocess-mock path PASS-pending) | Subprocess can be mocked exactly like `adapters/claude.py` tests mock the Claude CLI; an ACP/JSON-RPC mock is also feasible. Needs a harness proof in G004/G005. |
| Auth without repo secret | PASS | OAuth session JSON at `~/.augment/session.json` or `AUGMENT_SESSION_AUTH`; `login`/`logout`/`token` subcommands. No repository-stored secret. |
| Turn bounding / timeout | PASS | `--max-turns <n>` (with `--print`), `--retry-timeout <sec>`; subprocess timeout/cleanup per claude.py precedent. |

## CLI surface inventory (auggie 0.28.0, from `auggie --help`)

- Input: `[instruction]`, `-i/--instruction`, `-if/--instruction-file`, `--queue` (requires
  `--print`), `--image`, `--file`, `--enhance-prompt`.
- Output and interaction: `-p/--print` (one-shot), `-q/--quiet`, `--output-format text|json`,
  `-a/--ask` (retrieval and non-editing tools only), `--show-cost`, `--mcp`, `--acp`.
- Configuration: `-m/--model`, `--persona`, `-w/--workspace-root`, `--add-workspace`,
  `--rules`, `--augment-cache-dir` (default `~/.augment`), `--retry-timeout`, `--max-turns`,
  `--allow-indexing`, `--wait-for-indexing`.
- Session: `-c/--continue`, `-r/--resume [sessionId]`, `--dont-save-session`.
- Auth: `--augment-session-json`, `--github-api-token`; env `AUGMENT_SESSION_AUTH`,
  `GITHUB_API_TOKEN`.
- Tools and integrations: `--mcp-config`, `--permission`, `--remove-tool`, `--shell`,
  `--startup-script(-file)`, `--plugin-dir`.
- Subcommands: `login`, `logout`, `token`, `session`, `model`, `account`, `context`,
  `upgrade`, `command`, `mcp`, `tools`.

## Decisions from this spike

### Subprocess fallback decision: SUBPROCESS is the spine

The adapter shells out to the `auggie` CLI (precedent `adapters/claude.py:26`). There is no
Python SDK to fall back from; subprocess is the only viable spine. The `-a/--ask` mode
(retrieval and non-editing tools only) is the candidate default for a read-only posture, to be
confirmed against the no-hidden-execution test in G004. Model listing uses
`auggie model list --json`. One-shot Responses turns use `--print --output-format json`. ACP
mode (`--acp`) is the candidate for incremental event streaming and is the path that must be
exercised in G005 before claiming SSE parity.

### Auth

Use the existing OAuth session (`~/.augment/session.json` / `AUGMENT_SESSION_AUTH`); never a
repository-stored secret. Missing auth or unavailable CLI must yield a bounded provider error
(tested in G004/G005).

## Open items deferred to implementation

- ACP event stream shape: exercise `--acp` to confirm assistant-message-chunk and tool-call
  events map to Responses SSE (G005). Currently UNKNOWN.
- Mock harness: prove the subprocess (and/or ACP JSON-RPC) can be driven from tests without a
  live Auggie or a real subscription (G004).
- License: proprietary, subscription-gated. No redistribution of `augment.mjs`; Reverso only
  invokes the locally installed CLI. Confirm this is acceptable for the local-only gateway use
  before shipping docs that imply bundling.
