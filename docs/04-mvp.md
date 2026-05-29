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

**Goal.** A working gateway that handles HTTP-forwarded providers (DeepSeek, MiniMax) and does *stateless* wrapped-CLI provider calls (no sessions, one subprocess per request). End-to-end usable for single-turn requests.

**In scope.**

- Repository skeleton per Architecture Section 7.2.
- `models.yaml` with all eight planned entries.
- `config.yaml` with default values.
- `litellm_config.yaml` that references `models.yaml`.
- LiteLLM custom providers (`anthropic_cli_provider.py`, `openai_cli_provider.py`) that spawn the wrapped CLI per-request, capture output, return final assistant text. No session reuse, no observation parsing.
- The two HTTP-forwarded providers (DeepSeek, MiniMax) configured natively in LiteLLM, no custom code.
- The `x_gateway` envelope on responses (with `observations: []` and `session_id: null` for now).
- launchd LaunchAgents (one for LiteLLM only; daemon comes in Phase 2).
- Keychain integration for secrets.
- Codex CLI profiles configured for all four providers.
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
- Integration tests that exercise both inbound surfaces with all four backends.
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
| Phase 1 | 1 weekend | Stateless single-turn gateway, all four providers |
| Phase 2 | 1–2 weekends | Sessions and multi-turn |
| Phase 3 | 1–2 weekends | Tool-use observations |
| Phase 4 | Ongoing | Hardening, docs, polish |
| **Total to v1-full** | **4–6 weekends + ongoing** | |

This matches the BRD's stated estimate. The estimate has substantial uncertainty because Phase 0's findings can shift Phase 2 and Phase 3 effort materially.

---

## Definition of Done for v1

v1 ships when all of the following are true:

- Phases 0 through 3 are complete per their exit criteria.
- Phase 4 has at least: log rotation working, README polished, `gitleaks` enforced on commits.
- The developer has used the gateway in real work for at least two consecutive weeks without rolling back to direct CLI/API use.
- At least one cross-vendor workflow (Codex CLI → Claude, or Claude Code → GPT) is in habitual use.
- The repository is public and the README accurately describes what someone forking it would get.
