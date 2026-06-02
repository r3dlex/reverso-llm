# Business Requirements Document
## Reverso: Subscription-Backed Local LLM Gateway

**Project codename.** Reverso. Working name pending; reflects the strict 127.0.0.1 binding.

**Document version.** 0.2 (draft incorporating Q1-Q18 decisions)

**Author.** Andre

**Date.** 2026-05-26

**Audience.** Two readers. The first half (Sections 1-3) is written for a non-technical reader who needs to understand why this project exists and what business value it produces. The second half (Sections 4-11) is a personal rationale for future-Andre and for anyone who finds this repo on GitHub and wants to know the reasoning behind the design.

---

## 1. Executive Summary

A developer pays roughly 300 USD per month across two AI subscriptions (Anthropic Claude Max, OpenAI ChatGPT Pro). Both subscriptions ship official command-line tools (Claude Code, Codex CLI) that authenticate against the subscription rather than against a metered API. The flat-rate subscription is the inference budget; using the CLI is free at the margin.

A growing number of useful tools (third-party agents, IDE plugins, custom scripts) expect to call an HTTP API, not invoke a CLI as a subprocess. Today, pointing those tools at the vendor's HTTP API requires opening a metered account on top of the subscription already being paid. The cost compounds: every experiment with a new agent risks adding a metered line item.

Reverso removes that cost. It is a small piece of software that runs on the developer's own workstation, exposes the standard OpenAI and Anthropic API surfaces on the loopback network interface, and serves those API requests by driving the official CLIs as background subprocesses. The subscriptions become the inference budget for any tool that speaks the standard APIs. No new metered accounts. No vendor terms of service complications, because the gateway invokes the legitimately licensed CLIs the developer paid for, on the developer's own machine, for the developer's personal use.

In addition to the subscription-backed providers, the gateway also routes to DeepSeek and MiniMax over their standard HTTP APIs, providing a single unified endpoint for all of the developer's AI tools.

The project is built strictly for personal use on a single trusted machine. It is not designed to be shared, resold, or exposed to the network. Sharing it would void the personal-use assumption that makes the design legally sound. The repository is public so that other developers can adapt it for their own personal use, with their own subscriptions.

## 2. Business Drivers

### 2.1 Cost reduction

The developer's experimental velocity is constrained by the friction of adding metered API accounts. Each new agent or script worth trying nominally requires evaluating "is this worth opening another billing relationship." With Reverso, every such tool runs on subscriptions already paid. The expected savings are not enormous in absolute terms (perhaps 50 to 200 USD per month avoided) but the qualitative effect is large: experimentation becomes free.

### 2.2 Cross-vendor model access

Different LLM vendors have different strengths. The developer routinely wants to ask the same question to Claude and to GPT and compare. Today this requires running both CLIs side by side or maintaining two metered API budgets. Reverso exposes both vendors through a unified API surface, accessible from any tool, with cross-vendor routing controlled by the model identifier in the request. Codex CLI can ask Claude Sonnet a question through Reverso. Claude Code can ask GPT-5-Codex a question through Reverso. The capability arbitrage that was previously expensive becomes nearly free.

### 2.3 Local tool-use mediation

When an agent runs through Reverso, the gateway sees every tool the wrapped CLI invokes: every file edit, every shell command, every search. The gateway records these as structured observations attached to each response. The developer gets a unified, machine-readable log of every action any agent took on the workstation. This is useful for debugging when an agent goes off-script, for auditing when something unexpected changed, and for cross-vendor comparison ("which tools did Claude use to solve this problem vs which tools did GPT use").

### 2.4 Operational simplicity

The gateway is one small process per workstation. It survives reboots via the operating system's native service manager. It stores no secrets in version control. It has no external dependencies beyond the LLM vendors' APIs and the two CLIs the developer already uses. It is a tool, not a platform.

## 3. Stakeholders

- **The developer (Andre).** Owner, primary user, operator.
- **Anthropic and OpenAI.** Upstream subscription vendors. The developer's relationship with them is governed by their consumer terms, which permit personal use of the CLI tools and prohibit account sharing or resale.
- **DeepSeek and MiniMax.** Pay-per-use API vendors. Standard API usage, unchanged from how the developer uses them today.
- **Future readers of the public GitHub repository.** Developers who find the repo, understand the personal-use framing, and adapt it for their own personal use under their own subscriptions.

---

## 4. Personal Rationale (note to future-Andre)

The earlier section is the version someone reads if they wandered in from a search result and wants to understand what this is. This section is the version that exists so I remember why I built this six months from now.

I have been paying for Claude Max and ChatGPT Pro for over a year. Both subscriptions deliver real value: the CLIs are good, the workflows are tight. But I notice myself avoiding new agentic tools because they want an API key, and "another billing relationship" is friction I have learned to resent. Aider wants OpenAI credits. Cline wants Anthropic credits. The custom Elixir/Phoenix experiments I want to run want some key, somewhere. Every one of those keys is metered, every one of them produces a small unpleasant bill at month-end.

Meanwhile, I'm running the CLIs as a human, all day, well within the subscription allowances. The inference capacity I'm paying for is sitting there. The frustration is not the money; it is the structural mismatch between "I pay for unlimited use of these models" and "I cannot programmatically access them without separately paying for metered use of the same models."

This project closes that gap. The CLIs become workers driven by a small local process. Any tool that speaks the standard APIs gets to talk to those workers. My subscriptions become the inference budget for all my agentic experimentation, on top of being the budget for my direct CLI use.

The architecture is more elaborate than I originally hoped because the wrapped CLIs are not designed to be driven this way. They are designed for human interactive use. Getting them to behave as headless workers, maintain session continuity, surface tool-use events in a structured way, and recover from crashes requires real engineering. I have estimated four to six weekends and I expect that to be optimistic.

I am building this in Python because the wrapped CLIs are not in Python, the integration is mostly subprocess management and HTTP, and Python is the path of least resistance. I considered Elixir (my preferred personal stack) but the value of Phoenix/OTP supervision is dwarfed by the value of LiteLLM's existing OpenAI ↔ Anthropic translation, which is in Python. Using LiteLLM as the inbound HTTP layer and writing custom providers plus a small session daemon to handle the wrapped CLIs is the right scope tradeoff.

If this project is still running in 2027, the design assumptions that mattered most were:

- Strict loopback binding from day one. No "we'll add auth later." Auth is a different project.
- The 30-minute idle window is real-user-defined, not strictly time-defined. Sessions stay warm while child processes are alive. Long-running daemons started by tool calls keep their parent session pinned indefinitely. This matches how I actually work and how I will continue to work.
- Tool-use interception in v1, not deferred. The reason is that if I defer it, I will use the gateway as a thin pipe for months, will not have observation data to learn from, and will not be in a good position to design v2 well. Building it now, even at the cost of fragile output parsing, is the only way to find out what works.
- The session key is (machine, workspace, provider). Machine is constant in v1 but reserved for v2 multi-machine deployments. Workspace defaults to the gateway's working directory if the inbound request does not supply `x_gateway.workspace`.
- Per-machine instances. The Mac mini and the laptop each run their own copies. They do not share state. If I want to talk to the Mac mini's gateway from the laptop, that is a v2 problem requiring auth, mutual TLS, and possibly Tailscale. v1 does not anticipate it.

## 5. Scope (Goals)

### 5.1 Primary

- Expose OpenAI Chat Completions (`POST /v1/chat/completions`) and Anthropic Messages (`POST /v1/messages`) APIs on the loopback interface, port 64946.
- Route requests to one of four providers based on the `model` field:
  - **Anthropic** via wrapped Claude Code subprocess.
  - **OpenAI** via wrapped Codex CLI subprocess.
  - **DeepSeek** via direct HTTP forward.
  - **MiniMax** via direct HTTP forward.
- Maintain long-lived subprocess sessions for the two wrapped-CLI providers, keyed by (machine, workspace, provider).
- Intercept tool-use events from the wrapped CLIs and surface them in an `x_gateway.observations` extension field on every response.
- Bind to 127.0.0.1 only. Reject any attempt to bind to a non-loopback interface.

### 5.2 Secondary

- Survive workstation reboot via launchd.
- Provide a machine-readable model registry (`models.yaml`) committed to the repository.
- Provide structured logs of every conversation turn.
- Reload model registry on service restart (no hot reload in v1).

### 5.3 Non-goals (explicit)

- Not a multi-user system. Single user, single machine.
- Not exposed to any network beyond loopback.
- Not a replacement for using the CLIs directly. The CLIs remain the primary interface for direct work; Reverso exists for cases where another tool wants HTTP access.
- Not metered or quota-aware. The gateway does not track subscription allowances. Upstream rate limits and errors surface to inbound clients unmodified.
- Not a shared service. If a colleague wants the same setup, they fork the repo and run their own instance under their own subscriptions.

## 6. Decisions Made (from Q1-Q18)

The following decisions are locked. Detailed justifications appear in the PRD; the BRD records them so the reasoning is preserved.

| # | Decision | Rationale (summary) |
|---|---|---|
| 1 | Session key is (machine, workspace, provider) | Machine reserved for v2 multi-machine. Workspace and provider drive routing today. |
| 2 | Loopback bind strict, machine dimension preserved anyway | Single source of truth for keying, regardless of binding restriction. |
| 3 | Idle definition: no API in flight AND no live child processes | Long-running tool calls (servers, watchers) keep their session pinned. |
| 4 | No max age, no max turn count | Accept infinite session lifetime as a feature. |
| 5 | Gateway restart is full reset, documented as expected | No persistence layer. Simplest honest answer. |
| 6 | Tool-use interception: IV-pragmatic | CLIs execute their own tools; gateway reports observations after the fact. |
| 7 | DeepSeek and MiniMax are HTTP backends only | No subprocess management for these. |
| 8 | `x_gateway` extension envelope always present | Consistent across all providers, including HTTP-forwarded. |
| 9 | Bind to 127.0.0.1 is the only security boundary | Loopback is trusted. Personal Mac assumption. |
| 10 | Detect conflicting CLI processes and warn | No enforcement, just visibility. |
| 11 | HTTP 5xx with populated observations on crash | Honest about failure, preserves side-effect visibility. |
| 12 | Model registry separate from runtime config | Different update cadences. Registry is data. |
| 13 | Registry is `models.yaml` in the repo | Committed, PR-able, reload on restart. |
| 14 | Registry entries include capability flags | Inbound clients can introspect via `GET /v1/models`. |
| 15 | Any model callable from any inbound surface | DeepSeek via Anthropic surface is allowed but not encouraged. |
| 16 | Workspace from `x_gateway.workspace` extension, default fallback | Symmetric with response envelope. |
| 17 | Wrapped CLI's session is canonical conversation state | Inbound `messages` array's prior turns ignored. |
| 18 | v1 scope: sessions + tool interception (v1-full) | Four to six weekends. Building observability now informs v2. |

## 7. Success Criteria

Twelve weeks after first install:

- The developer is no longer running metered Anthropic or OpenAI API accounts for personal tooling.
- At least one cross-vendor workflow is in regular use (Codex CLI to Claude, or Claude Code to GPT).
- Session continuity through the gateway is indistinguishable in quality from direct CLI use for the workflows the developer actually runs.
- `x_gateway.observations` data has been used at least once for debugging or auditing.
- The developer has not had to restart the gateway manually more than twice per month.

## 8. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CLI output format changes break tool-use parsing | High | High | Version-pin the wrapped CLIs; isolate parsing in one module; integration tests run on CLI upgrade |
| Vendor terms-of-service interpretation changes | Low | High | Personal-use framing is well within current terms; no resale; no account sharing |
| Subscription rate-limit exhaustion mid-session | Medium | Medium | Surface upstream errors transparently; do not silently retry |
| Workspace conflict between gateway and direct CLI use | Medium | Medium | Detect and warn at session spawn (Q10) |
| Performance regression from subprocess management | Medium | Low | Warm sessions amortize spawn cost; the Mac mini has substantial headroom |
| Repo accidentally leaks secrets | Low | High | Public repo since day one; `gitleaks` pre-commit hook; secrets in macOS Keychain |
| Tool-use translation fidelity loss | High | Medium | Document which inbound tools map to which CLI tools; explicit error on un-mappable combinations |
| Loopback bypass via SSH tunnel exposes gateway | Low | Medium | Tunnel access is an explicit v2 design problem with its own auth |

## 9. Constraints

- **Single host.** Mac mini (Apple Silicon, macOS) is the primary deployment. Developer laptop is a secondary deployment with its own independent instance.
- **Python toolchain.** Implementation language is Python 3.12+, managed via `uv`. This reflects both the LiteLLM dependency and the wrapped-CLI ecosystem.
- **No commercial use.** Personal projects only.
- **Public repository.** Code, configs, and docs are public on GitHub. Secrets are not. Operational details (machine names, file paths) are not.
- **Vendor CLI as black box.** The wrapped CLIs are upstream-controlled binaries. Their internal behavior may change between releases. Reverso adapts; it does not depend on internal CLI mechanics.

## 10. Assumptions

- Claude Code and Codex CLI remain distributed as subscription-authenticated CLIs with some form of non-interactive invocation mode (`-p`, `exec`, or equivalent).
- Both CLIs continue to be runnable on Apple Silicon.
- Both CLIs continue to produce output that is parseable enough to extract assistant text and tool-use events. The exact format is a known liability; see Risks.
- DeepSeek and MiniMax continue to offer OpenAI-compatible chat endpoints.
- The developer's two subscriptions remain in good standing.

## 11. Open Questions

These remain unresolved and drive Phase 0 spike work in the MVP plan.

- **Q-Spike-1.** Does `claude -p` support multi-turn session resume via flag or environment? Or must session continuity be maintained by replaying the conversation each turn?
- **Q-Spike-2.** Same question for `codex exec`.
- **Q-Spike-3.** What format does Claude Code use for tool-call output in non-interactive mode? Is it stable enough to parse?
- **Q-Spike-4.** Same question for Codex CLI.
- **Q-Spike-5.** Can Claude Code be invoked with a working directory that is not the developer's actual cwd? (Affects per-workspace session keying.)
- **Q-Spike-6.** Same question for Codex CLI.

Phase 0 of the MVP plan resolves these. If any answer turns out to be "no, this is not possible," scope adjustments to v1 are required.

## 12. Glossary

- **Reverso.** The project.
- **Loopback interface.** 127.0.0.1; the only interface Reverso binds to.
- **Wrapped CLI.** Claude Code or Codex CLI, run as a subprocess under the gateway.
- **Inbound surface.** One of the two HTTP endpoints the gateway exposes: OpenAI Chat Completions (`/v1/chat/completions`) or Anthropic Messages (`/v1/messages`).
- **Provider.** A logical inference source. Four: Anthropic, OpenAI, DeepSeek, MiniMax.
- **Session.** A long-lived conversation state, materialized as a wrapped-CLI subprocess, keyed by (machine, workspace, provider).
- **Workspace.** A directory path treated as the project root.
- **Turn.** One request-response cycle within a session.
- **Observation.** A record of a tool the wrapped CLI invoked during a turn, surfaced in the response's `x_gateway.observations` array.
- **Model registry.** A YAML file in the repository describing all model identifiers the gateway accepts and their backend mapping.
