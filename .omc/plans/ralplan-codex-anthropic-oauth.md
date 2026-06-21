---
type: ralplan
project: reverso
slug: codex-anthropic-oauth
milestone: 2
status: approved
mode: DELIBERATE
source_prd: .omc/plans/prd-codex-anthropic-oauth.md
consensus_gate:
  complete: true
  iterations: 1
  architect_review: SOUND-WITH-CHANGES (resolved)
  critic_review: APPROVED
  resolved: ["C1 AC3 validate-only default + no-divergence test", "C2 clean cut both configs + named tests + empty-grep gate", "C3 static _CODEX_MODELS seeded in _build_model_index + config-existence exemption + lint-coverage test", "M1 text-only tool ceiling", "M2 clean cut staged last gated on real-credential smoke", "M3 codex_sync reconciled to PRD five"]
related:
  - docs/architecture/adr/0005-bounded-cli-spine.md
  - docs/architecture/adr/0006-anthropic-messages-api-surface.md
  - docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md
generated: 2026-06-21
---

# RALPLAN: Codex GPT models on the Anthropic surface via ChatGPT OAuth (Milestone 2)

Consensus implementation plan for Milestone 2. Source of truth is the approved PRD
(`.omc/plans/prd-codex-anthropic-oauth.md`) and ADR 0007
(`docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`). This plan
inspects real code and proposes a build; it writes no source code. Mode is DELIBERATE
(high-risk: a clean-cut legacy removal plus an unknown OAuth artifact format).

## 1. Requirements Summary

The PRD fixes nine acceptance criteria (PRD lines 89 to 98). Each is tied to a concrete
testable step in Section 5.

1. AC1 Docs-first ADR 0007 exists and precedes code. Already landed
   (`docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`). Mapped to G001.
2. AC2 First-party `CodexAdapter` implements the FROZEN `ProviderAdapter` Protocol
   (`src/reverso/protocols/adapter.py:117` to `:143`) and invokes `codex exec` via
   `cli_spine` for non-streaming and streaming. Mapped to G003.
3. AC3 (REVISED) `CodexOAuthAuth` reads and VALIDATES the ChatGPT OAuth subscription
   artifact and FAILS CLOSED with a structured Anthropic error on missing or expired; the
   Codex CLI authenticates from its own login session (validate-only default, design point
   A3). Token injection is an OPTIONAL upgrade applied ONLY if the G002.0 spike proves Codex
   honors an injection env var. No secret is logged. Mapped to G002. Recorded as an ADR 0007
   addendum (Section 7 Follow-ups).
4. AC4 `POST /v1/messages` with a gpt-* model returns a correct non-streaming Anthropic
   body. Mapped to G003 plus G004 (reuses the M1 translation reached through
   `build_anthropic_app`, `src/reverso/protocols/anthropic_app.py:718`).
5. AC5 `POST /v1/messages` with `stream=true` returns the Anthropic-native SSE sequence.
   Mapped to G003 plus G004.
6. AC6 (REVISED) `tool_use` is GATED per feature_policy to the proven Codex ceiling:
   text-only UNLESS the G002.0/spike-3 work proves structured function-call output from
   `codex exec --json`. Codex emits `command_execution` OBSERVATIONS, not Responses
   function-call output (`codex_cli.py:69` to `:85`), so structured tool_use is the
   optimistic branch, not the default. Mapped to G003 plus G006.
7. AC7 All five gpt model ids resolve to the codex backend on the Anthropic surface and
   are listed by `GET /v1/models`. Mapped to G004.
8. AC8 The Anthropic parity suite is extended to cover the codex backend. Mapped to G006
   (mirrors `tests/integration/test_anthropic_messages_parity.py`).
9. AC9 (REVISED) `openai_cli_provider.py` and the `openai_cli` gpt rows are removed from
   BOTH config files (`config/litellm_config.yaml:64` to `:87` AND `config/models.yaml:82`
   to `:121`); `codex_sync.py` reconciled; the named legacy tests (`test_proxy_app.py:23`,
   `test_cli_provider_streaming.py:7`) are deleted or rewritten; suite stays green and
   `uvx prek` passes. The provable gate is `grep -rn "openai_cli" src config tests` returns
   EMPTY. Mapped to G005 plus G007.

## 2. RALPLAN-DR Summary

### Principles

1. The frozen `ProviderAdapter` Protocol (`src/reverso/protocols/adapter.py:117`) is the
   only boundary the gateway depends on; the Codex backend is a fifth adapter producing
   the internal Responses contract, not a new Protocol method or inbound endpoint.
2. The subprocess safety contract lives once in the bounded CLI spine
   (`src/reverso/protocols/adapters/cli_spine.py`, ADR 0005); the Codex backend reuses it
   and never spawns its own subprocess.
3. Surface exposure is DATA, not a code branch (`SURFACE_BACKENDS` dict opens at
   `src/reverso/protocols/surface_registry.py:29`, the `anthropic` row is `:30`); codex is
   added to that row and is Anthropic-surface-only.
4. Authentication is the ChatGPT subscription OAuth, asserted on a real local artifact and
   fail-closed; it is never a metered API key (mirror of `ClaudeOAuthAuth`,
   `src/reverso/protocols/adapters/claude.py:104`).
5. Token material is never logged; all diagnostics route through the existing redaction
   (`redact_secret` / `redact_mapping`, used in `cli_spine.py:116` and `claude.py:300`).

### Decision Drivers (top 3)

1. Subscription billing fidelity. The chosen path must ride the ChatGPT subscription the
   user already logs in with via `codex login`, not an OpenAI API key (ADR 0007 Driver 1).
2. Reuse over rebuild. The M1 Anthropic translation, streaming, capability gate, and parity
   harness already convert the internal Responses contract to and from Anthropic; the
   adapter must only emit `ResponsesRequest` / `ResponseEnvelope` / `SSEEvent`.
3. Removing circularity. Coexisting with `openai_cli` would keep a circular
   gpt-on-the-Responses-surface route alive; the clean cut removes it (ADR 0007 D4).

### Open design point A: Codex OAuth artifact strategy (the discovery spike)

The Codex ChatGPT OAuth artifact location and format are NOT handled in reverso today
(PRD lines 109 to 110; ADR 0007 Open spike). `ClaudeOAuthAuth` is the template: it reads
the macOS Keychain item `Claude Code-credentials` first, then `~/.claude/.credentials.json`,
and asserts on `accessToken` / `expiresAt` under a top-level key
(`src/reverso/protocols/adapters/claude.py:78` to `:80`, `:152` to `:235`).

DECISION (reframed, iteration 1): A3 validate-only is the DEFAULT. The legacy provider
proves injection is unsupported today: `_invoke_codex` spawns `codex exec` via
`subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)` with NO
`env=` argument (`src/reverso/proxy/openai_cli_provider.py:135`), so there is zero evidence
Codex honors an injected bearer the way `claude` honors `CLAUDE_CODE_OAUTH_TOKEN`
(`claude.py:336`, `:398`). Building token injection as the default would likely be
UNBUILDABLE. A1/A2 (file/Keychain read plus env injection) are treated as an OPTIONAL
upgrade, applied ONLY if the G002.0 spike proves Codex reads a specific injection env var.

- Option A3 (DEFAULT) Validate-only: `CodexOAuthAuth` reads and validates the ChatGPT OAuth
  artifact and fails closed on missing or expired; the Codex CLI authenticates from its own
  `codex login` session for the actual turn (no env injection). The argv runs through the
  spine with the parent env inherited (mirroring how the legacy provider relied on the CLI
  session), not a reverso-injected token.
  Pros: buildable today; still fail-closed because the gate asserts the subscription artifact
  and never an API key; the falsifiable `AuthResolution` shape is unchanged
  (`reverso.protocols.auth`, consumed at `claude.py:177`).
  Cons: the gate is a PRE-FLIGHT validity check, not the credential the CLI actually uses, so
  gate validity and turn success could diverge. This residual risk is bounded by the
  falsifiable coupling test (Section 4 Unit and pre-mortem 1): a turn whose underlying Codex
  session is invalid or expired must surface a STRUCTURED Anthropic error, never a false-green.
- Option A1 (OPTIONAL upgrade) Read a Codex credential file directly (e.g.
  `~/.codex/auth.json`) and inject it into the child env.
  Pros: mirror of `ClaudeOAuthAuth._read_credentials_file` / `resolve` (`claude.py:145` to
  `:228`); aligns gate and turn on one credential.
  Cons: requires the spike to prove a Codex injection env var exists; file path and field
  names unverified until the spike runs.
- Option A2 (OPTIONAL upgrade) Read a macOS Keychain generic-password item and inject it.
  Pros: mirrors `claude.py:126` to `:143` (the `security find-generic-password` path).
  Cons: macOS-only; service name unknown until the spike; same injection precondition as A1.

Resolution: implement G002.0 spike FIRST to determine storage, field names, expiry, and
whether a Codex injection env var exists. Ship A3 as the default. Promote to A1/A2 ONLY on a
positive spike result; the read-and-validate gate, the source-layering, and the fail-closed
`AuthResolution` shape are identical across all three, so the spike outcome changes only
whether the child env carries the bearer, not the gate architecture. Record the A3 default
and any later promotion as an ADR 0007 addendum (Section 7 Follow-ups).

### Open design point B: PORT the legacy event parsing vs REWRITE against codex_cli.py

Two parsers for `codex exec --json` already exist in-tree and disagree on stop logic:
`openai_cli_provider._parse_codex_output` (`src/reverso/proxy/openai_cli_provider.py:86`
to `:115`) and `CodexCLIParser.parse_stream`
(`src/reverso/daemon/parsers/codex_cli.py:25` to `:97`). Both recognise the same grammar:
`thread.started` (thread_id), `item.completed` with `item.type == agent_message`
(assistant text), and `turn.completed`.

- Option B1 PORT the `openai_cli_provider` parsing into the new `CodexAdapter`.
  Pros: it is the path serving gpt today, so its behaviour is field-proven.
  Cons: it is synchronous and string-buffer oriented (`_parse_codex_output(stdout)` over
  `stdout.splitlines()`), bound to LiteLLM types (`ModelResponse`, `GenericStreamingChunk`,
  `openai_cli_provider.py:33` to `:35`), and is being deleted in G005; porting it would
  copy code we are about to remove.
- Option B2 REWRITE the adapter against the async, line-oriented `CodexCLIParser`
  (`codex_cli.py:25`, `parse_stream(stdout_lines: AsyncIterator[str])`).
  Pros: already async and line-based, which matches `stream_bounded_cli`'s
  `AsyncIterator[str]` output (`cli_spine.py:124`); has no LiteLLM coupling; survives the
  clean cut; `turn.completed` already terminates the stream (`codex_cli.py:93`).
  Cons: `CodexCLIParser` aggregates to a final `(text, observations)` tuple and does not
  yield incremental text deltas, so streaming needs a thin per-`agent_message` yield layer
  on top of it (analogous to `_extract_assistant_text` in `claude.py:596`).

Invalidation rationale: Option B1 is DOMINATED. The code it would port is deleted by G005
(ADR 0007 D4), is LiteLLM-coupled, and is synchronous against a streaming spine. Choose
B2: reuse the Codex grammar knowledge embodied in `codex_cli.py` (and cross-check the
event names against `openai_cli_provider._parse_codex_output` before deleting it), adding
only an incremental-yield wrapper. This keeps the adapter aligned with the async spine and
leaves nothing of the deleted module behind.

## 3. Pre-mortem (3 failure scenarios)

1. The gate is advisory and diverges from the actual turn credential. Because A3 validate-only
   is the default (no injection; the legacy provider spawns `codex exec` with NO `env=`,
   `openai_cli_provider.py:135`), `CodexOAuthAuth` validates a PRE-FLIGHT artifact while the
   Codex CLI authenticates from its own session. A user could have a valid-looking artifact
   but an invalid or expired CLI session (or vice versa), producing a false-green gate.
   Detection: the falsifiable coupling test (Section 4 Unit): a turn whose underlying Codex
   session is invalid or expired must surface a STRUCTURED Anthropic error, never a silent
   success. The spine already converts a nonzero CLI exit into a provider-typed error
   (`run_bounded_cli`, `cli_spine.py:111` to `:120`; `stream_bounded_cli`, `cli_spine.py:195`
   to `:205`), which the M1 surface renders as a structured Anthropic error.
   Mitigation: the adapter maps any spine failure to the structured Anthropic error path (no
   false-green). If the spike later proves a Codex injection env var, promote to A1/A2 so the
   gate and the turn share one credential and divergence is closed. If coupling proves
   impossible even then, the gate is explicitly ADVISORY and the residual risk (validity and
   turn-success may diverge) is documented in the ADR 0007 addendum. AC3 stays falsifiable in
   all cases: the gate asserts subscription OAuth and never an API key.
2. The clean-cut openai_cli removal breaks a consumer. Removing
   `openai_cli_provider.py` and the five gpt rows orphans `bootstrap.register_litellm_extensions`
   (`src/reverso/proxy/bootstrap.py:9`, `:16`), which imports and registers `openai_cli`,
   and may break `codex_sync` expectations or a Responses-surface caller. `codex_sync`
   reads live `/v1/models` per prefix and its `GATEWAY_PREFIXES` is `claude, copilot,
   auggie, deepseek` (`src/reverso/codex_sync.py:42`), and its `CODEX_DEFAULT_MODELS` /
   `STATIC_CATALOG_MODELS` already list gpt ids independently of the litellm rows
   (`codex_sync.py:48` to `:68`).
   Detection: `tests/unit/test_proxy_app.py`, `tests/unit/test_cli_provider_streaming.py`,
   and `tests/unit/test_codex_sync.py` plus the full suite in G007.
   Mitigation: in G005 also remove the `openai_cli` import and registration in
   `bootstrap.py` and any test that asserts the legacy gpt path; confirm `codex_sync` does
   not fetch a `codex`/`gpt` prefix (it does not; gpt ids there are static catalog data,
   `codex_sync.py:64` to `:68`) so no live fetch breaks. Keep the git revert ready (ADR 0007
   Consequences accept no in-tree fallback).
3. Codex event grammar does not map cleanly onto the internal Responses contract for
   tool_use or streaming. Codex emits tool activity as `command_execution` observations,
   not as Responses function-call output (`codex_cli.py:69` to `:85`), and emits assistant
   text only at `item.completed` (`agent_message`), which is buffered, not streamed
   token-by-token. The M1 streaming mapper and capability gate expect the internal SSE /
   tool_use contract that the parity harness asserts
   (`tests/integration/test_anthropic_messages_parity.py:166`, `:210`).
   Detection: the extended parity cell for codex in G006 plus the tool_use round-trip test.
   Mitigation: classify codex as TEXT-ONLY by default in the parity matrix, exactly like
   auggie (`test_anthropic_messages_parity.py:22` to `:26`, `:50`). Codex is therefore added
   to the text-only ceiling test (mirror `test_auggie_tools_text_only_ceiling`,
   `test_anthropic_messages_parity.py:234`), NOT to `_TOOL_OUTPUT_PROVIDERS` (`:50`). Gate
   tool_use per feature_policy (mirror `_assert_unsupported`, `:330`) rather than fabricate a
   tool_use block. Structured tool_use is the OPTIMISTIC branch, promoted only if the spike
   proves `codex exec --json` surfaces structured function-call output. Streaming uses the
   `replay_incremental` buffered-to-delta path already proven by the claude adapter
   (`claude.py:507`).

## 4. Expanded Test Plan

### Unit

- `CodexOAuthAuth.resolve` returns `authenticated=False` with a structured reason for:
  missing artifact, no access token, expired token; mirror the `ClaudeOAuthAuth` assertions
  and inject a fake reader through the constructor seam (the `keychain_reader` /
  `credentials_path` parameters at `claude.py:114` to `:124`). Falsifiable-gate test: assert
  `method` is the OAuth method and that no API-key env var is consumed (mirror the
  `_FORBIDDEN_AUTH_ENV` intent at `claude.py:76`).
- FALSIFIABLE COUPLING TEST (A3 default, AC3): with a VALID artifact but a CLI session that
  fails (simulate a nonzero `codex exec` exit via the injected `cli_runner` /
  `stream_cli_runner` seam, mirror `claude.py:287` to `:290`), a turn through `CodexAdapter`
  must raise the provider-typed error and the M1 surface must render a STRUCTURED Anthropic
  error, never a false-green success. This asserts gate validity and turn success do not
  diverge silently; it is the test that justifies shipping A3 validate-only as the default.
- Codex event parsing: feed canned `thread.started` / `item.completed(agent_message)` /
  `turn.completed` lines and assert the assistant text and termination, reusing the grammar
  in `codex_cli.py:61` to `:94`; include a `command_execution` line to assert it is treated
  as a non-emitting observation (text-only ceiling), NOT a tool_use output block.
- Surface routing: `resolve_anthropic_backend("gpt-5.5")` returns `codex` for all five gpt
  ids and a claude id still returns None (`surface_registry.py:124` to `:142`);
  `list_anthropic_surface_models` includes exactly the five gpt ids sourced from the new
  `_CODEX_MODELS` constant, not the config-derived index (`surface_registry.py:166`).
- IMPORT-SAFETY TEST: `import reverso.protocols.surface_registry` does NOT raise after the
  gpt config rows are removed; `cross_check_anthropic_models` (`surface_registry.py:189`,
  run at import `:224`) must exempt the static `_CODEX_MODELS` ids so they are not required
  to exist in `litellm_config.yaml`.
- `build_anthropic_adapters` constructs a `CodexAdapter` and `AnthropicMessagesApp` accepts
  `codex` (extend `anthropic_app.py:718`, `:330` to `:347`).

### Integration

- Extend `tests/integration/test_anthropic_messages_parity.py` PROVIDERS to add `codex`
  (`:41`) over a `FixtureAdapter("codex")`, exercising non-streaming body (`:118`), streaming
  event order (`:151`), the TEXT-ONLY tool ceiling (codex added to the text-only ceiling test
  pattern at `:234`, NOT to `_TOOL_OUTPUT_PROVIDERS` at `:50`), count_tokens (`:261`), and the
  unsupported-feature gates (`:330`). Structured tool_use moves codex into
  `_TOOL_OUTPUT_PROVIDERS` only on a positive spike-3 result.
- Negative test (mirror `tests/integration/test_anthropic_claude_exclusion.py`): a new
  `test_codex_responses_exclusion.py` asserting gpt-* models and the codex backend are NOT
  reachable on the RESPONSES surface (the exact mirror of claude-on-Anthropic). Assert codex
  is absent from the Responses gateway adapter set (`compose.build_adapters`,
  `compose.py:39`) the same way the claude-exclusion suite asserts claude is absent from the
  Anthropic adapters (`test_anthropic_claude_exclusion.py:178` to `:196`).

### E2E (loopback smoke)

- REQUIRED real-credential gate (blocks the G005 deletion PR, see M2 in Section 5): with a
  real `codex login` present, boot the composed app on `127.0.0.1:64946`
  (`src/reverso/proxy/main.py`, `compose.py:1`); `curl POST /v1/messages` with `gpt-5.5`
  non-streaming AND with `stream=true`; assert an Anthropic message body and an SSE sequence,
  BOTH GREEN. This real smoke is a precondition for the clean cut, not an optional check.
- Credential-absent variant: with no valid session, assert the fail-closed structured
  Anthropic error (the A3 gate path).
- `GET /v1/models` lists the five gpt ids on the Anthropic surface.

### Observability

- Redaction assertion is spike-conditional (avoid a vacuous test):
  - Under A3 (default, no injection): there is no reverso-injected bearer in the child env,
    so the test asserts (a) the OAuth artifact token never appears in any captured log line
    when `CodexOAuthAuth` logs an unresolved gate (mirror `redact_mapping` use at
    `claude.py:300`), and (b) redacted stderr on a nonzero CLI exit carries no secret
    (`redact_secret`, `cli_spine.py:116`, `:201`). Do NOT assert an absent injected bearer
    under A3 (it is never set), which would be vacuous.
  - Under A1/A2 (if promoted): additionally assert the injected bearer never appears in logs.
- Assert the kill-on-abandon path fires when the SSE consumer disconnects mid-stream (the
  `finally` in `stream_bounded_cli`, `cli_spine.py:206` to `:217`).

## 5. Implementation Steps (grouped to the 7 ultragoal goals)

### G001 ADR 0007 (DONE)

`docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md` is committed and
Accepted. Satisfies AC1. No further work; this plan must stay consistent with it.

### G002 CodexOAuthAuth including the discovery spike (AC3)

- G002.0 Spike (time-boxed, FIRST): determine the Codex OAuth artifact storage (Keychain
  service name and/or a file such as `~/.codex/auth.json`), token field names, expiry field,
  and whether a `CLAUDE_CODE_OAUTH_TOKEN`-analogous injection env var exists. Record findings
  inline in the new module docstring (no separate doc needed). The spike outcome only decides
  A3-default vs A1/A2-upgrade; it does not change the gate architecture.
- G002.1 Add `CodexOAuthAuth` in a NEW module `src/reverso/protocols/adapters/codex.py`,
  modelled on `ClaudeOAuthAuth` (`claude.py:104` to `:235`): layered artifact read with an
  injectable reader seam (mirror `claude.py:114` to `:175`), `resolve()` returning an
  `AuthResolution` (reuse `reverso.protocols.auth`, imported at `claude.py:46`), an `_is_expired`
  helper (mirror `claude.py:238`), and a `CodexAuthError` (mirror `claude.py:100`). DEFAULT is
  A3 validate-only: the resolver reads and validates the artifact for the gate but does NOT
  build a `bearer_token()` injection path unless the spike promotes to A1/A2 (the legacy
  provider proves no injection today, `openai_cli_provider.py:135` runs with no `env=`).
- G002.2 Define the OAuth method constant and a forbidden-env tuple (mirror `OAUTH_METHOD`
  and `_FORBIDDEN_AUTH_ENV`, `claude.py:71`, `:76`) so the gate stays falsifiable: it asserts
  subscription OAuth and never consumes an OpenAI API key.
- Files: NEW `src/reverso/protocols/adapters/codex.py`; reuses `reverso.protocols.auth`.
- Acceptance: unit tests + the falsifiable coupling test (Section 4) pass; gate fails closed;
  no secret logged. AC3 reworded to "validates the subscription artifact and fails closed".

### G003 CodexAdapter core (AC2, AC4, AC5, AC6)

- G003.1 Add `CodexAdapter` in the same NEW `src/reverso/protocols/adapters/codex.py`,
  implementing the five frozen `ProviderAdapter` methods (`adapter.py:125` to `:143`):
  `create_response`, `stream_response`, `list_models`, `get_response`, `list_input_items`.
  Use `ClaudeAdapter` as the structural template (`claude.py:265` to `:585`): `build_prompt`
  / `buffered_envelope` / `replay_turn` / `replay_incremental` from
  `reverso.protocols.replay` (imported at `claude.py:50`), a `ResponseStore` for
  get_response / list_input_items (`claude.py:285`, `:573` to `:585`), and
  `resolve_profile_model("codex", request.model)` for model mapping (mirror the deepseek and
  claude usage, `claude.py:415`, `deepseek.py:196`).
- G003.2 Non-streaming turn: invoke `codex exec <prompt> --json --model <flag>
  --skip-git-repo-check` through `run_bounded_cli` (`cli_spine.py:67`). Under the A3 default,
  pass the parent env inherited (no reverso-injected bearer); under A1/A2 only, add the
  injected token to the child env per the spike. Reuse the argv shape from the legacy provider
  (`openai_cli_provider.py:123` to `:133`) but drive it through the spine, not `subprocess.run`.
  SANDBOX FLAG DECISION (conscious): the legacy argv carries `-s workspace-write`
  (`openai_cli_provider.py:130` to `:131`). This plan DROPS `-s workspace-write` for the
  Anthropic-surface codex backend: reverso serves a chat/completions turn (text out), not an
  agentic file-mutating task, so the default (read-only) sandbox is the safer, narrower
  grant. If a turn legitimately needs workspace writes, re-add the flag as a deliberate
  follow-up. Parse with the chosen B2 parser (the `codex_cli.py` grammar) into a
  `ResponseEnvelope` (`buffered_envelope`, mirror `claude.py:419`). Offload the blocking call
  with `asyncio.to_thread` (mirror `claude.py:418`).
- G003.3 Streaming: drive `stream_bounded_cli` (`cli_spine.py:124`), wrap the
  `CodexCLIParser` grammar in an incremental per-`agent_message` yield (analogous to
  `_extract_assistant_text`, `claude.py:596`), and feed `replay_incremental`
  (`claude.py:507`) with the same finalize callable shape (`claude.py:491`). Reuse the
  buffered-fallback contract for a pre-first-chunk failure (`claude.py:427` to `:473`).
- G003.4 `list_models`: return the five gpt ids as a `ModelList` (`adapter.py:94`); no live
  upstream call is required (unlike claude's Anthropic listing), so return a static list of
  the configured gpt ids.
- Files: NEW `src/reverso/protocols/adapters/codex.py` (adapter + auth in one module, mirror
  of `claude.py`).
- Acceptance: AC2, AC4, AC5, AC6 covered by the parity cells in G006.

### G004 Surface exposure and routing (AC4, AC5, AC7)

- G004.1 Add `codex` to the Anthropic surface DATA row: the `anthropic` entry in
  `SURFACE_BACKENDS` (`src/reverso/protocols/surface_registry.py:30`) becomes
  `frozenset({"copilot", "deepseek", "auggie", "codex"})` (dict opens at `:29`).
- G004.2 (REVISED for C3) Route the five gpt ids to `codex` INDEPENDENT of config, because
  G005 removes the gpt config rows and `cross_check_anthropic_models` runs at IMPORT
  (`surface_registry.py:224`) asserting every indexed model exists in `litellm_config.yaml`.
  Seeding gpt into the config-derived `_MODEL_INDEX` while the rows are gone would raise
  `RuntimeError` at import (`surface_registry.py:209` to `:214`). Exact changes:
  (a) Add a module-level constant `_CODEX_MODELS: dict[str, str]` mapping the five ids
      (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, `gpt-4.1`) to `"codex"`,
      defined beside `_DEEPSEEK_PREFIX` / `_AUGGIE_PREFIX` (`surface_registry.py:44` to `:45`).
  (b) Seed `_CODEX_MODELS` INSIDE `_build_model_index` (`surface_registry.py:101` to `:117`),
      not only into the module-level `_MODEL_INDEX` at `:121`. This is required because
      `cross_check_anthropic_models` rebuilds a `fresh_index` via `_build_model_index(path)`
      (`:202`) independent of the module-level dict; seeding inside the builder makes BOTH the
      module-level `_MODEL_INDEX` and `cross_check`'s `fresh_index` carry the codex ids
      consistently, so resolution is config-independent AND codex routing stays lint-covered.
      (Prefix-collision check: none of `copilot`/`deepseek`/`auggie`/`claude` start with `gpt`,
      and `_is_claude_model` at `:66` does not match any gpt id, so no fail-closed conflict.)
  (c) Source the codex rows in `list_anthropic_surface_models` from `_CODEX_MODELS` / the seeded
      index, NOT from a config lookup (it reads `_MODEL_INDEX` at `:182`; the seeded entries make
      the five gpt ids appear there with backend `codex`).
  (d) AMEND `cross_check_anthropic_models` (`surface_registry.py:189` to `:220`) to EXEMPT the
      static `_CODEX_MODELS` ids from ONLY the config-existence assertion (the
      `model_name not in config_names` check at `:209` to `:214`), so `import surface_registry`
      does NOT raise after the gpt rows are removed. CRITICAL: do NOT exempt them from the
      backend-membership assertion (`:215` to `:220`); that lint MUST stay active for codex so
      routing drift is caught at import.
  (e) Falsifiable lint-coverage test: a unit test that removes `codex` from
      `SURFACE_BACKENDS["anthropic"]` while leaving it in `_CODEX_MODELS` MUST cause
      `cross_check_anthropic_models()` to raise `RuntimeError` at import, proving codex routing
      is lint-covered (not a silent no-op).
- G004.3 Register `CodexAdapter` on the Anthropic surface: add `codex` to
  `build_anthropic_adapters` (`src/reverso/protocols/anthropic_app.py:718`) and confirm
  `AnthropicMessagesApp` accepts it now that it is in `_ANTHROPIC_SURFACE_BACKENDS`
  (`anthropic_app.py:90`, `:330` to `:347`). The `_PROFILE_PREFIXES` set (`anthropic_app.py:91`)
  picks up the new backend automatically since it derives from `SURFACE_BACKENDS`.
- G004.4 Do NOT register `CodexAdapter` in `compose.build_adapters`
  (`src/reverso/proxy/compose.py:39` to `:55`): codex is Anthropic-surface-only, the mirror
  of claude being Responses-surface-only.
- Files: `src/reverso/protocols/surface_registry.py`, `src/reverso/protocols/anthropic_app.py`.
- Acceptance: AC7 routing and listing; AC4 / AC5 reachability through the M1 surface.

### G005 Clean-cut openai_cli removal plus codex_sync reconcile (AC9)

SEQUENCING (M2): G005 is the LAST commit of the milestone and its deletion PR is BLOCKED
until the REQUIRED real-credential loopback smoke (Section 4 E2E: gpt-5.5 non-streaming AND
streaming, both GREEN against a real `codex login`) has passed. The clean cut leaves no
in-tree fallback (ADR 0007 Consequences), so the new path must be proven live first.

- G005.1 Delete `src/reverso/proxy/openai_cli_provider.py`.
- G005.2 Remove the `openai_cli` import and registration in `src/reverso/proxy/bootstrap.py:9`
  and `:16` (drop the `"openai_cli": openai_cli` entry; keep `anthropic_cli`).
- G005.3 (REVISED for C2) Remove the five `openai_cli` gpt rows from BOTH config files:
  `config/litellm_config.yaml:64` to `:87` AND `config/models.yaml:82` to `:121` (a second
  full copy). Authoritative-config note: the runtime config is the one `reverso.proxy.main`
  resolves and `surface_registry._resolve_config_path` reads (honoring `REVERSO_CONFIG`,
  defaulting to `config/litellm_config.yaml`, `surface_registry.py:48` to `:58`);
  `config/models.yaml` is a non-authoritative duplicate that must be kept in sync, so both
  are cleaned to satisfy the empty-grep gate.
- G005.4 (REVISED for M3) Reconcile `src/reverso/codex_sync.py`. `GATEWAY_PREFIXES`
  (`codex_sync.py:42`) has no gpt/codex prefix, so no live fetch breaks. But
  `CODEX_DEFAULT_MODELS` (`codex_sync.py:48` to `:54`) lists a DIFFERENT set than the PRD five:
  it has `gpt-5.3-codex` and `gpt-5-mini` and OMITS `gpt-4.1`. Decision: reconcile
  `CODEX_DEFAULT_MODELS` to the PRD five exactly
  (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark`, `gpt-4.1`) so the Codex TUI
  catalog matches the models reverso now serves first-party; update `STATIC_CATALOG_MODELS`
  (`codex_sync.py:64` to `:68`) accordingly and add a comment that gpt is served first-party on
  the Anthropic surface, not via `openai_cli`. Update `tests/unit/test_codex_sync.py` fixtures
  that assert the old id set (e.g. the NUX/catalog expectations referencing `gpt-5-mini` and
  the absence of `gpt-4.1`) to the reconciled five.
- G005.5 (REVISED for C2) Delete or rewrite the named legacy tests so the empty-grep gate
  holds: `tests/unit/test_cli_provider_streaming.py` imports and exercises
  `openai_cli_provider` (`:7`, `:64`, `:69`, `:91`) and must drop the openai_cli portions (or
  be deleted if it only covers openai_cli streaming); `tests/unit/test_proxy_app.py:23`
  asserts `"openai_cli" in litellm.provider_list` and must remove that assertion.
- Files: delete `openai_cli_provider.py`; edit `bootstrap.py`, `config/litellm_config.yaml`,
  `config/models.yaml`, `codex_sync.py`, `tests/unit/test_codex_sync.py`,
  `tests/unit/test_proxy_app.py`, `tests/unit/test_cli_provider_streaming.py`.
- Acceptance: AC9; `grep -rn "openai_cli" src config tests` returns EMPTY; suite green;
  `uvx prek` passes.

### G006 Parity plus negative test (AC6, AC8)

- G006.1 Extend `tests/integration/test_anthropic_messages_parity.py`: add `codex` to
  `PROVIDERS` (`:41`), define the codex feature ceiling alongside the existing per-backend
  ceilings (`:22` to `:26`), and add a gpt model id for the bare-path resolution case (mirror
  `_DEEPSEEK_MODEL`, `:46`). Cover non-streaming (`:118`), streaming (`:151`), tool_use
  (`:210` if Codex surfaces tool calls, else the text-only ceiling pattern at `:234`),
  count_tokens (`:261`), and unsupported gates (`:330`).
- G006.2 New `tests/integration/test_codex_responses_exclusion.py` mirroring
  `tests/integration/test_anthropic_claude_exclusion.py`: assert gpt-* and codex are NOT on
  the Responses surface (codex absent from `compose.build_adapters`, `compose.py:39`), the
  exact reverse of the claude-on-Anthropic exclusion (`test_anthropic_claude_exclusion.py:178`).
- Files: edit `tests/integration/test_anthropic_messages_parity.py`; NEW
  `tests/integration/test_codex_responses_exclusion.py`.
- Acceptance: AC6, AC8.

### G007 Final QA (AC9 closure)

- Run the full suite and the loopback smoke (Section 8). Confirm zero `openai_cli`
  references remain (`grep -rn openai_cli src config tests`). Confirm `uvx prek run
  --all-files` passes. Confirm token redaction and kill-on-abandon (Section 4 Observability).

## 6. Risks and Mitigations

| Risk | Evidence | Mitigation |
|------|----------|------------|
| Advisory gate divergence (A3 default) | legacy spawns with no `env=` (`openai_cli_provider.py:135`); no evidence Codex honors an injected bearer | Ship A3 validate-only; falsifiable coupling test asserts a failed CLI session surfaces a structured Anthropic error (no false-green); promote to A1/A2 only if the spike proves injection; else document the gate as advisory in the ADR addendum |
| Clean-cut rollback (no fallback) | ADR 0007 D4 / Consequences; `bootstrap.py:9` registers openai_cli | REQUIRED real-credential loopback smoke gates the G005 deletion PR (M2); fast git revert; remove bootstrap registration in G005.2 |
| Import-time crash from routing | `cross_check_anthropic_models` runs at import (`surface_registry.py:224`) and asserts indexed models exist in config (`:209` to `:214`); G005 removes gpt rows | Route gpt via static `_CODEX_MODELS` seeded into `_MODEL_INDEX` and EXEMPT it from the config-existence check (G004.2 a-d) |
| Incomplete clean cut | gpt rows in BOTH `litellm_config.yaml:64` and `models.yaml:82`; `test_proxy_app.py:23`, `test_cli_provider_streaming.py:7` reference openai_cli | Remove rows from both configs; delete/rewrite both named tests; empty-grep gate (G005, G007) |
| Streaming / tool fidelity | Codex buffers text at `item.completed`; tools surface as `command_execution` (`codex_cli.py:69`, `:87`) | Default codex to TEXT-ONLY ceiling (mirror auggie); add to the text-only ceiling test, not `_TOOL_OUTPUT_PROVIDERS`; structured tool_use is the optimistic spike branch; streaming uses `replay_incremental` (`claude.py:507`) |
| codex_sync model-set drift | `CODEX_DEFAULT_MODELS` differs from the PRD five (`codex_sync.py:48` to `:54`) | Reconcile to the PRD five exactly and update `test_codex_sync.py` (G005.4, M3) |
| Surface-exclusion enforcement | claude exclusion is data + negative test (`surface_registry.py:36`, `test_anthropic_claude_exclusion.py`) | codex absent from `compose.build_adapters`; new Responses-exclusion negative test (G006.2) |
| Parser duplication / dead code | two parsers (`openai_cli_provider.py:86`, `codex_cli.py:25`) | Adopt B2 (rewrite against `codex_cli.py`); delete `openai_cli_provider.py` in G005 |

## 7. ADR Section

Consistent with `docs/architecture/adr/0007-codex-anthropic-surface-via-chatgpt-oauth.md`.

- Decision: Add a first-party `CodexAdapter` (plus `CodexOAuthAuth`) in
  `src/reverso/protocols/adapters/codex.py` that produces the internal Responses contract
  from `codex exec` over the bounded CLI spine under ChatGPT OAuth, exposed Anthropic-surface-only
  as one `SURFACE_BACKENDS` row, replacing the legacy `openai_cli` path with a clean cut.
- Drivers: subscription billing fidelity (no API key); the frozen `ProviderAdapter` Protocol
  stays frozen; ADR 0005 owns subprocess safety (reuse the spine); ADR 0006 made surface
  exposure data (one row); the clean cut removes the circular gpt-on-Responses route.
- Alternatives considered: direct OpenAI Platform Responses API via `openai-python` plus an
  API key (rejected, metered key contradicts the subscription premise); direct HTTP to the
  ChatGPT backend without the CLI (rejected, unsupported protocol); coexist with `openai_cli`
  (rejected, keeps the circular route). For the parser: PORT the legacy parser (rejected, it
  is deleted in G005 and is LiteLLM-coupled and synchronous) vs REWRITE against
  `codex_cli.py` (chosen).
- Why chosen: it mirrors the proven `ClaudeOAuthAuth` / `ClaudeAdapter` template, reuses the
  M1 translation, streaming, gate, and parity harness unchanged, and keeps the OpenAI SDK and
  Platform API out of the dependency surface.
- Consequences: a fifth adapter (codex) exposed only on the Anthropic surface; gpt coverage
  under the ChatGPT subscription with no new endpoint and no Protocol change; no legacy gpt
  fallback after the clean cut (git revert is the rollback); `codex_sync.py` and
  `litellm_config.yaml` reconciled.
- Follow-ups (ADR 0007 ADDENDUM items for this iteration):
  - AC3 is reworded: the gate VALIDATES the subscription artifact and FAILS CLOSED (it does
    not "inject the token"). A3 validate-only is the default; the Codex CLI authenticates from
    its own session. Document that the gate is a pre-flight check coupled to the turn via the
    falsifiable coupling test; if injection is later proven, promote to A1/A2 so gate and turn
    share one credential; if coupling is impossible, the gate is explicitly ADVISORY and the
    residual risk is recorded here.
  - Sandbox: the Anthropic-surface codex backend drops the legacy `-s workspace-write` flag
    (read-only sandbox default); re-adding it is a deliberate follow-up.
  - codex_sync `CODEX_DEFAULT_MODELS` is reconciled to the PRD five ids.
  - Backend-key naming: the canonical token is `codex` (NOT `codex-cli`) across this plan,
    `build_adapters`/`build_anthropic_adapters`, `FixtureAdapter`, and the profile prefix; the
    `surface_registry.py` module docstring and ADR 0007 D3 say "codex-cli" in prose and should
    be aligned to `codex` (a docstring/ADR wording fix, not a code-behavior change).
  - resolve the OAuth artifact spike before `CodexOAuthAuth` (G002.0) and record the outcome
    in the codex module docstring; extend the parity suite (G006); add the Responses-exclusion
    negative test (G006.2); confirm image input feasibility through the Codex CLI and gate per
    feature policy until proven.

## 8. Verification Steps

- Unit and integration tests:
  - `uv run pytest tests/integration/test_anthropic_messages_parity.py -q`
  - `uv run pytest tests/integration/test_codex_responses_exclusion.py -q`
  - `uv run pytest tests/unit/test_codex_sync.py tests/unit/test_proxy_app.py -q`
  - `uv run pytest -q` (full suite stays green for AC9)
- Lint and pre-commit gate: `uvx prek run --all-files`
- Dead-code gate (AC9): `grep -rn "openai_cli" src config tests` returns EMPTY after G005
  (covers both `config/litellm_config.yaml` and `config/models.yaml` and the named tests).
- Import-safety check (C3): `uv run python -c "import reverso.protocols.surface_registry"`
  exits 0 after the gpt config rows are removed (the static `_CODEX_MODELS` exemption in
  `cross_check_anthropic_models` prevents the import-time `RuntimeError`).
- REQUIRED real-credential loopback smoke against `127.0.0.1:64946` (`compose.py:1`), the M2
  gate that BLOCKS the G005 deletion PR until both pass GREEN:
  - `curl -s -X POST http://127.0.0.1:64946/v1/messages -H 'content-type: application/json'
    -d '{"model":"gpt-5.5","max_tokens":64,"messages":[{"role":"user","content":"Say hi."}]}'`
    -> Anthropic non-streaming body (AC4).
  - same with `"stream":true` -> `text/event-stream` SSE sequence (AC5).
  - `curl -s http://127.0.0.1:64946/v1/models` -> lists the five gpt ids (AC7).
- OAuth gate without real credentials (AC3, fail-closed): drive `CodexOAuthAuth.resolve()`
  with an injected reader returning a missing or expired artifact (the constructor seam
  mirrors `claude.py:114` to `:124`) and assert `authenticated=False` with a structured reason.
- A3 coupling test without real credentials (AC3, no false-green): with a VALID artifact but a
  simulated failing `codex exec` (injected `cli_runner` raising / nonzero exit, seam at
  `claude.py:287` to `:290`), assert the turn surfaces a STRUCTURED Anthropic error, not a
  silent success. No real ChatGPT login is required for either gate test.

## 9. Changelog (consensus iteration 1)

- C1: AC3 reworded to "validates the subscription artifact and fails closed"; A3 validate-only
  is now the DEFAULT (legacy spawns with no `env=`, `openai_cli_provider.py:135`); A1/A2
  injection is an optional spike-gated upgrade; added a falsifiable coupling test (failed CLI
  session must surface a structured Anthropic error, no false-green); recorded as an ADR
  addendum; pre-mortem 1 rewritten around advisory-gate divergence.
- C2: AC9 / G005 now remove gpt rows from BOTH `config/litellm_config.yaml:64-87` AND
  `config/models.yaml:82-121`; named `test_proxy_app.py:23` and `test_cli_provider_streaming.py:7`
  for deletion/rewrite; stated `litellm_config.yaml` is the runtime-authoritative config;
  empty-grep is the provable gate.
- C3: G004.2 rewritten with exact steps (a-d) - add `_CODEX_MODELS` constant, seed into
  `_MODEL_INDEX`, source `list_anthropic_surface_models` from it, and exempt it in
  `cross_check_anthropic_models` so `import surface_registry` does not raise; confirmed no
  prefix collision with gpt.
- M1: AC6 reframed to text-only ceiling by default; codex added to the text-only ceiling test
  (mirror `test_auggie_tools_text_only_ceiling`), not `_TOOL_OUTPUT_PROVIDERS`.
- M2: the G005 openai_cli deletion is the LAST commit, BLOCKED on a REQUIRED real-credential
  loopback smoke (gpt-5.5 non-streaming + streaming GREEN).
- M3: `CODEX_DEFAULT_MODELS` reconciled to the PRD five ids exactly; `test_codex_sync.py`
  fixtures updated.
- Nice-to-have: backend-key naming aligned on `codex` (ADR/docstring `codex-cli` wording to be
  fixed); observability redaction test made spike-conditional (no vacuous absent-bearer
  assertion under A3); sandbox `-s workspace-write` drop made a conscious documented decision;
  off-by-one fixed (`SURFACE_BACKENDS` anthropic row is `surface_registry.py:30`).

Status: pending approval. Route to Architect and Critic.
