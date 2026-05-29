# Deep Interview Spec: Reverso Gateway - AI-SDLC Bootstrap

## Metadata
- Interview ID: reverso-sdlc-2026-05-27
- Rounds: 3
- Final Ambiguity Score: 16.5%
- Type: brownfield workspace / greenfield sub-project
- Generated: 2026-05-27
- Threshold: 0.20
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|---|---|---|---|
| Goal Clarity | 0.90 | 35% | 0.315 |
| Constraint Clarity | 0.80 | 25% | 0.200 |
| Success Criteria | 0.80 | 25% | 0.200 |
| Context Clarity | 0.80 | 15% | 0.120 |
| **Total Clarity** | | | **0.835** |
| **Ambiguity** | | | **16.5%** |

## Topology
| Component | Status | Description | Coverage |
|---|---|---|---|
| Repo Setup | active | Extract reverso.zip into reverso/, register in setup.sh LOCAL_DIRS | Git init already present; no remote yet; BRD intends future public GitHub repo |
| SDLC Init | active | Run deepinit on reverso/ to generate AGENTS.md from the 4 spec docs | deepinit skill is the concrete action for "ai-sdlc-init" |
| Development Loop | active | Drive full MVP Phase 0-3 implementation from spec docs via ralph/team | Phase exit criteria per 04-mvp.md; replaces existing LiteLLM shim infrastructure |

## Goal
Build Reverso - a subscription-backed local LLM gateway that:
1. Runs on `127.0.0.1:4000` as a single process
2. Wraps Claude Code CLI and Codex CLI as session-managed subprocess workers (subscription providers)
3. HTTP-forwards DeepSeek and MiniMax (absorbing the existing shim infrastructure currently on ports 48731/49731 and 48737/49737)
4. Exposes standard OpenAI and Anthropic HTTP API surfaces with cross-vendor body translation
5. Maintains multi-turn session state per (machine, workspace, provider) tuple
6. Intercepts and reports tool-use events from wrapped CLIs via `x_gateway` envelope

**Replacement scope:** Reverso replaces the existing `codex-litellm-responses-shim` infrastructure. The existing minimax/deepseek LaunchAgents (`com.andres.codex-litellm-minimax.plist`, `com.andres.codex-litellm-deepseek.plist`) will be decommissioned. Codex `config.toml` model providers will be updated to point to `http://127.0.0.1:4000`.

## Constraints
- Bind exclusively to `127.0.0.1:4000`. No other bind address permitted. No auth (loopback is the security boundary).
- Single user, single machine (Mac). Not designed for sharing, resale, or network exposure.
- Python + LiteLLM + asyncio stack (established by architecture doc).
- The existing shim's core normalization logic (`normalize_function_call_arguments`, `sanitize_input_tool_sequence`, `compact_input_items`, `strip_think_blocks`) must be ported or superseded. The logic lives at `~/.local/bin/codex-litellm-responses-shim`.
- Secrets stay out of version control. Use macOS Keychain for `MINIMAX_ANTHROPIC_API_KEY`, `DEEPSEEK_ANTHROPIC_API_KEY`. Template from `~/.config/litellm/codex-env.sh` (mode 0600).
- `models.yaml` must cover all 8 planned providers: claude-sonnet-4-x, claude-opus-4-x, gpt-5.x variants (via Codex), deepseek-reasoner, deepseek-chat, MiniMax aliases.
- LaunchAgent for Reverso replaces the two existing LaunchAgents. One plist for LiteLLM process + one for session daemon.
- `reverso/` registered in `setup.sh` as `LOCAL_DIRS` (no GitHub remote yet).
- `ai-sdlc-init` = `deepinit` skill. Run after zip extraction.

## Non-Goals
- Authentication or network exposure (this is strictly localhost personal use).
- Multi-machine session sharing (v2 concern).
- Frontend UI or dashboard.
- Support for providers beyond the 8 in the registry.
- Phase 4 production-readiness work (out of scope for this loop).

## Acceptance Criteria

### Phase 0 - Spike
- [ ] Determined how to invoke `claude` and `codex` non-interactively with a user prompt
- [ ] Documented output format for plain text and tool-use cases for both CLIs
- [ ] Confirmed per-workspace isolation (running in different dirs does not pollute each other)
- [ ] `docs/spike-notes.md` written with answers to Q-Spike-1 through Q-Spike-6
- [ ] Go/no-go decision on v1-full (tool interception) or v1-small (sessions only)

### Phase 1 - Skeleton
- [ ] `models.yaml` with all 8 planned entries
- [ ] `config.yaml` with defaults
- [ ] `litellm_config.yaml` referencing `models.yaml`
- [ ] `anthropic_cli_provider.py` and `openai_cli_provider.py` (stateless, one subprocess per request)
- [ ] DeepSeek and MiniMax HTTP-forwarded natively via LiteLLM (no custom code)
- [ ] `x_gateway` envelope on all responses (`observations: []`, `session_id: null` for phase 1)
- [ ] LaunchAgent plist for LiteLLM process
- [ ] Keychain integration for secrets
- [ ] Codex `config.toml` updated with `reverso_gateway` provider pointing to `http://127.0.0.1:4000`
- [ ] `curl` smoke test: POST to `/v1/chat/completions` with deepseek model returns valid response
- [ ] `curl` smoke test: POST to `/v1/messages` with claude model returns valid response
- [ ] `README.md` with quick-start instructions

### Phase 2 - Session Daemon
- [ ] Session daemon process implemented
- [ ] Unix-domain socket at `~/Library/Application Support/reverso/daemon.sock`
- [ ] Session table with (machine, workspace, provider) keys
- [ ] Wrapped CLI subprocesses persist across turns (multi-turn test: 5 sequential prompts, same workspace)
- [ ] Idle detection and subprocess recycle working
- [ ] LaunchAgent for daemon added
- [ ] `x_gateway.session_id` populated on all responses

### Phase 3 - Tool Interception
- [ ] Tool-use events from wrapped CLIs parsed into structured observations
- [ ] `x_gateway.observations` populated with file edits and shell commands
- [ ] Structured logs capturing observations per session
- [ ] Decision: if spike showed tool parsing is not feasible, v1-small is acceptable and this phase is marked as deferred with documented rationale

## Technical Context

### Existing infrastructure to absorb or replace
- `~/.local/bin/codex-litellm-responses-shim`: Core normalization logic. Port relevant functions to Reverso's provider layer.
- `~/.config/litellm/minimax-codex.yaml` and `deepseek-codex.yaml`: Model alias tables. Merge into `reverso/models.yaml`.
- `~/.config/litellm/codex-env.sh`: Secret names (`MINIMAX_ANTHROPIC_API_KEY`, `DEEPSEEK_ANTHROPIC_API_KEY`). Reference, do not copy; migrate to Keychain.
- `~/.codex/config.toml`: Update `model_providers` and `profiles` sections to point at `http://127.0.0.1:4000` instead of `48731`/`48737`.
- `com.andres.codex-litellm-minimax.plist`, `com.andres.codex-litellm-deepseek.plist`: Decommission after Reverso's LaunchAgent is verified working.

### Architecture
- Two long-lived processes: LiteLLM proxy (port 4000) and Session Daemon (UDS socket).
- Communication via `httpx` with `transport=httpx.HTTPTransport(uds=...)`.
- Session key: `(machine, workspace, provider)`.
- Spec docs in `reverso/docs/`: `01-brd.md`, `02-prd.md`, `03-architecture.md`, `04-mvp.md`.

### Repository structure (per architecture doc section 7.2)
```
reverso/
  docs/              # 01-brd.md 02-prd.md 03-architecture.md 04-mvp.md
  reverso/           # Python package
    providers/       # anthropic_cli_provider.py openai_cli_provider.py
    daemon/          # session_daemon.py session_table.py
    middleware/      # x_gateway.py
  config/            # models.yaml config.yaml litellm_config.yaml
  launchd/           # plist templates
  tests/
  README.md
  pyproject.toml
```

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|---|---|---|---|
| Gateway | core domain | port=4000, bind=127.0.0.1, config_path | owns LiteLLMProcess, SessionDaemon |
| LiteLLMProcess | core domain | port, config_file, custom_providers | uses SessionDaemon via UDS |
| SessionDaemon | core domain | socket_path, session_table | owns WrappedCLISessions |
| Session | core domain | key=(machine,workspace,provider), subprocess, idle_timeout | holds WrappedCLIProcess |
| WrappedCLIProcess | core domain | cli_type=(claude,codex), workspace, pid | child of Session |
| ModelRegistry | supporting | models_yaml_path, model_list | loaded by LiteLLMProcess |
| XGatewayEnvelope | supporting | session_id, observations, workspace | wraps all responses |
| Observation | supporting | type=(file_edit,shell_cmd,read), timestamp, detail | child of XGatewayEnvelope |
| Provider | external system | type=(subscription,http_forward), endpoint | registered in ModelRegistry |

## Bootstrap Sequence

The concrete execution order for the ai-sdlc loop:

1. **Extract zip**: `unzip reverso.zip -d reverso/docs/` (place spec docs under docs/)
2. **Register in setup.sh**: Add `"reverso"` to `LOCAL_DIRS` array
3. **deepinit**: Run `deepinit` skill on `reverso/` to generate AGENTS.md from the 4 spec docs
4. **Development loop** (ralph or team): Execute Phase 0 → Phase 1 → Phase 2 → Phase 3 using this spec + the 4 spec docs as the task definition

## Interview Transcript
<details>
<summary>Full Q&A (3 rounds)</summary>

### Round 0 (Topology)
**Q:** Is this topology right? 1. Repo setup, 2. SDLC init, 3. Development loop?
**A:** Yes, plus: pull LiteLLM information from existing codex integration (detailed architecture provided for existing MiniMax/DeepSeek shim on ports 48731/49731 and 48737/49737)
**Ambiguity:** not scored yet

### Round 1
**Q:** How should Reverso coexist with the existing LiteLLM shim?
**A:** Reverso replaces the existing shim. All functionality absorbed into Reverso at port 4000. Existing LaunchAgents decommissioned.
**Ambiguity:** 31%

### Round 2
**Q:** What phase should the ai-sdlc loop target?
**A:** Full MVP (Phase 0-3) — sessions + tool interception
**Ambiguity:** 23%

### Round 3
**Q:** git remote (REPOS vs LOCAL_DIRS)? And what is "ai-sdlc-init"?
**A:** "ai-sdlc-init" = deepinit skill. git remote question not explicitly answered; defaulting to LOCAL_DIRS.
**Ambiguity:** 16.5% ✅

</details>
