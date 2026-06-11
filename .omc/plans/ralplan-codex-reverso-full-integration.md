---
title: "RALPLAN: Codex full integration with the four reverso providers"
status: approved
consensus: architect-approved-critic-approved
approved: 2026-06-10 (user, execution via /team)
source_spec: .omc/specs/deep-interview-codex-reverso-full-integration.md
generated: 2026-06-10
mode: consensus-short
iteration: 2
---

# RALPLAN: Codex full integration with the four reverso providers

## Requirements Summary

Source: `.omc/specs/deep-interview-codex-reverso-full-integration.md` (deep interview PASSED at 18% ambiguity).

Make the Codex CLI a fully integrated client of the reverso gateway (127.0.0.1:64946) for claude, copilot, auggie, and deepseek:

1. **Context integration**: multi-turn memory, workspace context, context window accounting, and `codex resume` work per provider.
2. **Model listing**: live per-provider models become selectable from Codex via a researched mechanism ladder (native Codex support, else config sync tool, else documented -m/profiles).
3. **SSE/streaming**: incremental streaming where feasible (claude via CLI stream output; auggie buffered with documented limitation if its CLI cannot stream; copilot/deepseek verified as already incremental).
4. **Full Responses parity**: the entire OpenAI Responses surface implemented or translated per provider; physically infeasible features return structured 400 `unsupported_feature` errors naming provider and feature.

Done gate (all three): scripted E2E Codex matrix, parity matrix doc, and green unit plus integration suites.

## RALPLAN-DR Summary

### Principles

1. **Evidence before code**: verify actual Codex and provider CLI behavior before building anything conditional (store persistence, /model mechanism, auggie streaming).
2. **Honest surface**: explicit structured `unsupported_feature` errors over silent degradation or fabricated results.
3. **Frozen seams**: the ProviderAdapter Protocol (create_response, stream_response, list_models, get_response, list_input_items) is never modified; all new behavior lives behind or beside it.
4. **Local-only and secret-safe**: bind 127.0.0.1:64946 only; no secrets in version control or logs; token material never printed.
5. **Test-backed parity**: every added behavior is fixture-tested; the E2E matrix is a repeatable script, not a one-off manual check.

### Decision Drivers (top 3)

1. The user explicitly chose the full Responses parity bar (interview Round 2), beyond the Codex-critical subset.
2. Hard Codex-side constraints (TUI /model picker, client-side transcripts) bound what gateway work can achieve; verify-first policies prevent wasted effort.
3. AGENTS.md hard rules and the frozen adapter Protocol constrain how parity machinery can be introduced.

### Viable Options

**Option A: Research-gated phased lanes (chosen)**
Approach: Phase A answers the four open empirical questions (codex resume vs restarted gateway, Codex /model support, auggie CLI streaming, exact Responses surface enumeration); Phase B implements per-component lanes conditioned on those answers; Phase C assembles the E2E matrix, parity doc, and green suites.
Pros: no unneeded persistence or sync tooling; honors the spec's verify-first constraints; lanes parallelize inside Phase B.
Cons: Phase A serializes ahead of Phase B; slightly longer wall clock than building everything speculatively.

**Option B: Build-everything-up-front**
Approach: implement disk persistence, config sync tool, claude streaming, and full parity simultaneously without research gates.
Pros: maximal parallelism from the start.
Cons: violates the spec's verify-first constraints (Rounds 3 and 6); risks building persistence and sync machinery the verified behavior may prove unnecessary. Rejected.

**Option C: Codex-critical-subset parity**
Invalidation rationale: explicitly rejected by the user in interview Round 2; the bar is full Responses parity.

## Implementation Steps

### Phase A: Research and verification gates (sequential, blocks Phase B decisions)

- **A1. Codex resume probe.** Script a real session: `codex exec -p <profile>` turn 1, restart gateway (`launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy`), `codex resume` turn 2. Record per provider whether resume succeeds against the wiped in-memory `ResponseStore` (src/reverso/protocols/store.py). Output: decision PERSIST or NO-PERSIST in `.omc/research/codex-resume-probe.md` (frontmatter, no dashes).
- **A2. Codex /model support research.** Inspect installed Codex version, docs, and source for any supported mechanism by which custom `model_providers` feed the TUI /model picker (for example a /models endpoint poll). If no native mechanism exists (the likely outcome given the session-verified picker behavior), the decision falls through to SYNC-TOOL explicitly. Output: decision NATIVE, SYNC-TOOL, or DOCUMENT-ONLY in `.omc/research/codex-model-picker.md`.
- **A3. Auggie streaming capability check.** Run `auggie --help` and probe for streamed output modes (for example `--output-format stream-json`). Output: decision STREAM or BUFFER in `.omc/research/auggie-streaming.md`.
- **A4. Responses surface enumeration.** Enumerate the full OpenAI Responses API request/response surface from the official API reference (input variants, instructions, tools incl. web_search/file_search, tool_choice, parallel_tool_calls, reasoning, store, previous_response_id, stream, sampling params, max_output_tokens, truncation, metadata, include, background, text.format). Classify per provider: native, translated, or unsupported. Output: draft matrix in `.omc/research/responses-parity-surface.md` PLUS a machine-readable `.omc/research/responses-parity-surface.json` from which the B1 capability tables are generated (not hand-transcribed); the markdown drafts the final docs page.

### Phase B: Implementation lanes (parallel after Phase A)

- **B1. Feature policy and unsupported_feature machinery (foundation for B4), hybrid gate design.**
  - New module `src/reverso/protocols/feature_policy.py`: `extract_features(request: ResponsesRequest) -> set[str]`, per-provider capability tables GENERATED from `.omc/research/responses-parity-surface.json` (A4), and a typed `UnsupportedFeature(Exception)` carrying `provider` and `feature`.
  - Fast path: enforcement in `src/reverso/protocols/responses_app.py` before adapter dispatch; unsupported feature returns HTTP 400 with body `{"error": {"type": "invalid_request_error", "code": "unsupported_feature", "message": "<provider> does not support <feature>"}}`.
  - Back-stop: adapters MAY raise `UnsupportedFeature` from inside `create_response`/`stream_response` when they hit a table omission; `responses_app` catches it around dispatch and renders the IDENTICAL 400 body via the same shared builder. The capability table is the source of truth; the exception prevents silent semantic drift when the table forgets a feature.
  - Unit tests: one fixture per provider x representative unsupported feature for the fast path (assert 400 shape and that the adapter runner is never invoked) plus a back-stop test (adapter raises `UnsupportedFeature`, same 400 body rendered).
- **B2. Claude incremental streaming with an explicit streaming runner contract.**
  - The existing blocking runner (`_run_claude_cli` using `subprocess.run` behind `asyncio.to_thread` in `src/reverso/protocols/adapters/claude.py`) CANNOT emit incremental deltas; B2 introduces a NEW injectable streaming runner with its own signature, `stream_cli_runner: Callable[[str, str], AsyncIterator[str]]`, default implementation built on `asyncio.create_subprocess_exec` reading stdout line by line (claude CLI stream output flags probed in Phase A). Test doubles are plain async generators.
  - `_stream_response` consumes the iterator and emits one `response.output_text.delta` per yielded chunk instead of a single buffered delta.
  - Fallback conditions, named precisely: (a) stream flag rejected (nonzero exit before the first chunk) or (b) first-chunk parse error -> fall back to the buffered `_run_claude_cli` path for that request. Once any delta has been emitted, NO silent fallback: a mid-stream iterator exception must surface through the existing `responses_app` mid-stream contract (`response.failed` event followed by `[DONE]`), which B2 must preserve unchanged.
  - Tests: fake async-generator runner asserting multiple deltas concatenate to the full text and canonical event order is preserved (parity suite `_collapse_repeated_deltas` already tolerates chunking); a fallback test (runner fails before first chunk -> buffered path serves the request); a mid-stream failure test (exception after first delta -> `response.failed` + `[DONE]`).
- **B3. Conditional store persistence (only if A1 = PERSIST).**
  - Extend `src/reverso/protocols/store.py` with a disk-backed layer keyed per provider/repo under `~/.local/state/reverso/`, 7 day TTL, size cap; in-memory remains the hot path.
  - Concurrency and safety requirements: writes serialized per key (per-key lock alongside the existing store lock), each write lands via temp file + `os.replace` with the temp file created IN THE SAME DIRECTORY as the target (atomic rename requires same filesystem), the state directory is created with mode 0o700, and both the permission bits and the no-token-material invariant are FIXTURE-TESTED assertions, not prose.
  - Tests: TTL expiry, cap eviction, restart survival (tmp_path), atomic-write behavior, directory mode 0o700, no token material on disk.
  - If A1 = NO-PERSIST: document the in-memory boundary in the parity doc instead; no code.
- **B4. Parity translation work per provider (driven by A4 matrix).**
  - copilot (src/reverso/protocols/adapters/copilot.py): native forward; verify pass-through of remaining surface (include, background, metadata, text.format) with fixture tests.
  - deepseek (src/reverso/protocols/adapters/deepseek.py): extend chat translation where mappable (response_format/text.format, parallel_tool_calls, max_output_tokens, sampling params); unsupported set (web_search, image input, background) goes to B1 tables.
  - claude and auggie (CLI spines): translate instructions and multi-item text inputs through build_prompt (src/reverso/protocols/replay.py); declare image/file inputs, native web_search, background as unsupported in B1 tables.
- **B5. Model listing mechanism (decided by A2).**
  - NATIVE: implement whatever Codex supports (likely zero gateway work; config keys only) and document.
  - SYNC-TOOL: new console script `reverso-codex-sync` (entry in pyproject.toml, module `src/reverso/codex_sync.py`) that GETs each provider's live `/v1/models` and idempotently writes per-model profile entries into `~/.codex/config.toml`. Write discipline: UTC-timestamped backup before write with rotation (keep the 5 newest backups, prune older), compose the full new TOML in a temp file in the same directory and `os.replace` it into place (corruption-on-crash impossible), never touch unrelated keys, no secrets.
  - DOCUMENT-ONLY: write usage docs for -m and profile TOMLs.
  - Tests (SYNC-TOOL case): TOML rewrite idempotency, backup creation, unrelated-key preservation, against fixture model payloads.

### Phase C: Done-gate assembly

- **C1. E2E Codex matrix script** `scripts/codex-e2e-matrix.sh`: per provider runs multi-turn memory (Ada test), workspace context (file content reference), usage plausibility (jq on response.completed), resume-after-restart, streaming TTFB measurement with an explicit threshold assertion (initial gate: first delta within 20 seconds for streaming providers, catching buffering regressions automatically; tighten to per-provider thresholds once the matrix establishes baselines), tool-call loop where supported, model selection via the B5 mechanism. Emits a pass/fail table; evidence saved to `.omc/research/codex-e2e-matrix-results.md`.
- **C2. Parity matrix doc** `docs/architecture/codex-responses-parity-matrix.md` (YAML frontmatter, no en/em dashes): final per-provider feature support matrix incl. every unsupported_feature entry and streaming status, sourced from A4 + B-lane outcomes.
- **C3. Full suites green**: `uv run pytest tests/unit -q` and `uv run pytest tests/integration -q`; dash check `rg -nP '[\x{2013}\x{2014}]' docs README.md src tests` clean, plus a unit test that scans `docs/` for em/en dashes so the constraint is enforced by the suite, not only by manual verification.

## Acceptance Criteria

- [ ] A1-A4 research notes exist under `.omc/research/` with explicit decisions recorded.
- [ ] E2E matrix script passes every applicable cell for all four providers; non-applicable cells (documented unsupported) listed explicitly with reasons.
- [ ] Claude streaming emits 2+ deltas for a multi-sentence completion in a real `codex` session (observable) and in unit tests (fake runner).
- [ ] Every unsupported feature returns the structured 400 `unsupported_feature` body and never invokes the provider runner (unit-tested per provider).
- [ ] `codex resume` verified per provider; if persistence was built, restart-survival, TTL, and cap behavior are unit-tested.
- [ ] Model selection works per provider via the A2-decided mechanism; SYNC-TOOL case is idempotent and backup-protected.
- [ ] `docs/architecture/codex-responses-parity-matrix.md` exists, frontmattered, dash-clean, and covers all four providers across the A4 surface.
- [ ] `uv run pytest tests/unit -q` and `uv run pytest tests/integration -q` green; no em/en dashes introduced anywhere.
- [ ] No commits or PRs without explicit user approval; merge only after reviewer-loop APPROVE plus green GH CI plus green local CI.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Claude CLI stream output format changes or is unavailable in installed version | Probe the flag in Phase A; parser is fixture-driven; automatic fallback to buffered single-delta path on parse failure or nonzero exit |
| `codex resume` semantics differ across Codex versions | A1 probe runs against the installed version; decision recorded with version stamp; persistence design (TTL/cap) ready if needed |
| Full Responses surface is large and creeps | A4 matrix bounds scope up front; default classification is unsupported (structured error), so unmapped features are safe, not silent |
| Auggie CLI has no streaming output | Spec explicitly allows documented buffered limitation (Round 4 decision) |
| Config sync tool corrupts ~/.codex/config.toml | Idempotent writes, pre-write backup, unrelated-key preservation, fixture-tested rewrites |
| Disk-persisted store leaks conversation content | Store under user-only `~/.local/state/reverso/` permissions, no token material ever stored, TTL purges aged data |
| Feature gate false positives block working requests | Capability tables are per provider and fixture-tested against the already-verified codex exec flows before merge |

## Verification Steps

1. `uv sync --extra dev && uv run pytest tests/unit -q && uv run pytest tests/integration -q`
2. `rg -nP '[\x{2013}\x{2014}]' docs README.md src tests` returns nothing
3. `launchctl kickstart -k gui/$(id -u)/com.user.reverso-proxy` then poll until `/claude/v1/models` responds
4. `bash scripts/codex-e2e-matrix.sh` and review the emitted pass/fail table
5. Manual spot check: `codex exec -p <profile>` per provider for one multi-turn exchange with streaming visible
6. Review `docs/architecture/codex-responses-parity-matrix.md` against `.omc/research/responses-parity-surface.md`

## ADR

- Decision: Option A, research-gated phased lanes. Phase A research gates (Codex resume probe, /model picker behavior, auggie streaming feasibility, Responses surface enumeration) decide what Phase B builds; Phase B implements the hybrid feature gate, claude incremental streaming, conditional persistence, per-provider parity translation, and the model sync mechanism; Phase C assembles the composed done gate (E2E matrix, parity doc, suites green).
- Drivers: (1) Codex-side behavior (resume semantics, /model sourcing) is unverified and changes WHAT must be built; (2) full Responses parity across four heterogeneous adapters needs a single enforcement point to avoid capability drift; (3) streaming must be incremental where feasible without breaking the existing mid-stream failure contract.
- Alternatives considered: Option B (build all lanes speculatively in parallel, no research gates) rejected because A1/A3 outcomes can invalidate B3/B2 work; Option C (fork or patch Codex for /model and resume) invalidated by the user (no Codex fork, ladder ends at documentation).
- Why chosen: research gates eliminate speculative work; the hybrid gate (generated capability tables fast path plus typed UnsupportedFeature back-stop) prevents drift between declared and actual adapter capabilities; the injectable stream_cli_runner seam adds streaming without modifying the frozen ProviderAdapter Protocol.
- Consequences: Phase A serializes ahead of Phase B; capability tables become a generated artifact sourced from responses-parity-surface.json; the claude adapter gains a second runner seam (buffered and streaming); B3 may introduce new on-disk state (0o700, atomic writes, TTL 7 days, per provider/repo) only if A1 returns PERSIST.
- Follow-ups: tighten C1 TTFB from the initial 20 second gate to per-provider thresholds once baselines exist; revisit auggie incremental streaming if the CLI gains a streaming output mode; backup rotation policy for reverso-codex-sync (keep 5 newest) reviewed after first real use; PR flow per the merge protocol (reviewer loop APPROVE, GH CI green, local CI green).

## Changelog

- Iteration 2 (Architect REVISE feedback applied):
  1. B2 rewritten with an explicit streaming runner contract (`stream_cli_runner` async iterator via `asyncio.create_subprocess_exec`), precisely named fallback conditions, and preservation of the `response.failed` + `[DONE]` mid-stream contract.
  2. B1 upgraded to a hybrid gate: central capability tables (fast path) plus a typed `UnsupportedFeature` exception adapters may raise (back-stop), both rendered through one shared 400 builder.
  3. B3 hardened: per-key write serialization, temp file + `os.replace` atomic writes, 0o700 state directory, fixture-tested permission and no-token assertions.
  4. Optional improvements adopted: A4 emits machine-readable JSON for generated capability tables; C1 asserts a TTFB threshold; C3 adds a suite-enforced dash check; B5 sync tool uses atomic replace with UTC-timestamped backups.
- Iteration 2 final (Critic APPROVED, optionals applied): frontmatter marked pending-approval with consensus architect-approved-critic-approved; A2 documents the explicit SYNC-TOOL fallthrough; B3 and B5 write temp files in the same directory as the target before os.replace; B5 backup rotation keeps the 5 newest; C1 TTFB starts at a 20 second initial gate and tightens to per-provider thresholds once baselines exist; ADR section finalized.
