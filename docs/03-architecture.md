---
type: architecture
project: reverso
---

# Architecture Sketch
## Reverso Gateway

**Document version.** 0.2 (draft)

**Companion documents.** `01-brd.md`, `02-prd.md`, `04-mvp.md`.

**Scope.** Component-level architecture. Not code. Identifies the major modules, their responsibilities, their interfaces, and the runtime topology. Stack-specific (Python + LiteLLM + asyncio) but at the level of "what subprocesses run where," not "what classes have what methods."

---

## 1. Runtime Topology

Two long-lived processes per Reverso installation:

```
+----------------------------------+
|  launchd  (macOS service mgr)    |
+----------------------------------+
        |                |
        v                v
+--------------+   +-----------------+
| LiteLLM      |   | Session Daemon  |
| Python proc  |   | Python proc     |
| :64946        |   | UDS socket      |
+------+-------+   +--------+--------+
       |                    |
       | UDS http calls     |
       +-----+--------------+
             v
       +-------------+
       | Wrapped     |
       | CLIs        |
       | (claude,    |
       | codex)      |
       +-------------+
```

**LiteLLM process.** Hosts the inbound HTTP API on `127.0.0.1:64946`. Handles request parsing, body translation between OpenAI and Anthropic shapes, response streaming. Loads `models.yaml` and uses it as its `model_list`. Custom providers route subprocess-backed models to the session daemon.

**Session daemon process.** Owns all wrapped CLI subprocesses. Maintains session table. Implements session lifecycle (spawn, turn, idle detection, recycle). Exposes a small internal HTTP API over a Unix-domain socket. Not exposed on TCP at all.

The two processes communicate over a Unix-domain socket at `~/Library/application support/reverso/daemon.sock`. The session daemon has no TCP listener. This means:

- Only processes running as the same user as the daemon can talk to it.
- It is trivially impossible to expose the daemon to the network even by misconfiguration.
- The LiteLLM custom provider uses `httpx` with a `transport=httpx.HTTPTransport(uds=...)` to call the daemon.

## 2. Component Inventory

### 2.1 LiteLLM proxy (Process 1)

| Component | Responsibility |
|---|---|
| LiteLLM core | Inbound HTTP server, model_list routing, body translation, streaming bridge |
| `models.yaml` loader | Parsed once at startup, expanded into LiteLLM's `model_list` |
| `anthropic_cli_provider.py` | LiteLLM custom provider; forwards Anthropic-backend requests to the session daemon |
| HTTP-forwarded provider | Standard LiteLLM behavior for DeepSeek (no custom code) |
| `x_gateway` injector | Middleware that wraps responses to add the `x_gateway` envelope |

### 2.2 Session daemon (Process 2)

| Component | Responsibility |
|---|---|
| Internal HTTP server | Accepts `POST /session/turn` from LiteLLM custom providers over UDS |
| Session table | In-memory dict mapping (machine, workspace, provider) to Session object |
| Session lifecycle | Spawn, turn execution, idle detection, recycle |
| Subprocess manager | Owns wrapped CLI processes via `asyncio.subprocess` |
| Output parser | Per-CLI module that extracts assistant text and tool-call events from CLI output |
| Recycle sweeper | `asyncio` task running every 60 minutes; checks idle conditions, terminates stale sessions |
| Workspace conflict detector | Scans for other `claude`/`codex` processes at session spawn; emits warnings |
| `x_gateway` data producer | Constructs observation objects from parsed tool-call events |

## 3. Request Flow

### 3.1 Wrapped-CLI request (subscription-backed)

1. Client sends `POST /v1/chat/completions` to `127.0.0.1:64946` with body `{model: "claude-sonnet-4-5", messages: [...], x_gateway: {workspace: "/Users/andre/Ws/foo"}}`.
2. LiteLLM parses the request, looks up `claude-sonnet-4-5` in `model_list`, sees it routes to the custom provider `anthropic_cli_provider`.
3. The custom provider extracts the latest user message and workspace, calls the session daemon over UDS: `POST /session/turn {workspace: "/Users/andre/Ws/foo", provider: "anthropic", user_message: "..."}`.
4. The session daemon computes the session key `(machine, "/Users/andre/Ws/foo", "anthropic")`, looks it up in its session table.
5. If no session exists: spawn a new wrapped Claude Code subprocess in the workspace directory, store it in the session table.
6. If session exists: use the existing subprocess.
7. The daemon writes the user message to the subprocess (via stdin or the CLI's prompt mechanism, whichever Phase 0 determines is viable).
8. The daemon's output parser reads the subprocess's stdout incrementally, identifying:
   - Tool-call events (recorded as observations).
   - The final assistant text.
9. When the turn completes, the daemon returns `{assistant_text: "...", observations: [...]}` to the custom provider.
10. The custom provider constructs an OpenAI Chat Completions response shape with the assistant text and adds the `x_gateway` envelope (session_id, observations, etc.).
11. LiteLLM returns the response to the client.

### 3.2 HTTP-forwarded request (DeepSeek)

1. Client sends `POST /v1/chat/completions` with `model: "deepseek-reasoner"`.
2. LiteLLM looks up the model, sees it routes to the native LiteLLM DeepSeek backend.
3. LiteLLM forwards the request to `api.deepseek.com`, with the API key injected from env (which the gateway populated at startup from Keychain).
4. LiteLLM receives the response, applies any necessary body translation if the inbound surface was Anthropic-shape.
5. The `x_gateway` injector adds the envelope (session_id null, observations empty, provider set to deepseek).
6. LiteLLM returns the response.

### 3.3 Streaming

Both flows above support streaming. The wrapped-CLI flow requires the daemon to emit chunks as the wrapped CLI produces output, the custom provider to relay them as a generator into LiteLLM's streaming pipeline, and LiteLLM to forward them as SSE to the client. The HTTP-forwarded flow uses LiteLLM's existing streaming relay unchanged.

## 4. Session State Model

### 4.1 In-memory session table

```python
# Conceptual shape, not literal code:
{
    ("mac-mini-andre", "/Users/andre/Ws/foo", "anthropic"): Session(
        process=<subprocess.Process>,
        spawned_at=<datetime>,
        last_request_at=<datetime>,
        turn_count=42,
        ...
    ),
    ("mac-mini-andre", "/Users/andre/Ws/bar", "openai"): Session(...),
}
```

### 4.2 Idle detection

The recycle sweeper, on each tick, for each session in the table:

1. Computes minutes since `last_request_at`. If less than 30, skip (not idle).
2. Walks the process tree of the wrapped CLI subprocess (via `psutil`). If any descendant is alive, skip (per Q3-(ii): live children keep the session warm).
3. Otherwise, mark for recycle. Send SIGTERM to the wrapped CLI. Wait 5 seconds. SIGKILL if still alive. Remove from session table.

### 4.3 No persistence

The session table is in-memory only. Gateway restart loses everything. This is the locked-in behavior from Q5.

## 5. Tool-Use Interception Detail

### 5.1 What the daemon parses

When the wrapped CLI runs a tool (Read, Edit, Bash, etc.), it emits structured output that the daemon's per-CLI parser must recognize. Examples of what to look for (subject to Phase 0 validation):

- Claude Code in non-interactive mode emits tool-call JSON blocks delimited by recognizable markers, or emits a structured output format when invoked with appropriate flags.
- Codex CLI may emit similar structured output or may require parsing of its TUI rendering.

The parser's job is to identify, for each tool the CLI ran:
- The tool name.
- The arguments (as a JSON object).
- A short result indicator (success/failure + maybe a length or error string).
- The elapsed time.

This is the most fragile component in the system. It depends on the CLIs' output format, which is upstream-controlled and not contractually stable.

### 5.2 Inbound tool declaration translation

When the inbound request includes a `tools` array (declaring tools the model could call):

1. The daemon attempts to map each inbound tool to a wrapped-CLI native tool by name + schema similarity.
2. Mappings that succeed are noted; the wrapped CLI will use its native equivalent when the model asks for a mapped tool.
3. Mappings that fail are dropped. The dropped names are added to `x_gateway.warnings` in the response.

This mapping is data-driven, defined per-provider in a `tool_mappings.yaml` file. v1 ships with a small initial set covering the most common tools (file read, file edit, shell).

## 6. Configuration and Secrets

### 6.1 Configuration files (committed to git)

- `models.yaml` - model registry (per Q13).
- `config.yaml` - runtime config (paths, timeouts, ports).
- `tool_mappings.yaml` - inbound-tool to CLI-tool mapping.

### 6.2 Secrets (not in git)

Stored in macOS Keychain under the namespace `reverso/<KEY_NAME>`:

- `DEEPSEEK_API_KEY`
- No Anthropic or OpenAI keys are needed; the wrapped CLIs authenticate via their own ChatGPT/Claude subscription tokens.

Both processes (LiteLLM and the session daemon) read Keychain on startup via the `security` CLI, populate env vars, and pass them to upstream HTTP calls.

## 7. Deployment Topology

### 7.1 Two launchd LaunchAgents

`com.user.reverso-daemon.plist` - Session daemon. Starts first, listens on the UDS.

`com.user.reverso-proxy.plist` - LiteLLM proxy. Starts independently on loopback. When the daemon transport is unavailable, times out, or closes unexpectedly, wrapped-CLI providers record a warning and degrade to a stateless one-turn CLI subprocess so local Codex profiles keep working; session continuity is cold for that turn. Daemon HTTP status errors are surfaced and are not masked by stateless fallback.

Both run under the developer's user account, both restart on crash via launchd's `KeepAlive`, both log to `~/Library/Logs/reverso/`.

### 7.2 Repository structure

```
loopback/
├── README.md
├── LICENSE
├── .gitignore
├── .python-version
├── pyproject.toml
├── uv.lock
├── config.yaml
├── models.yaml
├── tool_mappings.yaml
├── litellm_config.yaml         # LiteLLM-specific config referencing models.yaml
├── Makefile
├── src/
│   ├── loopback/
│   │   ├── __init__.py
│   │   ├── daemon/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── session.py
│   │   │   ├── recycler.py
│   │   │   ├── parsers/
│   │   │   │   ├── claude_code.py
│   │   │   │   └── codex_cli.py
│   │   │   └── api.py
│   │   └── proxy/
│   │       ├── __init__.py
│   │       └── anthropic_cli_provider.py
├── scripts/
│   ├── install-launchagents.sh
│   ├── uninstall-launchagents.sh
│   ├── keychain-set.sh
│   ├── start-fg-daemon.sh
│   ├── start-fg-proxy.sh
│   ├── smoke.sh
│   └── test-anthropic.sh
├── launchd/
│   ├── com.user.reverso-daemon.plist.tmpl
│   └── com.user.reverso-proxy.plist.tmpl
├── docs/
│   ├── 01-brd.md
│   ├── 02-prd.md
│   ├── 03-architecture.md
│   ├── 04-mvp.md
│   ├── codex-cli-setup.md
│   └── claude-code-setup.md
└── tests/
    ├── integration/
    └── unit/
```

## 8. Failure Modes and Recovery

| Failure | Detection | Recovery |
|---|---|---|
| Wrapped CLI crashes mid-turn | Subprocess exits before assistant text complete | Return HTTP 500 with populated observations; remove session from table |
| Session daemon transport unavailable | LiteLLM custom provider gets UDS connection, timeout, or remote protocol error | Provider records a warning, runs one stateless CLI turn when the CLI binary is available, and launchd restarts the daemon; session continuity is cold for that degraded turn. Daemon HTTP status errors are surfaced. |
| LiteLLM crashes | launchd notices process exit | launchd restarts; daemon is unaffected; existing sessions remain alive |
| Workspace path invalid | Validated at request time | HTTP 400 returned to client; no session created |
| Two requests for same session arrive concurrently | Session daemon detects via per-session asyncio lock | Second request blocks until first completes; daemon processes serially per session |
| Models.yaml malformed | Validated at startup | LiteLLM fails to start; launchd retries with backoff; logs explain the parse error |
| Keychain missing required secret | Validated at startup | Daemon emits warning, marks the affected provider as unavailable; requests for that provider return HTTP 503 |
| Wrapped CLI hangs (no output for N minutes) | Per-turn timeout (default 5 minutes) | Daemon sends SIGTERM to subprocess; returns HTTP 504; session removed from table |

## 9. Performance Characteristics

| Metric | Target | Reasoning |
|---|---|---|
| Cold-start session latency overhead | < 10 seconds | Wrapped CLI spawn + first-token time |
| Warm-session per-turn latency overhead | < 200 ms | Daemon dispatch + parser + envelope construction |
| Throughput | 4 concurrent sessions on Mac mini | One core per active wrapped CLI is generous |
| Memory at rest | < 200 MB | LiteLLM ~100 MB, daemon ~50 MB |
| Memory per active session | wrapped CLI footprint + 50 MB | Daemon-side overhead is modest |
| Restart time | < 10 seconds | Both processes warm-start quickly |

## 10. v2 Considerations (deliberately out of scope for v1)

These are explicitly deferred but worth noting so v1 design choices accommodate them where free to do so:

- **Multi-machine deployment.** The machine dimension of the session key is already reserved.
- **Session forking.** The `x_gateway.session_tag` extension field is reserved in the response envelope; v1 always sets it to null.
- **Hot-reload of models.yaml.** Possible via SIGHUP; v1 requires restart.
- **Tool execution mediation (IV-strict).** v1 is IV-pragmatic; v2 could intercept and require client approval before tool execution if the wrapped CLIs gain support for this mode.
- **Token usage accounting.** Either via wrapped CLI cooperation or via tiktoken-based estimation.
- **Authentication for tunnel access.** A v2 concern paired with multi-machine; not relevant for v1.
- **An Elixir/Phoenix rewrite if maintenance burden grows.** The architecture maps cleanly. Daemon to GenServer-per-session under Supervisor. UDS to Phoenix endpoint on a different port. The model registry, configuration, operational scripts, and overall design transfer intact.

## 11. Responses-Native Provider Gateway (ADR 0002)

This section augments, and does not replace, Sections 1-10. It records the first-milestone
shift toward a Reverso-owned OpenAI Responses gateway for the Claude and Copilot provider
paths. The authoritative decision record is `docs/architecture/adr/0002-responses-native-provider-gateway.md`.
A follow-on increment registers Auggie and DeepSeek on the same gateway and resolves how the
gateway owns the loopback port; its authoritative record is
`docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md`. Subsections 11.6 and
11.7 below summarize that follow-on; ADR 0003 governs.

### 11.1 What changes

Today `src/reverso/proxy/app.py` wraps `litellm.proxy.proxy_server.app`. The new milestone
introduces a first-party ASGI app at `src/reverso/protocols/responses_app.py` that owns the
Claude and Copilot `/v1/responses` paths. LiteLLM is quarantined (not the core router) for
those paths and is retired fully in a later milestone (criteria in ADR 0002 D2). DeepSeek and
other non-goal providers are untouched in this milestone.

### 11.2 Single-port, path-prefixed endpoint model

All providers are served from the one loopback port `127.0.0.1:64946`. Each provider is a
path-prefixed Responses endpoint under that single port, so one Codex profile maps to one
provider:

- Claude: `http://127.0.0.1:64946/claude/v1`
- Copilot: `http://127.0.0.1:64946/copilot/v1`

A Codex profile sets `base_url` to a provider prefix and reaches `/v1/responses`,
`/v1/models`, and related routes under that prefix. There is no per-provider port. This
reuses the existing `/<profile>/v1/...` to `/v1/...` rewrite in
`src/reverso/proxy/profile_routing.py`. The `copilot` prefix is net-new and is added in the
first-party app, not by mutating the legacy `PROVIDER_PREFIXES`.

### 11.3 Provider boundary

The app separates protocol normalization, provider routing, provider adapters, a response
or session store (in-memory for the first milestone), and compatibility middleware. The
provider adapter interface is stable so the Claude and Copilot internals can evolve
independently:

- `create_response(request) -> ResponseEnvelope`
- `stream_response(request) -> AsyncIterator[SSEEvent]`
- `list_models() -> ModelList`
- `get_response(response_id)` and `list_input_items(response_id)` where Codex-observed
  fixtures require them.

### 11.3.1 Headroom compression seam

Headroom is installed as a base runtime dependency so a normal Reverso install can use
compression without a second setup step. The first increment is a provider-neutral seam in
`src/reverso/protocols/headroom_compression.py`; gateway dispatch does not call it yet.
The seam projects text-bearing Responses fields to Headroom messages, runs Headroom off
the event loop with a short timeout, then reconstructs the original Responses shape. It
preserves non-text content, tool metadata, response ids, and adapter boundaries. Unsafe
output, timeout, exceptions, or token inflation fail open to the original request.

Runtime controls:

- Enabled by default. Set `REVERSO_HEADROOM_ENABLED=0` to disable.
- Compression profile defaults to `agent-90` and is configurable with
  `REVERSO_HEADROOM_PROFILE`.
- Headroom process settings are enforced as stateless: `HEADROOM_STATELESS=true`,
  telemetry off, update checks off, no periodic token stats, and
  `HEADROOM_CCR_BACKEND=memory` so CCR retrieval state is process-local only.
- Metrics are process-local aggregates only and never store prompt text.
- Optional Headroom extras such as `headroom-ai[ml]` or `headroom-ai[all]` are not
  installed by default; operators can add them explicitly when local model support is
  needed.

### 11.4 Authentication

- Claude: subscription OAuth via the `claudeAiOauth` artifact (macOS Keychain service
  `Claude Code-credentials`, or `~/.claude/.credentials.json` headless), asserted explicitly
  by the adapter, not inferred by absence of an API key. See ADR 0002 D3.
- Copilot: local logged-in-user credentials via the ported direct-forward adapter, no
  repository secret. See ADR 0002 D4.

### 11.5 LiteLLM quarantine guard

A runtime guard test traces `litellm.proxy.proxy_server.app` for zero invocations during
Claude and Copilot handling, and asserts the new app's import graph excludes
`reverso.proxy.app`. This proves LiteLLM is not the hidden core for the new paths.

## 12. Auggie and DeepSeek on the same gateway (ADR 0003)

This section augments Section 11 and records the follow-on increment that registers two more
providers and resolves the single-port composition gap. The authoritative decision record is
`docs/architecture/adr/0003-single-port-composition-auggie-deepseek.md`.

### 12.1 Four providers, one port, path-prefixed

All providers are served from the one loopback port `127.0.0.1:64946` as path-prefixed
Responses endpoints. The follow-on increment adds the Auggie and DeepSeek prefixes:

- Claude: `http://127.0.0.1:64946/claude/v1`
- Copilot: `http://127.0.0.1:64946/copilot/v1`
- Auggie: `http://127.0.0.1:64946/auggie/v1`
- DeepSeek: `http://127.0.0.1:64946/deepseek/v1`

Registration extends the gateway allow-list
`APP_PROVIDER_PREFIXES` (`src/reverso/protocols/responses_app.py:42`) to include `auggie` and
`deepseek`, and passes their adapters into `build_app(adapters)`
(`src/reverso/protocols/responses_app.py:352`). No new router type is created; the merged
`ResponsesGatewayApp` owns dispatch, and the constructor allow-list guard
(`responses_app.py:277-283`) still rejects any unknown prefix.

### 12.2 Composition root resolves how the gateway owns the port

Today `src/reverso/proxy/main.py:99` boots `reverso.proxy.app:app` (the legacy LiteLLM
middleware stack), so `ResponsesGatewayApp` is never reached at runtime. The increment
introduces a thin composition-root ASGI module (for example `src/reverso/proxy/compose.py`)
that owns the port and dispatches by leading path segment: first-party provider prefixes go
to `ResponsesGatewayApp`, and every other path is delegated to the legacy
`reverso.proxy.app:app` as the named legacy-fallthrough surface. `main.py:99` changes its
uvicorn target to the composition root; host and port handling (`main.py:92-93`) are
unchanged, so there is no new port, listener, or process. Rollback is a one-line revert of the
boot target.

The composition root is the only module that imports both the gateway and the legacy app, so
`responses_app.py` keeps its no-import-of-`reverso.proxy.app` property. The LiteLLM quarantine
guard gains a second assertion: the `reverso.proxy.app` wrapper is bypassed for first-party
prefixes, not only that the inner `litellm.proxy.proxy_server.app` symbol is uninvoked.

### 12.3 DeepSeek is first-party, not LiteLLM fallthrough

`/deepseek/v1/responses` is served by a first-party DeepSeek adapter that calls the DeepSeek
API directly (mirroring the subprocess precedent at
`src/reverso/protocols/adapters/claude.py:26`), not by LiteLLM. The legacy DeepSeek config is
deprecated in place, not deleted. The first-party adapter must not inherit the legacy
`drop_params` behavior at `config/litellm_config.yaml:23,90-116`: it must not strip
`response_format` (gates JSON mode) or `reasoning_content` (gates thinking mode). Those two
modes stay `unverified` until their survival tests pass.

## 13. Inbound Anthropic Messages surface (ADR 0006)

This section augments Sections 11 and 12 and records the Milestone 1 addition of a second
inbound dialect: the Anthropic Messages API. The authoritative decision record is
`docs/architecture/adr/0006-anthropic-messages-api-surface.md`.

### 13.1 A second inbound surface on the same port

Reverso adds the Anthropic Messages surface (`POST /v1/messages`, `POST /v1/messages/count_tokens`,
`GET /v1/models`) alongside the existing OpenAI Responses surface, on the one loopback port
`127.0.0.1:64946`. The primary client is Claude Code and the Claude Agent SDK pointed at Reverso
via `ANTHROPIC_BASE_URL`. The fidelity target is Claude-Code-observed parity. The surface is
inbound only: Reverso does NOT call `api.anthropic.com`; it translates Anthropic Messages traffic
onto the existing Responses backends.

### 13.2 Translation app over the frozen Responses contract

A new pure-ASGI `AnthropicMessagesApp` plus a stateless `anthropic_translate` module are mounted
in the composition root (`reverso.proxy.compose`). They translate Anthropic Messages requests and
responses to and from the FROZEN `ProviderAdapter` Responses contract (`ResponsesRequest`,
`ResponseEnvelope`, `SSEEvent`) and reuse `protocols/replay.py` for streaming. The
`ProviderAdapter` Protocol is NOT changed; the Anthropic surface is a front-of-gateway translation
seam, not a sixth adapter method. All conversation state continues to ride the in-memory
`ResponseStore` through the Responses contract.

### 13.3 surface_registry: the single model-to-backend authority

A new `surface_registry` is the single first-party model-to-backend authority. The first-party
stack routes by path prefix only and has no model map of its own; the only model map,
`config/litellm_config.yaml`, belongs to the quarantined legacy LiteLLM app. `surface_registry`
reads that file via `yaml.safe_load` as DATA only and never imports the legacy app, so the ADR
0002 D2 quarantine is preserved. Surface-scoped exposure is data, held in a `SURFACE_BACKENDS`
table. For Milestone 1 the Anthropic-surface backends are `copilot`, `deepseek`, and `auggie`;
`claude` was excluded because Claude Code talking to a claude backend is circular (superseded by
ADR 0009: claude is now served first-party via the local claude CLI, with `ANTHROPIC_BASE_URL`/
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` scrubbed from the subprocess env to prevent the loop).
Milestone 2 adds `codex-cli` as a single one-row addition.

### 13.4 Routing, version, and error behavior

Default routing is automatic model-to-backend resolution through `surface_registry`, with optional
per-profile path prefixes (`/deepseek/v1/messages`, `/copilot/v1/messages`, `/auggie/v1/messages`)
for explicit pinning. An unknown model OR any `claude` model returns HTTP 404 `not_found_error`. A
missing `anthropic-version` header defaults to `"2023-06-01"` and is echoed back; a missing version
header is never a 400. The error envelope is the Anthropic shape
`{"type": "error", "error": {"type": ..., "message": ...}}`.

### 13.5 Capability ceiling

The surface does not promise full Messages fidelity on every backend. Per the ADR 0006 matrix,
image input is native on copilot and gated on deepseek and auggie; tool_use output is
native/translated on copilot and deepseek and a text-only gated error on auggie. Streamed thinking
deltas and honored `cache_control` are structurally impossible in Milestone 1 (the Responses
replay discards reasoning deltas and nothing honors prompt caching) and both surface to the client
as a hard `invalid_request_error`. `count_tokens` is a documented word-count approximation, not a
real tokenizer. The `anthropic` SDK is a docs and contract reference only, not a runtime
dependency.

## 14. Codex GPT backend on the Anthropic surface (ADR 0007)

This section augments Section 13 and records the Milestone 2 addition of a first-party Codex
backend that exposes gpt-* models on the Anthropic surface. The authoritative decision record is
`docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`.

### 14.1 A fifth adapter, Anthropic-surface-only

Milestone 2 adds a first-party `CodexAdapter` that serves the five gpt models (gpt-5.5, gpt-5.4,
gpt-5.4-mini, gpt-5.3-codex-spark, gpt-4.1) on the Anthropic Messages surface ONLY. This is the
symmetric mirror of Milestone 1: the claude backend is Responses-surface-only because Claude Code
talking to a claude backend is circular, so the codex backend is Anthropic-surface-only because
Codex talking to a codex backend is circular. gpt-on-the-Responses-surface is removed by this
milestone, not relocated. `codex` is added to `SURFACE_BACKENDS` as a single data row and
registered in `build_adapters`; a negative test asserts the codex backend is not reachable on the
Responses surface.

### 14.2 Codex CLI under ChatGPT OAuth via the bounded spine

`CodexAdapter` implements the frozen `ProviderAdapter` Protocol and invokes the Codex CLI
(`codex exec`) through the bounded `cli_spine` (ADR 0005) for both non-streaming and streaming,
inheriting its wall-clock bound, redaction, cause suppression, and kill-on-abandon contract. It
parses Codex Responses-style events into the internal Responses contract (`ResponsesRequest`,
`ResponseEnvelope`, `SSEEvent`); the Milestone 1 Anthropic translation, streaming, and gating
layers then convert that contract to and from Anthropic Messages. Reverso does NOT call the OpenAI
Platform Responses API and does NOT add `openai-python` as a runtime dependency.

Authentication is a new `CodexOAuthAuth` resolver mirroring `ClaudeOAuthAuth`: it reads and
validates the ChatGPT/Codex OAuth subscription artifact (written by `codex login`, NOT an OpenAI
API key), fails closed with a structured Anthropic error when the session is missing or expired,
and injects the OAuth token into the Codex CLI child environment without ever logging it. The exact
artifact location and format are an explicit discovery spike before the resolver is implemented.

### 14.3 Clean-cut removal of the legacy openai_cli path

The legacy LiteLLM gpt path (`openai_cli_provider.py` and the `openai_cli` gpt rows in
`config/litellm_config.yaml`) is removed in this milestone, not kept as a coexisting fallback;
`codex_sync.py` is reconciled with the removal. After the clean cut the first-party `CodexAdapter`
is the sole gpt path, backstopped by the Anthropic parity suite, the loopback smoke test, and a
fast `git` revert.
