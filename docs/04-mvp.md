---
type: mvp-plan
project: reverso
---

# MVP Scope and Phased Implementation Plan
## Reverso Gateway

**Document version.** 0.2 (draft)

**Companion documents.** `01-brd.md`, `02-prd.md`, `03-architecture.md`.

**Honest framing.** v1-full (sessions + tool interception, locked in at Q18) is realistically four to six weekends of focused work. This document phases that work into stages, each of which produces something testable and useful on its own. Skipping ahead is allowed, but each phase has explicit exit criteria so you know when the previous phase is actually done.

---

## Phase 0: Spike (one evening, possibly two)

**Goal.** Resolve the six open spike questions from the BRD before committing to architecture details that depend on them.

**Activities.**

- **Q-Spike-1.** Invoke `claude -p "say hello"` and inspect the output format. Determine: does it produce structured JSON when given a flag? Does it support some form of session continuity flag (e.g., `--resume <id>`)? What does its stdout look like when it runs a Read tool? When it runs an Edit? When it runs Bash?
- **Q-Spike-2.** Same for `codex exec` or whatever Codex CLI's non-interactive mode is called as of the current version.
- **Q-Spike-3.** From Q-Spike-1: assess parsing fragility. Is the tool-call output a JSON line we can split? Is it ANSI-decorated terminal output we have to clean up? Is it interleaved with prose so it's ambiguous where the tool call starts?
- **Q-Spike-4.** Same for Codex CLI.
- **Q-Spike-5.** Run `claude` in two different directories. Confirm that the workspace context (the `.claude/` directory or equivalent) is per-directory and that running in `/tmp/test1` does not pollute `/tmp/test2`.
- **Q-Spike-6.** Same for Codex CLI.

**Deliverables.**

- A short notes file (`docs/spike-notes.md`, not committed) recording answers and observed quirks.
- A go/no-go decision on v1-full scope. If spike reveals tool-use parsing is genuinely impossible (e.g., one CLI emits no structured tool output and the other only emits half-rendered TUI), scope-revise to v1-small (sessions only, no interception) before Phase 1.

**Exit criteria.**

- For each wrapped CLI, you know (a) how to invoke it non-interactively, (b) how to provide it with a user prompt, (c) what its output format looks like for plain text and for tool-use cases, (d) whether per-workspace isolation works as expected.
- You have either confirmed v1-full is feasible or decided to drop tool interception from v1.

---

## Phase 1: Skeleton (one weekend)

**Goal.** A working gateway that handles HTTP-forwarded provider (DeepSeek) and does *stateless* wrapped-CLI provider calls (no sessions, one subprocess per request). End-to-end usable for single-turn requests.

**In scope.**

- Repository skeleton per Architecture Section 7.2.
- `models.yaml` with the planned Reverso entries.
- `config.yaml` with default values.
- `litellm_config.yaml` that references `models.yaml`.
- LiteLLM custom providers (`anthropic_cli_provider.py`, `openai_cli_provider.py`) that spawn the wrapped CLI per-request, capture output, return final assistant text. No session reuse, no observation parsing.
- The HTTP-forwarded provider DeepSeek configured natively in LiteLLM, no custom code.
- The `x_gateway` envelope on responses (with `observations: []` and `session_id: null` for now).
- launchd LaunchAgents (one for LiteLLM only; daemon comes in Phase 2).
- Keychain integration for secrets.
- Codex CLI profiles configured for Reverso providers plus direct MiniMax.
- Claude Code `ANTHROPIC_BASE_URL` configured.
- Smoke tests for both inbound surfaces.
- `README.md` with quick-start instructions.

**Out of scope (deferred to Phase 2).**

- Session daemon.
- Long-lived subprocess sessions.
- Tool-use observation parsing.
- Recycle sweeper.

**Exit criteria.**

- `codex -p anthropic "say hello"` produces a response from Claude Code.
- `codex -p deepseek "say hello"` produces a response from DeepSeek.
- `claude --model gpt-5-codex "say hello"` (or similar Codex CLI model) produces a response from Codex CLI through the gateway.
- `claude --model deepseek-reasoner "say hello"` produces a response from DeepSeek.
- Smoke tests pass.
- Gateway survives Mac mini reboot.

---

## Phase 2: Session Daemon (one to two weekends)

**Goal.** Long-lived sessions per (machine, workspace, provider). Multi-turn conversations work. No tool-use observation yet.

**In scope.**

- Session daemon process: separate Python application, runs as own launchd LaunchAgent.
- Internal HTTP API over Unix-domain socket.
- Session table data structure with the lookup, insert, remove operations.
- Subprocess lifecycle: spawn, send turn, capture output, detect turn completion, return result.
- Per-session asyncio lock to serialize concurrent requests for the same session.
- Recycle sweeper: 60-minute timer that walks the session table, checks idle conditions, terminates stale sessions.
- Idle detection logic: 30 minutes since last request AND no live descendant processes. Uses `psutil` for process tree walking.
- Workspace conflict detection: scan for other `claude`/`codex` processes at session spawn, emit warning to log.
- LiteLLM custom providers updated to call the daemon over UDS instead of spawning subprocesses themselves.
- `x_gateway.session_id` populated correctly.
- Workspace extraction from `x_gateway.workspace` request extension, with default fallback.
- Multi-turn integration tests: send three sequential prompts to the same workspace, verify session continuity.

**Out of scope (deferred to Phase 3).**

- Tool-use observation parsing.
- `x_gateway.observations` population.
- Inbound tool declaration translation.

**Exit criteria.**

- A three-turn conversation through `codex -p anthropic` correctly maintains context across turns (asking "remember my name is X" then "what is my name" works).
- Idle sessions are terminated by the sweeper after 30+ minutes.
- Sessions with live child processes are *not* terminated (test by running a long sleep as a tool action).
- Workspace conflict warnings appear in logs when running direct `claude` in the same workspace as a gateway session.
- Both launchd LaunchAgents survive Mac mini reboot in the correct order.
- Daemon crash recovery works: kill daemon, launchd restarts it, LiteLLM custom providers reconnect cleanly on next request.

---

## Phase 3: Tool-Use Interception (one to two weekends)

**Goal.** Parse tool-call events from wrapped CLI output, populate `x_gateway.observations`. The hardest phase; allocated the most uncertainty.

**In scope.**

- Per-CLI output parsers: `parsers/claude_code.py` and `parsers/codex_cli.py`. Each reads the wrapped CLI's stdout incrementally, identifying tool-call events.
- Observation object construction (per PRD F-ENV-3 shape).
- `x_gateway.observations` populated on every response.
- `x_gateway.warnings` populated when inbound tool declarations cannot be mapped.
- `tool_mappings.yaml` with initial mappings (file read, file edit, shell command).
- Integration tests verifying observations match actual filesystem changes.
- Error path testing: wrapped CLI crashes mid-turn, partial observations are returned with HTTP 5xx (PRD F-ENV-4).

**Out of scope.**

- IV-strict mode (pre-execution mediation). Stays in v2.
- Streaming of individual observations as they happen. Observations are emitted in one batch at end of turn.

**Exit criteria.**

- A turn that runs a Read, an Edit, and a Bash produces three observations in `x_gateway.observations`.
- Each observation correctly identifies the tool, args, and a short result summary.
- The observation count matches what the developer manually counts when looking at the wrapped CLI's terminal output.
- HTTP 5xx responses still contain populated observations up to the point of failure.
- Inbound tool declarations from at least one third-party agent (e.g., Aider) are either correctly mapped or appear in `x_gateway.warnings`.

---

## Phase 4: Hardening (ongoing)

**Goal.** Production-grade reliability for personal daily use.

**In scope.**

- Per-turn timeout (default 5 minutes) with structured 504 response.
- Comprehensive structured logging at INFO level for all lifecycle events.
- Log rotation via macOS `newsyslog.d` config.
- `GET /v1/models` endpoint returning capability metadata from the registry.
- `gitleaks` pre-commit hook installed via `scripts/install-hooks.sh`.
- Integration tests that exercise both inbound surfaces with all Reverso backends.
- Documentation: `docs/codex-cli-setup.md`, `docs/claude-code-setup.md`, troubleshooting guide.
- README polished for public-repo discoverability.

**Exit criteria.**

- The developer has not manually restarted the gateway for more than two weeks of daily use.
- Logs contain enough structured information that any failure can be diagnosed without rerunning.
- The README answers "what is this," "is this for me," "how do I install it," and "how do I configure it" within five minutes of reading.

---

## Phase 5 and beyond (post-v1)

Listed in `03-architecture.md` Section 10. Highlights:

- Multi-machine v2 with auth.
- IV-strict tool mediation.
- Hot reload of `models.yaml`.
- Token usage estimation.
- Possible Elixir/Phoenix rewrite if maintenance burden of Python parsers grows.

These are not v1 commitments.

---

## Risk Adjustments Based on Phase 0 Findings

Three scenarios that may force scope changes:

### Scenario A: Tool-call output is unparseable

If Phase 0 reveals that the wrapped CLIs do not emit any structured indication of tool calls in non-interactive mode (only final text), Phase 3 becomes either impossible or requires heuristic parsing (e.g., diffing the workspace before and after). Decision point: descope tool interception entirely from v1, or accept heuristic parsing that may miss or misattribute tool events.

### Scenario B: Session continuity does not work non-interactively

If Phase 0 reveals that `claude -p` and `codex exec` are truly one-shot and have no concept of resuming a prior session, the architecture must drive the CLIs in interactive mode via PTY (`pexpect` or similar). This adds at least one weekend of work to Phase 2 and increases parsing fragility for Phase 3 (now you are reading TUI rendering, not non-interactive output).

### Scenario C: Workspace isolation is not honored

If Phase 0 reveals that the wrapped CLIs share state across workspaces in some unexpected way (e.g., a global config that overrides per-workspace context), the session keying may be partially defeated. Mitigation: document the limitation, run the gateway with one workspace at a time, accept the constraint until a vendor fix.

All three scenarios are real possibilities. Phase 0 exists precisely to find out which apply.

---

## Estimated Effort Summary

| Phase | Effort | Outcome |
|---|---|---|
| Phase 0 | 1 evening | Open questions resolved |
| Phase 1 | 1 weekend | Stateless single-turn gateway, all Reverso providers plus direct MiniMax where applicable |
| Phase 2 | 1-2 weekends | Sessions and multi-turn |
| Phase 3 | 1-2 weekends | Tool-use observations |
| Phase 4 | Ongoing | Hardening, docs, polish |
| **Total to v1-full** | **4-6 weekends + ongoing** | |

This matches the BRD's stated estimate. The estimate has substantial uncertainty because Phase 0's findings can shift Phase 2 and Phase 3 effort materially.

---

## Definition of Done for v1

v1 ships when all of the following are true:

- Phases 0 through 3 are complete per their exit criteria.
- Phase 4 has at least: log rotation working, README polished, `gitleaks` enforced on commits.
- The developer has used the gateway in real work for at least two consecutive weeks without rolling back to direct CLI/API use.
- At least one cross-vendor workflow (Codex CLI → Claude, or Claude Code → GPT) is in habitual use.
- The repository is public and the README accurately describes what someone forking it would get.

---

## Responses Providers Milestone (ADR 0002)

This section augments, and does not replace, the phased plan above. It defines a separate
first-milestone increment that moves the Claude and Copilot provider paths to a
Reverso-owned OpenAI Responses gateway. The authoritative decision record is
`docs/architecture/adr/0002-responses-native-provider-gateway.md`; the working plan, PRD, and
test spec are under `.omc/plans/`.

### Milestone goal

Codex targets Claude Code and GitHub Copilot through Reverso using a first-party Responses
API contract, served from one loopback port (`127.0.0.1:64946`) as path-prefixed endpoints
(`/claude/v1`, `/copilot/v1`), with subscription OAuth (Claude) and local logged-in-user
credentials (Copilot) as first-class, tested auth paths. Both providers pass the same
Codex-observed parity suite.

### First deliverable (current increment): docs first

The only in-scope deliverable for the current increment is documentation: ADR 0002 plus
these companion doc sections. No source implementation, adapters, or tests are written until
the docs and ADR are reviewed and define the boundary.

### Milestone boundary (in scope)

- Update canonical docs before code (done by this increment).
- A first-party ASGI app at `src/reverso/protocols/responses_app.py` owning the Claude and
  Copilot `/v1/responses` paths, with a stable provider-adapter boundary.
- LiteLLM quarantined for those paths, with a runtime guard proving it is not the core.
- A Claude adapter with a falsifiable subscription-OAuth gate.
- A Copilot adapter via the ported direct-forward spine (the SDK was evaluated and fails
  Responses parity; see ADR 0002 D4).
- A shared Codex-observed parity fixture suite run against both providers.

### Milestone non-goals

- No Codex CLI provider reimplementation.
- No DeepSeek migration (full LiteLLM retirement is a later milestone, criteria in ADR 0002).
- No launchd productionization or LaunchAgent decommissioning.
- No repository-stored secrets.
- No blind vendoring of the `claude-code-openai-wrapper` or `copilot-openai-api` repos.

### Relationship to Phases 0-4 above

This milestone is orthogonal to the session-daemon and tool-interception phases above. It
does not change the locked Q1-Q18 decisions or the existing chat-completions and
`x_gateway` design; it adds a Responses-native path for two providers under the same loopback
port.

## Auggie and DeepSeek Increment (ADR 0003)

This section augments, and does not replace, the Responses Providers Milestone above. It
defines a follow-on increment that registers two more providers on the same first-party
gateway and resolves how the gateway owns the loopback port. The authoritative decision record
is `docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md`; the working plan,
PRD, and test spec are `.omc/plans/ralplan-auggie-deepseek-responses.md`,
`.omc/plans/prd-auggie-deepseek-responses.md`, and
`.omc/plans/test-spec-auggie-deepseek-responses.md`.

### Increment goal

Codex targets Auggie (via `auggie-sdk`) and DeepSeek through Reverso as same-port,
path-prefixed Responses endpoints (`/auggie/v1`, `/deepseek/v1`) on the one loopback port
`127.0.0.1:64946`, alongside the existing `/claude/v1` and `/copilot/v1`. DeepSeek targets its
full documented Responses modes; Auggie surfaces provider-native behavior with indexing
disabled by default.

### First deliverable (current increment): docs first

The only in-scope deliverable for the current increment is documentation: ADR 0003 plus these
companion doc sections (this section and `docs/03-architecture.md` Section 12). No source
implementation, adapters, or tests are written until the docs and ADR are reviewed.

### Increment boundary (in scope)

- Update canonical docs before code (done by this increment).
- Register `auggie` and `deepseek` by extending `APP_PROVIDER_PREFIXES`
  (`src/reverso/protocols/responses_app.py:42`) and passing their adapters into
  `build_app(adapters)`. No new router type; the merged `ResponsesGatewayApp` owns dispatch.
- Resolve the single-port composition gap with a composition-root front dispatcher booted by
  `src/reverso/proxy/main.py` in place of `reverso.proxy.app:app`, dispatching first-party
  prefixes to the gateway and delegating all other paths to the legacy stack. No new port,
  listener, or process.
- A first-party DeepSeek adapter that calls the DeepSeek API directly (not LiteLLM
  fallthrough) and does not inherit the legacy `drop_params` stripping of `response_format`
  or `reasoning_content`.
- An Auggie adapter (SDK, with a bounded subprocess fallback if the Phase 1 spike requires
  it) with indexing disabled by default and a falsifiable `hard-disable unproven` caveat when
  a hard-disable control cannot be proven.
- The shared Codex-observed parity suite run against both new providers, plus the runtime
  LiteLLM quarantine guard extended to assert the legacy wrapper is bypassed for first-party
  prefixes.

### Increment non-goals

- No new port, listener, process, or provider sidecar.
- No edits to `../oh-my-auggie/`.
- No Claude or Copilot replan beyond topology wording.
- No full LiteLLM retirement (criteria remain in ADR 0002 D2).
- No repository-stored secrets.

### DeepSeek mode promotion gates

DeepSeek JSON output and thinking mode start `unverified` and promote to `pass` only when
their survival tests are green: `response_format` survives end-to-end to the DeepSeek call
(JSON), and a two-turn fixture carries turn-1 `reasoning_content` into the turn-2 request or
rejects clearly (thinking).

## Inbound Anthropic Messages Surface Increment (ADR 0006)

This section augments, and does not replace, the increments above. It defines the Milestone 1
addition of an inbound Anthropic Messages API surface that coexists with the OpenAI Responses
surface on the same loopback port. The authoritative decision record is
`docs/architecture/adr/0006-anthropic-messages-api-surface.md`; the working spec and plan are
`.omc/specs/deep-interview-anthropic-api-surface.md` and
`.omc/plans/ralplan-anthropic-api-surface.md`.

### Increment goal

Claude Code and the Claude Agent SDK target Reverso via `ANTHROPIC_BASE_URL` over the Anthropic
Messages surface (`/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`) on
`127.0.0.1:64946`, with Claude-Code-observed parity. The surface is inbound only (Reverso never
calls `api.anthropic.com`); Messages traffic is model-routed by default to the copilot, deepseek,
and auggie backends through a single first-party authority, with optional per-profile prefixes.
The claude backend was excluded (circular); superseded by ADR 0008, which serves claude
first-party via the local claude CLI with routing/auth env scrubbed from the subprocess.

### First deliverable (current increment): docs first

The only in-scope deliverable for the current increment is documentation: ADR 0006 plus these
companion doc sections (this section, `docs/03-architecture.md` Section 13, the README paragraph,
and the AGENTS.md line). No source implementation, translation layer, registry, or tests are
written until the docs and ADR are reviewed. Implementation then proceeds via per-goal PRs.

### Increment boundary (in scope)

- Update canonical docs before code (done by this increment).
- A pure-ASGI `AnthropicMessagesApp` plus a stateless `anthropic_translate` module mounted in
  `reverso.proxy.compose`, translating Anthropic Messages to and from the FROZEN
  `ProviderAdapter` Responses contract and reusing `protocols/replay.py`. The Protocol is not
  changed.
- A `surface_registry` that is the single first-party model-to-backend authority, reading
  `config/litellm_config.yaml` via `yaml.safe_load` as data and never importing the legacy app,
  with a data-driven `SURFACE_BACKENDS` exposure table (Anthropic surface = copilot, deepseek,
  auggie; claude excluded at Milestone 1, now served per ADR 0008).
- Default auto-routing plus optional per-profile prefixes; unknown model -> 404 (claude was 404
  at Milestone 1, now served per ADR 0008)
  `not_found_error`; missing `anthropic-version` -> default `"2023-06-01"` and echo; Anthropic
  error envelope.
- A per-(feature x backend) capability ceiling enforced as structured errors, with streamed
  thinking deltas and honored `cache_control` as hard `invalid_request_error` cases, and
  count_tokens as a documented word-count approximation.

### Increment non-goals

- No reverso-as-Anthropic-client (no `api.anthropic.com` upstream).
- No Responses-surface regression.
- No Batches or Files API in Milestone 1.
- The claude backend is not exposed on the Anthropic surface.
- codex-cli is Milestone 2 (Anthropic-surface-only, a one-row `SURFACE_BACKENDS` add).

### Relationship to the increments above

This increment is orthogonal to the Responses providers and Auggie/DeepSeek increments. It adds a
second inbound dialect over the same frozen backends and the same loopback port; it does not
change the frozen `ProviderAdapter` Protocol or the existing Responses behavior.
