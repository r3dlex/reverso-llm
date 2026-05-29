# Product Requirements Document
## Reverso Gateway

**Document version.** 0.2 (draft)

**Author.** Andre

**Date.** 2026-05-26

**Companion documents.** `01-brd.md` (business framing), `03-architecture.md` (component design).

---

## 1. Product Summary

Reverso is a single-process gateway that runs on a developer workstation, exposes the OpenAI and Anthropic standard HTTP APIs on the loopback interface, and serves those requests by either driving wrapped vendor CLIs as subprocesses (Anthropic and OpenAI providers) or forwarding to upstream HTTP APIs (DeepSeek, MiniMax). The gateway preserves multi-turn conversation state per workspace, intercepts tool-use events from wrapped CLIs, and reports them to inbound clients via an extension envelope.

## 2. Users and Use Cases

### 2.1 Primary user

A single developer running Reverso on their own workstation, using the gateway as the endpoint for:

- Cross-vendor experimentation (Codex CLI calling Claude models, Claude Code calling GPT models).
- Third-party agents and IDE plugins that expect HTTP API access.
- Custom scripts and pipelines (including, eventually, the developer's PLM automation stack).
- Direct curl-style testing during gateway development.

### 2.2 Use cases

**UC-1. Cross-vendor CLI invocation.**
The developer runs `codex -p anthropic "explain this diff"` and Codex CLI sends the request to Reverso, which routes it to Claude Code, which generates the response under the Claude Max subscription.

**UC-2. Third-party agent on subscription budget.**
The developer runs Aider configured with `OPENAI_API_BASE=http://127.0.0.1:4000/v1` and Aider's GPT-targeted calls are served by Codex CLI under the ChatGPT Pro subscription.

**UC-3. DeepSeek for cost-sensitive bulk work.**
A custom script calls `POST /v1/chat/completions` with `model: deepseek-reasoner` and Reverso forwards the request to the DeepSeek API.

**UC-4. Tool-use observation.**
The developer reads `x_gateway.observations` from a response and sees the full list of file edits and shell commands the wrapped CLI performed during that turn. This data is also captured in structured logs for later review.

**UC-5. Multi-turn coding session.**
The developer runs a tool that sends 20 sequential prompts to Reverso over an hour, each targeting `claude-sonnet-4-5` from the same workspace. The wrapped Claude Code subprocess persists across all 20 turns, maintaining model context.

## 3. Functional Requirements

### 3.1 Inbound HTTP API

**F-INB-1.** The gateway binds to 127.0.0.1:4000 on startup. Any other bind address in configuration is a startup error.

**F-INB-2.** The gateway exposes `POST /v1/chat/completions`, accepting the OpenAI Chat Completions request shape, returning the OpenAI Chat Completions response shape, including streaming SSE per the OpenAI spec.

**F-INB-3.** The gateway exposes `POST /v1/messages`, accepting the Anthropic Messages request shape, returning the Anthropic Messages response shape, including streaming SSE per the Anthropic spec.

**F-INB-4.** The gateway exposes `GET /v1/models`, returning the list of registered models from `models.yaml` with their capability metadata.

**F-INB-5.** The gateway exposes `GET /health/live` and `GET /health/ready` returning 200 when the gateway and session daemon are responsive.

**F-INB-6.** No authentication is required for any endpoint. The loopback bind is the security boundary.

**F-INB-7.** Inbound requests may include an `x_gateway` extension object in their body containing at minimum a `workspace` field with an absolute filesystem path. If the path does not exist or is not a directory, the gateway returns HTTP 400.

**F-INB-8.** If `x_gateway.workspace` is absent, the gateway uses a configured default workspace.

### 3.2 Routing

**F-ROUTE-1.** Routing is determined by the `model` field of the request body, looked up in the model registry.

**F-ROUTE-2.** Every model in the registry is callable from both inbound surfaces. Surface ↔ backend mismatch triggers body translation (e.g., Anthropic-shape inbound + OpenAI-backend → translate inbound to OpenAI shape, send, translate response back to Anthropic shape).

**F-ROUTE-3.** Unknown models (not in registry) return HTTP 404 with a clear error message.

**F-ROUTE-4.** Models marked `deprecated_after` in the registry continue to work but emit a deprecation warning in `x_gateway`.

### 3.3 Wrapped-CLI providers (Anthropic, OpenAI)

**F-CLI-1.** Sessions are keyed by the tuple (machine, workspace, provider). Machine is the gateway's host identifier (constant for v1).

**F-CLI-2.** On first request for an unknown session key, the gateway spawns a new wrapped-CLI subprocess in the specified workspace directory.

**F-CLI-3.** On subsequent requests for a known session key, the gateway uses the existing subprocess if it is still alive.

**F-CLI-4.** The gateway feeds each turn's latest user message to the subprocess and captures its output. Prior messages in the inbound `messages` array are ignored.

**F-CLI-5.** The gateway's recycle sweeper runs every 60 minutes. A session is recycled if both conditions hold:
- No inbound API request has been served for this session in the past 30 minutes.
- No descendant process of the wrapped CLI subprocess is currently alive.

**F-CLI-6.** Recycling means sending SIGTERM to the wrapped CLI subprocess (allowing 5 seconds for graceful shutdown), then SIGKILL if still alive. The session table entry is removed.

**F-CLI-7.** There is no maximum session age. There is no maximum turn count per session.

**F-CLI-8.** Gateway restart is a hard reset. All session table entries are discarded. All wrapped CLI subprocesses die with the gateway. On next request for any session key, the gateway treats it as a new session.

**F-CLI-9.** Before spawning a new session, the gateway scans for other wrapped-CLI processes whose working directory matches the requested workspace. If found, the gateway logs a structured warning to its log file. The gateway does not refuse the request.

### 3.4 HTTP-forwarded providers (DeepSeek, MiniMax)

**F-HTTP-1.** Requests routed to DeepSeek or MiniMax are forwarded to the upstream API specified in the registry entry, with the API key injected from the macOS Keychain.

**F-HTTP-2.** Body translation is applied if the inbound surface differs from the upstream API shape (e.g., Anthropic-shape inbound → translate to OpenAI shape for DeepSeek's OpenAI-compatible endpoint).

**F-HTTP-3.** Streaming responses are streamed through to the inbound client.

**F-HTTP-4.** HTTP-forwarded providers do not have sessions. Each request is independent.

**F-HTTP-5.** The `x_gateway` envelope on responses from HTTP-forwarded providers has `session_id: null` and `observations: []`.

### 3.5 Tool-use interception (IV-pragmatic)

**F-TOOL-1.** During each turn served by a wrapped CLI, the gateway monitors the CLI's output stream for tool-call events.

**F-TOOL-2.** Detected tool-call events are recorded as observation objects with: tool name, tool arguments (as understood from the CLI's output), result summary (success/failure indicator and a short string), elapsed milliseconds, and sequence number within the turn.

**F-TOOL-3.** Observation objects are returned in `x_gateway.observations` on the response.

**F-TOOL-4.** Inbound clients that declare tools in their request have their tool declarations translated into the wrapped CLI's native tool vocabulary where mappable. Un-mappable declarations are silently dropped with a structured log entry; v1 does not error on un-mappable tools but the response includes a `x_gateway.warnings` array noting which tools were dropped.

**F-TOOL-5.** The wrapped CLI executes its native tools as part of its normal operation. The gateway does not suppress or intercept tool execution. The observations are after-the-fact reporting, not pre-execution mediation.

### 3.6 Response envelope

**F-ENV-1.** Every response (success or error) includes an `x_gateway` object as a top-level extension field in the JSON body.

**F-ENV-2.** The `x_gateway` object has the following shape:

```
{
  "session_id": string | null,
  "machine": string,
  "workspace": string,
  "provider": "anthropic" | "openai" | "deepseek" | "minimax",
  "model_inbound": string,
  "model_backend": string,
  "observations": [Observation],
  "warnings": [string]
}
```

**F-ENV-3.** Observation objects have the following shape:

```
{
  "seq": integer,
  "tool": string,
  "args": object,
  "result_summary": string,
  "ms": integer
}
```

**F-ENV-4.** Non-2xx responses include the standard error envelope of the inbound surface (OpenAI or Anthropic shape) plus the `x_gateway` extension. `x_gateway.observations` is populated up to the point of failure.

### 3.7 Model registry

**F-REG-1.** The registry is a YAML file at `models.yaml` in the repository root.

**F-REG-2.** The registry is loaded on gateway startup. Changes require restart.

**F-REG-3.** Each entry contains:

```yaml
- inbound_id: claude-sonnet-4-5
  backend: anthropic-cli
  backend_model_id: claude-sonnet-4-5
  capabilities:
    supports_streaming: true
    supports_tools: true
    supports_vision: true
    context_window_tokens: 200000
    max_output_tokens: 8192
  deprecated_after: null
  replacement_model: null
  notes: "Default Claude model for general work"
```

**F-REG-4.** Backend kinds: `anthropic-cli`, `openai-cli`, `deepseek-http`, `minimax-http`.

**F-REG-5.** HTTP backend entries additionally specify `api_base` (URL) and `api_key_keychain_name` (the Keychain service identifier for the API key).

**F-REG-6.** `GET /v1/models` returns entries with their full capability metadata, formatted as the OpenAI Models API shape with `x_gateway` extensions for non-standard fields.

### 3.8 Configuration

**F-CFG-1.** The gateway reads runtime configuration from a `config.yaml` file in the repository root.

**F-CFG-2.** Configuration fields:
- `bind_host`: must be `127.0.0.1`. Other values are startup errors.
- `bind_port`: defaults to 4000.
- `default_workspace`: absolute path used when inbound requests omit `x_gateway.workspace`. Defaults to the user's home directory.
- `session_idle_timeout_minutes`: defaults to 30.
- `session_recycle_sweep_minutes`: defaults to 60.
- `log_path`: defaults to `~/Library/Logs/reverso/gateway.log`.
- `log_level`: defaults to `INFO`.

**F-CFG-3.** Secrets are not in `config.yaml`. They live in the macOS Keychain under the namespace `reverso/<KEY_NAME>`.

### 3.9 Logging

**F-LOG-1.** The gateway emits structured JSON logs to its log file.

**F-LOG-2.** Each log entry includes a timestamp, level, event type, session_id (if applicable), and event-specific fields.

**F-LOG-3.** Logged events at INFO level:
- Service start, service stop.
- Session spawn, session recycle.
- Each inbound request (method, path, model, workspace, session_id).
- Each inbound response (status code, latency, observation count).
- Workspace conflict detection (the warning from F-CLI-9).

**F-LOG-4.** Logged events at DEBUG level:
- Full request body (with secrets redacted).
- Full response body (with assistant text truncated to 500 characters).
- Per-tool observation as it is detected.

**F-LOG-5.** Logs are rotated using the OS's `newsyslog` or a similar mechanism configured outside the gateway. The gateway does not implement log rotation internally.

## 4. Non-Functional Requirements

**NFR-1. Latency.** Warm-session turns must complete in no more than 2x the latency of the equivalent direct CLI invocation. Cold-start turns (first request for a new session) may add up to 10 seconds of overhead.

**NFR-2. Memory.** Gateway process at rest (no active sessions) uses less than 200 MB RAM. Each active session adds the wrapped CLI's own footprint plus less than 50 MB of gateway overhead per session.

**NFR-3. Concurrency.** The gateway handles at least 4 concurrent inbound requests across different sessions without blocking.

**NFR-4. Restart time.** Gateway restart from launchd-issued SIGTERM to fully-ready takes less than 10 seconds.

**NFR-5. Reliability.** The gateway must not crash on malformed inbound requests, on wrapped-CLI exit during a turn, or on upstream HTTP errors. All such conditions surface as structured error responses.

**NFR-6. Security.** No secrets are present in any file under version control. The gateway accepts no connection from any address other than 127.0.0.1. No outbound network traffic is initiated by the gateway except DeepSeek and MiniMax API calls; all Anthropic and OpenAI traffic is initiated by the wrapped CLI subprocesses.

**NFR-7. Observability.** All session lifecycle events and all inbound request/response pairs are logged.

## 5. API Specification Details

### 5.1 Inbound request example (OpenAI shape, with workspace extension)

```json
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "claude-sonnet-4-5",
  "messages": [
    {"role": "user", "content": "What does this file do?"}
  ],
  "stream": false,
  "x_gateway": {
    "workspace": "/Users/andre/Ws/Personal/AiTool/loopback"
  }
}
```

### 5.2 Inbound response example (OpenAI shape, with x_gateway extension)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1748275200,
  "model": "claude-sonnet-4-5",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "This file implements..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "x_gateway": {
    "session_id": "anthropic:loopback:abc123",
    "machine": "mac-mini-andre",
    "workspace": "/Users/andre/Ws/Personal/AiTool/loopback",
    "provider": "anthropic",
    "model_inbound": "claude-sonnet-4-5",
    "model_backend": "claude-sonnet-4-5",
    "observations": [
      {
        "seq": 1,
        "tool": "Read",
        "args": {"path": "src/gateway.py"},
        "result_summary": "read 4521 bytes",
        "ms": 12
      }
    ],
    "warnings": []
  }
}
```

Token usage is reported as zero because the gateway cannot accurately count tokens through the wrapped CLI. This is documented as a known limitation.

### 5.3 Error response example (HTTP 500)

```json
{
  "error": {
    "type": "wrapped_cli_crash",
    "message": "Claude Code subprocess exited unexpectedly during turn"
  },
  "x_gateway": {
    "session_id": "anthropic:loopback:abc123",
    "machine": "mac-mini-andre",
    "workspace": "/Users/andre/Ws/Personal/AiTool/loopback",
    "provider": "anthropic",
    "model_inbound": "claude-sonnet-4-5",
    "model_backend": "claude-sonnet-4-5",
    "observations": [
      {
        "seq": 1,
        "tool": "Edit",
        "args": {"path": "src/gateway.py", "old": "...", "new": "..."},
        "result_summary": "ok",
        "ms": 18
      }
    ],
    "warnings": ["wrapped CLI exited before final assistant message"]
  }
}
```

## 6. Known Limitations

- **Token accounting.** Wrapped CLIs do not report token counts in their non-interactive output. The gateway reports zero for prompt_tokens, completion_tokens, and total_tokens. This is invisible to a user who cares about cost (the cost is absorbed by the subscription) but visible to a user who programs against the standard API expecting usage data.
- **Branching conversations.** Editing prior turns in the inbound `messages` array has no effect. The wrapped CLI's session is canonical. To get a fresh conversation, use a different workspace.
- **Multi-tenant sharing.** Multiple inbound clients hitting the same session key share the conversation. There is no per-client isolation. This is intentional for v1.
- **Tool-use translation fidelity.** Inbound tool declarations that do not map to a wrapped CLI's native tools are silently dropped (with a warning in `x_gateway.warnings`). Tools the wrapped CLI ran that the inbound client did not declare appear in `x_gateway.observations` for visibility.
- **Workspace conflict.** If the developer runs Claude Code directly in a workspace that also has an active gateway session, behavior is undefined for both. The gateway logs a warning but does not enforce.
- **Restart loses state.** Gateway restart kills all sessions. Multi-turn work in progress is lost.

## 7. Open Questions (deferred from BRD Section 11)

These are Phase 0 spike work items that may force scope changes:

- Whether `claude -p` supports session-style multi-turn invocation, and what the relationship is between an invocation and the persisted `.claude/` directory in the workspace.
- Whether `codex exec` provides equivalent semantics.
- The exact output format of each CLI's tool-call events.
- Whether each CLI honors a working-directory argument that differs from the gateway's cwd.

Resolution of these questions is gated on Phase 0 of the MVP plan and may necessitate revisions to F-CLI-* requirements.
