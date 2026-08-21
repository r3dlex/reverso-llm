---
type: product-spec
project: reverso
title: Reverso-routed Ollama provider for Codex and Claude Code
status: active
slug: ollama-reverso-provider
mode: deliberate
date: 2026-08-20
source_handoff: .ai/handoff/Archive/northstar-ollama-claude-code-codex-provider.md
source_context: .omx/context/ollama-claude-code-codex-provider-20260820T161633Z.md
consensus_prd: .omx/plans/prd-ollama-claude-code-codex-provider.md
consensus_test_spec: .omx/plans/test-spec-ollama-claude-code-codex-provider.md
---

# Reverso-routed Ollama provider for Codex and Claude Code

## Outcome

Add Ollama as a first-party Reverso-routed provider on the existing loopback gateway. Codex calls `http://127.0.0.1:64946/ollama/v1` through a dedicated Ollama profile and the OpenAI Responses contract. Claude Code calls the same Reverso listener through the Anthropic Messages surface and a managed Ollama launcher/catalog. Reverso dispatches both surfaces to the user-owned local Ollama service, which serves installed local models and authorized Ollama Cloud models.

Embedded Headroom compression remains automatic and provider-agnostic before Ollama dispatch. It must preserve structured content, function tools, tool results, images, model identity, and streaming semantics on both inbound surfaces.

## Scope and success

- Type: routed provider feature with authentication, protocol-fidelity, and convergence risk.
- Delivery: four thin one-PR vertical goals.
- No new gateway port or long-lived Reverso service.
- No change to the frozen `ProviderAdapter` signature.
- No Ollama installation, daemon management, implicit model pull, credential store, or shell-profile sourcing.

Success means:

1. A dedicated Codex Ollama profile exposes only raw Ollama model ids and completes Responses requests through Reverso.
2. A managed Claude Code Ollama launcher/catalog completes Messages requests through Reverso.
3. Both paths use embedded Headroom before dispatch without corrupting text, tools, tool results, or images.
4. Inventory is the deterministic union of installed local models and current Cloud eligibility from supported Ollama authority.
5. Ollama device sign-in remains Ollama-owned and can start only in an attended foreground recovery flow.
6. Cloud opt-out is absolute and local models remain usable.
7. Existing providers, user-owned files, port topology, and frozen interfaces remain unchanged.

## Evidence-grounded current seams

- `src/reverso/protocols/adapter.py` freezes the Responses-shaped `ProviderAdapter` methods.
- `src/reverso/protocols/responses_app.py` normalizes, feature-gates, compresses, then calls `ProviderAdapter`.
- `src/reverso/protocols/anthropic_app.py` resolves a backend, prepares an Anthropic request, translates it to `ResponsesRequest`, compresses, dispatches through `ProviderAdapter`, then maps the result back.
- `src/reverso/protocols/headroom_compression.py` is already provider-agnostic and executes before adapter dispatch on both surfaces.
- `src/reverso/proxy/compose.py` constructs adapters and owns the single loopback listener.
- `src/reverso/protocols/surface_registry.py` owns Anthropic backend resolution, discovery aliases, and canonical model ids.
- `src/reverso/protocols/model_exposure.py`, `codex_sync.py`, `claude_code_sync.py`, `client_sync.py`, and `config/supported-client-surfaces.json` own client presentation and convergence.
- Official Ollama 0.32.14 supports OpenAI-compatible `/v1/responses`, Anthropic-compatible `/v1/messages`, and installed-model discovery through `/api/tags`.
- External research recommends two outbound protocol clients behind shared Ollama connection/auth/catalog infrastructure so each upstream protocol retains fidelity.

## Canonical decisions

1. Ollama is a Reverso-routed provider named `ollama`.
2. Responses requests use a frozen-contract `OllamaAdapter` and an outbound Ollama Responses client.
3. Anthropic Messages requests use an optional internal native-Messages facet on the same provider object and an outbound Ollama Messages client.
4. One composition-owned `OllamaRuntime` is the sole lifecycle authority. It contains the shared AsyncClient, endpoint, catalog, auth state, protocol clients, and the dual-facet Ollama adapter injected into both app registries.
5. The dedicated Codex profile contains only raw Ollama ids. It never contains `ollama/<id>` selectors.
6. Claude Code uses a header-bound isolated Ollama catalog. Anthropic accepts no bare raw Ollama ids, even when currently unique. Only a full exact catalog alias bound to `(backend="ollama", exact_raw_id)` may route Ollama. The global normalized bare-id index is never extended or overwritten by Ollama inventory.
7. Headroom compression precedes both outbound clients. Native Messages projection changes only compressible text leaves and preserves all structured blocks.
8. `ollama signin` is foreground-attended only. Background refresh, sync, verify, gateway requests, and scheduled work return `auth_required` and never open a browser.
9. `OLLAMA_NO_CLOUD=1` and supported Ollama server Cloud disablement are absolute opt-outs.
10. Reverso never sources `.zsh_exports`, reads or persists device keys, or persists API keys. The initial inherited `OLLAMA_API_KEY` operation allowlist is empty, so spawned operations scrub it.
11. Reverso never installs, starts, stops, or restarts Ollama and never implicitly pulls a model.
12. The source handoff is superseded wherever it says direct provider, no Reverso adapter, automatic daemon/LaunchAgent, automatic serve, or `ollama/` and `ollama-` provider-prefixed selectors.

## Minimal architecture

### Composition-owned Ollama runtime

Add a small internal Ollama package under `src/reverso/protocols/adapters/ollama/` and expose one lifecycle aggregate:

```python
@dataclass
class OllamaRuntime:
    client: httpx.AsyncClient
    catalog: OllamaCatalog
    auth: OllamaAuthState
    responses_client: OllamaResponsesClient
    messages_client: OllamaMessagesClient
    adapter: OllamaAdapter

    async def close(self) -> None: ...
```

- `CompositionRoot` constructs exactly one `OllamaRuntime` for the real application.
- The runtime owns one validated endpoint, one shared `httpx.AsyncClient`, one catalog/auth state, both protocol clients, and one dual-facet adapter.
- In G1, only the Responses registry receives `runtime.adapter`; the Anthropic registry has no Ollama entry. Starting in G2, both registries receive the same `runtime.adapter` object by identity. Builders may accept injected registries for tests, but they never construct or close a runtime locally.
- `CompositionRoot` is the only owner allowed to call `runtime.close()`.
- Shutdown order is: stop accepting new HTTP work, await existing tracked requests/streams to quiesce within the existing bounded lifespan policy, close the runtime exactly once, then finish remaining root-owned cleanup.
- `OllamaRuntime.close()` is idempotent for defensive safety, but normal `CompositionRoot` shutdown invokes it exactly once.
- If construction fails after the AsyncClient or another owned component exists, the factory closes every already-created component before re-raising. No partially constructed runtime is injected.
- App/registry objects have no `close()` path for Ollama and hold only non-owning references.
- `connection.py`, `catalog.py`, `auth.py`, `responses.py`, `messages.py`, and `adapter.py` remain narrow implementation modules inside this runtime boundary.

Do not introduce a generic provider framework. The runtime exists because two concrete protocol clients share connection, catalog, auth, and lifecycle.

### Frozen Responses contract

`OllamaAdapter` implements all existing `ProviderAdapter` methods without changing their signatures:

- `create_response` forwards a compressed `ResponsesRequest` to Ollama `/v1/responses`, validates the response, and returns `ResponseEnvelope`.
- `stream_response` parses Ollama Responses SSE into existing `SSEEvent` objects with pre-stream and mid-stream failure semantics preserved.
- `list_models` returns the shared raw-id catalog in the existing `ModelList` shape.
- `get_response` and `list_input_items` use the existing Reverso `ResponseStore` seam because Ollama need not provide retrieval endpoints.
- Provider model ids arrive raw. The adapter never strips an Ollama prefix because isolated client catalogs add none.

### Exact internal Anthropic seam

Add a non-public, non-frozen runtime-checkable protocol in `src/reverso/protocols/anthropic_native.py`:

```python
class AnthropicNativeAdapter(Protocol):
    async def create_anthropic_message(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def stream_anthropic_message(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]: ...
```

`OllamaAdapter` implements this facet in addition to `ProviderAdapter`. No existing adapter must implement it.

#### Translation-produced source-address map

Add an internal prepared result used by Anthropic dispatch while preserving any existing public helper compatibility:

```python
@dataclass(frozen=True)
class AnthropicProjectionSource:
    response_address: tuple[int | str, ...]
    native_json_pointer: tuple[int | str, ...]
    native_block_kind: str
    structural_fingerprint: str

@dataclass(frozen=True)
class PreparedAnthropicDispatch:
    request: ResponsesRequest
    payload: dict[str, Any]
    projection_sources: tuple[AnthropicProjectionSource, ...]
```

The Anthropic-to-Responses translator emits a source record at the moment each reversible native text leaf becomes a Responses text leaf. It does not infer addresses later from list position. The structural fingerprint covers the containing role, native block type, stable tool/block id where present, and sibling block-kind sequence, but never content bytes.

Dispatch order:

1. Keep existing backend resolution, catalog-bound exact alias resolution, canonical raw model id, degradable-feature stripping, capability gating, and preparation order.
2. Prepare `ResponsesRequest`, prepared native payload, and source-address map together.
3. Compress through `compress_responses_request(..., surface="anthropic_messages")` exactly once.
4. Call `project_compressed_request_to_anthropic_payload(prepared, compressed_request)`.
5. Project only changed text leaves with a one-to-one source record whose Responses address still exists, native pointer still names a text leaf, and structural fingerprint still matches.
6. Preserve system/message ordering, roles, tools, `tool_choice`, `tool_use`, `tool_result`, images, cache-control fields, model id, max tokens, stop sequences, metadata, and stream flag.
7. Treat merged/split text items, flattened unsupported nesting, duplicate addresses, missing addresses, changed cardinality, reordered blocks, altered block kinds, fingerprint mismatch, or any source record without an exact reverse target as lossy. Fail open to the complete prepared native payload. Never align by coincidental position and never partially project.
8. If the adapter implements `AnthropicNativeAdapter`, dispatch the projected native payload and return or stream validated native results directly.
9. Otherwise retain the current Responses adapter and Responses-to-Anthropic mapping path unchanged.

This seam preserves Ollama Messages fidelity without weakening the frozen adapter contract.

### Routing, exact alias authority, and composition

#### Runtime injection

- Add `ollama` to `APP_PROVIDER_PREFIXES` and `SURFACE_BACKENDS["anthropic"]` as a backend name only.
- `CompositionRoot` creates one `OllamaRuntime`, then injects the identical `runtime.adapter` into both the Responses and Anthropic registries.
- Add `/ollama/v1/responses`, `/ollama/v1/models`, and existing response retrieval routes through the Responses app.
- Keep `127.0.0.1:64946` as the only Reverso listener.

#### Catalog-scoped exact Anthropic alias authority

Anthropic Ollama routing is header-bound and exact:

- The managed launcher sends `x-reverso-model-catalog: ollama`.
- Only that exact catalog value activates Ollama alias lookup. Missing, malformed, multiple, or another catalog value cannot route Ollama.
- For each eligible raw id, the scoped model listing creates one full presented alias and stores an authority entry keyed by the complete alias bytes: `alias -> (backend="ollama", exact_raw_id)`.
- The authority is request-context/catalog-bound. It is passed explicitly to backend resolution for the scoped request and is not written into `_MODEL_INDEX`, `_BACKENDS_WITH_ROWS`, or any other global normalized bare-id authority.
- Bare raw Ollama ids and generic provider-qualified `ollama/<raw-id>` forms are disallowed entirely on Anthropic, even if a raw id appears unique today. This prevents Ollama from using rowless-backend fallback or claiming an existing backend's normalized taxonomy.
- Resolution checks the full presented alias exactly before any generic normalized alias handling. It performs no lowercase lookup, prefix-tail parsing, or reconstruction of raw ids.
- Catalog build computes both exact alias keys and Unicode casefold keys. Any duplicate exact alias, two aliases that casefold equal, or alias that casefold-collides with an existing backend's presented id invalidates the conflicting Ollama entries and blocks publication of that scoped catalog generation.
- A successful exact lookup returns both backend and exact raw id. Canonicalization receives that tuple and replaces the model with `exact_raw_id` byte-for-byte before preparation, Headroom, or adapter dispatch.
- The alias never reaches either Ollama protocol client and never affects unscoped Anthropic requests.

### Raw model identity and client presentation

#### Codex

- Generate `~/.codex/reverso-ollama.config.toml` under existing marker ownership.
- Provider slug is `reverso_ollama`; base URL is `http://127.0.0.1:64946/ollama/v1`; wire API is Responses.
- Generate `~/.codex/reverso/ollama.json` containing raw Ollama ids as both selector slug and upstream model id.
- The profile is isolated, so no `ollama/` qualifier is needed or allowed.
- Do not modify the built-in OpenAI default, top-level model, or unrelated catalogs.

#### Claude Code

- Generate marker-owned `~/.local/bin/claude-ollama` using the existing pinned-Claude, scrub, atomic-write, lock, and mode rules.
- The launcher targets Reverso `ANTHROPIC_BASE_URL=http://127.0.0.1:64946`, uses the non-secret loopback placeholder token, enables gateway model discovery, and sets `x-reverso-model-catalog: ollama` plus workspace.
- The scoped `/v1/models` result is derived only from the live or explicitly retained Ollama raw-id catalog.
- Because current Claude Code gateway discovery accepts only `claude*` or `anthropic*` ids, present `anthropic-ollama-<raw-id>` only as an opaque full alias key.
- The catalog-scoped authority binds that exact full alias to `(backend="ollama", exact_raw_id)`. Bare raw ids are never accepted on Anthropic.
- The compatibility alias is never stored in Codex, never enters the global normalized index, and never reaches Ollama. Exact raw id case and punctuation are restored from the authority value, never parsed from the alias tail.

### Inventory and Cloud eligibility

- Installed local ids come from validated `/api/tags` results.
- Cloud ids require a supported machine-readable Ollama Cloud authority established for the pinned minimum version. None is established in G1, so G1 blocks Cloud publication. No suffix inference, HTML scraping, or static shipped list is permitted.
- Catalog entries retain raw ids, source eligibility (`local`, `cloud`, or both), observation time, and freshness internally. Client catalogs omit secrets.
- Exact duplicate raw ids collapse deterministically while retaining both eligibility flags.
- Cloud disabled produces a current local-only catalog and performs no Cloud probe or sign-in. Cloud requested in G1 reports `unavailable` while preserving every validated `/api/tags` row as local inventory.
- Background `auth_required`, timeout, or malformed Cloud discovery retains prior marker-owned Cloud entries as stale, updates valid local entries, reports partial freshness, and never claims stale entries are currently eligible.
- At request time, a stale Cloud-only id must be revalidated. If it cannot be confirmed, return `auth_required` or `model_not_current`; never route elsewhere.
- If no compatible local model is installed, live proof reports `local_model_required` with an explicit user-run pull command but does not execute it.

### Authentication boundary

- Gateway requests never launch `ollama signin` because they are noninteractive.
- `reverso-client-sync` dry-run/apply/refresh/verify and scheduled refresh never sign in.
- Add or extend one explicit foreground operator command in the Ollama provider group for attended Cloud recovery. It may run `ollama signin`, wait with a bounded timeout, then retry Cloud eligibility once.
- Device identity remains under `~/.ollama/id_ed25519`; Reverso may observe only bounded auth result from supported Ollama behavior. It never reads, copies, hashes, backs up, logs, or persists the key.
- The initial per-operation inherited API-key allowlist is empty. Every spawned Ollama command scrubs `OLLAMA_API_KEY`. A future allowlist change requires official necessity evidence, active-spec and ADR updates, named operation scope, environment-only inheritance, and canary tests.

## Governance and authoritative artifacts

The canonical dedicated feature specification is:

- `docs/specifications/ACTIVE/ollama-reverso-provider.md`

G1 creates that specification and also amends:

- `docs/specifications/ACTIVE/reverso-install-profile-catalog-headroom-convergence.md`

The convergence amendment must update its exact tables and contracts for:

- manifest provider/group/surface rows and dependency ownership;
- raw Ollama selector policy for the isolated Codex profile;
- Headroom `ollama` provider and inbound/outbound surface dimensions;
- local/Cloud freshness, partial freshness, auth-required, Cloud-disabled, and stale-retention status;
- atomic Ollama group paths, marker ownership, rollback, restore, and uninstall;
- Responses routing authority in G1 and catalog-scoped Anthropic exact-alias authority introduced only by G2.

If repository validation requires regenerated context after specification edits, the same PR runs `python3 scripts/export-ai-context.py` and commits the updated `.ai/execution/context/default.json`. A drifted generated context blocks the goal.

After Ralplan consensus, Northstar writes a replacement handoff at:

- `.ai/handoff/northstar-ollama-reverso-provider.md`

The replacement handoff names this PRD and companion test specification as authoritative and marks `.ai/handoff/Archive/northstar-ollama-claude-code-codex-provider.md` superseded. It explicitly supersedes these former decisions:

1. Ollama is a direct client provider rather than Reverso-routed.
2. No Reverso Ollama adapter is added.
3. Reverso may manage an Ollama daemon, LaunchAgent, automatic serve, start, stop, install, or restart path.
4. Codex uses `ollama/<id>` selectors.
5. Claude uses `ollama-<id>` selectors.
6. Clients may bypass Reverso and therefore bypass embedded Headroom.
7. Separate client-owned Ollama authentication or credential persistence is permitted.
8. Automatic sign-in may occur in background refresh or gateway requests.

Until that replacement handoff exists, the two Ralplan artifacts are the canonical planning authority for review, but not implementation authorization before the Architect then Critic consensus record is complete. The replacement handoff and four work-intake records must exist before G1 execution begins.

## RALPLAN-DR

### Principles

1. Preserve protocol fidelity at the closest supported boundary.
2. Keep frozen public contracts frozen; add a narrow optional internal facet rather than widening every adapter.
3. Compress once before dispatch and preserve all non-text structure.
4. Keep model and credential authority with Ollama while Reverso owns routing and client convergence.
5. Fail closed on auth, inventory, ownership, and projection ambiguity without affecting unrelated providers.

### Decision drivers

1. Correct Responses and Messages streaming/tool/image behavior.
2. Minimal change to existing gateway and frozen adapter architecture.
3. Safe, idempotent client convergence and Ollama-owned authentication.

### Options

#### A. One Responses-only adapter for both inbound surfaces

Pros: no new internal protocol; maximum reuse of current Anthropic translation.

Cons: translates Messages to Responses and back even though Ollama supports native Messages; increases fidelity risk for tools, images, content blocks, stop reasons, and SSE.

Verdict: rejected for Ollama Anthropic dispatch, retained as fallback behavior for existing adapters.

#### B. Two unrelated adapter objects and separate catalogs

Pros: protocol-specific implementations are simple in isolation.

Cons: duplicates auth, endpoint, inventory, lifecycle, and freshness state; risks catalog disagreement.

Verdict: rejected.

#### C. One dual-facet Ollama adapter with shared core

Pros: satisfies frozen Responses contract, preserves native Messages fidelity, shares authoritative connection/auth/catalog, and changes only Anthropic dispatch interior.

Cons: introduces one provider-specific optional branch in Anthropic dispatch and a safe compression projection seam.

Verdict: selected.

#### D. Change `ProviderAdapter` to include Messages methods

Pros: one nominal interface.

Cons: violates the frozen contract and forces unrelated providers to change.

Verdict: invalidated.

#### E. Direct client-to-Ollama configuration

Pros: fewer gateway components.

Cons: contradicts canonical routing, bypasses embedded Headroom and Reverso observability, and fragments convergence.

Verdict: superseded and rejected.

## Architecture decision record

### Decision

Introduce `OllamaAdapter` as a dual-facet object. Its Responses facet implements the unchanged `ProviderAdapter`. Its optional native Messages facet implements a new internal `AnthropicNativeAdapter`. Both use one composition-owned `OllamaRuntime` containing the shared client, connection/auth/catalog state, protocol clients, and dual-facet adapter. Anthropic dispatch resolves only a header-bound exact alias authority, then gates, translates with an explicit source-address map, compresses, and safely projects mapped text back into the prepared native payload before native Ollama Messages dispatch. Other providers keep the existing path.

### Drivers

- Ollama 0.32.14 supports both outbound protocols.
- `ProviderAdapter` is frozen.
- Headroom already runs before adapter dispatch on both inbound surfaces.
- Tool, image, and SSE fidelity is more reliable with native protocol clients.
- Reverso must remain single-port and loopback-only.

### Alternatives considered

Responses-only translation, separate unrelated clients, frozen-interface expansion, and direct client routing were considered and rejected as described in RALPLAN-DR.

### Consequences

- One optional runtime protocol, one translation-produced source-address map, and one pure projection function are added.
- `CompositionRoot` is the sole Ollama lifecycle authority and injects one runtime into both registries.
- Ollama owns two wire clients but one connection/auth/catalog authority.
- Headroom projection must be exhaustively tested for structured content and fail-open behavior.
- Claude compatibility aliases remain header-bound exact presentation only; bare raw Ollama ids are disallowed on Anthropic and raw ids are recovered byte-for-byte from the scoped authority.
- Repository ADR and active convergence specification updates are required in G1.

### Follow-ups

- Reassess the native facet only if the gateway later adopts a stable provider-neutral multi-protocol adapter contract through a separate ADR.
- Any Ollama daemon management, API-key persistence, or extra listener requires a new plan and ADR.

## Issue-ready delivery DAG

```text
G1 OLLAMA-RP-G1 Codex Responses vertical
  |
  +--> G2 OLLAMA-RP-G2 Claude Messages vertical
        |
        +--> G3 OLLAMA-RP-G3 convergence, refresh, and docs
              |
              +--> G4 OLLAMA-RP-G4 attended proof and hardening
```

Each goal is one PR, one work-intake record, and one red-green evidence record. Dependencies are strict and sequential.

### G1: OLLAMA-RP-G1 Codex Responses vertical

Issue path:

- `.ai/work-intake/ollama-reverso-provider-g1-codex-responses.md`

Dependencies:

- replacement Northstar handoff and all four work-intake records exist;
- official Ollama 0.32.14 Responses and `/api/tags` evidence available;
- no circuit-breaker condition.

Prerequisite state:

- Ollama is absent from all Reverso runtime and client registries;
- no supported Codex Ollama profile exists;
- existing providers and convergence are green.

Scope:

- Create only the composition-owned runtime pieces required by Responses: AsyncClient, endpoint validation, local/Cloud catalog/auth state, Responses client, frozen-contract adapter facet, response store integration, and lifecycle ownership.
- Register `/ollama/v1/models` and `/ollama/v1/responses` in the Responses registry only.
- Deliver `~/.codex/reverso-ollama.config.toml` and `~/.codex/reverso/ollama.json` with raw ids only.
- Register the Codex Ollama provider group/surfaces in `config/supported-client-surfaces.json` and client convergence.
- Create the dedicated active spec, repository ADR, and exact convergence-spec amendments listed above.
- Do not add `ollama` to `SURFACE_BACKENDS["anthropic"]`, do not inject Ollama into the Anthropic registry, do not add native Messages types, and do not make any Ollama alias reachable on `/v1/messages`.

The G1 Codex catalog is intentionally conservative because `/api/tags` does
not expose model-specific modalities, parallel-tool capability, or a context
window and G1 performs no additional network discovery. Every generated Ollama
row advertises text-only input, no parallel tool calls, and a 2048-token
context bound. This catalog presentation does not narrow the gateway contract:
explicit image and structured function-tool Responses payloads remain
pass-through and are governed by upstream model support.

Exit state:

- A user can run the generated Codex Ollama profile and complete text, structured tool, and supported image Responses requests through Reverso with embedded Headroom.
- Current local raw ids appear in the isolated Codex catalog. Cloud publication remains blocked in G1 because no supported machine-readable authority is established.
- G1 has no reachable Ollama Anthropic route. Scoped header, bare raw id, `ollama/<id>`, and forged discovery alias all fail before Ollama dispatch on `/v1/messages`.
- Runtime is composition-owned, created once, closed once after quiescence, and contains no unused Messages client/facet.
- Apply twice is no-op; restore and ownership conflict behavior are proven.

Targeted TDD:

- Red command: `uv run pytest tests/unit/test_ollama_responses.py tests/integration/test_ollama_codex_profile.py -q`
- Required red: missing routed Responses provider/profile assertion, not an unrelated failure.
- Green command: `uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/unit/test_codex_sync.py tests/unit/test_client_convergence_contract.py tests/integration/test_ollama_codex_profile.py -q`
- Evidence: `.ai/evidence/ollama-reverso-provider-g1-tdd.md`

`verification[]`:

```text
uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/unit/test_codex_sync.py tests/unit/test_client_convergence_contract.py -q
uv run pytest tests/integration/test_ollama_codex_profile.py -q
uv run pytest tests/unit/test_anthropic_provider_qualified.py tests/integration/test_anthropic_messages_parity.py -q
uv run ruff check .
uv run ruff format --check .
uvx prek run --all-files
uv run pytest tests/ -v --ignore=tests/integration --tb=short
```

Autobahn invokes this unchanged seven-command sequence through the single
allowlisted entrypoint `bash tests/verify_ollama_g1.sh`. The wrapper runs the
commands above in the same order under strict Bash and does not omit, bypass,
replace, or relax any underlying verification command.

The `dev` optional dependency pins Ruff 0.6.0, matching the version already
pinned by `prek.toml`. Consequently both `uv run ruff` commands and the
`uvx prek` Ruff hooks enforce one declared tool version rather than divergent
formatter behavior.

The executor uses the repository-current root validation path if the child repo exposes a different local CI command. Missing declared gates block completion.

### G2: OLLAMA-RP-G2 Claude Code native Messages vertical

Implementation status: delivered on the G2 branch with the composition-owned
dual-protocol runtime, exact header-bound alias authority, translation-emitted
projection sources, native Messages dispatch, and marker-owned launcher.

Issue path:

- `.ai/work-intake/ollama-reverso-provider-g2-claude-messages.md`

Dependencies:

- `OLLAMA-RP-G1` merged and exact-head green.

Prerequisite state:

- Codex/Responses Ollama is usable;
- composition owns one runtime and adapter;
- Ollama remains unreachable on Anthropic.

Scope:

- Extend the existing `OllamaRuntime` with the Messages client and optional native facet. Do not create a second runtime or client.
- Add `AnthropicNativeAdapter`, translation-produced source maps, safe projection, native Messages nonstreaming/streaming, and catalog-scoped exact alias authority.
- Only now add `ollama` to the Anthropic backend registry and inject the identical runtime adapter.
- Deliver marker-owned `claude-ollama`, scoped Ollama catalog presentation, and exact alias canonicalization.
- Register Claude Ollama surfaces and dependencies in the manifest in this PR.
- Amend the dedicated active spec, ADR, and convergence routing table for the newly reachable Anthropic authority.

Exit state:

- A user can run `claude-ollama` and complete text, tool, tool-result, image, and streaming Messages requests through Reverso to Ollama.
- Header-bound exact aliases resolve byte-for-byte to raw ids; bare and generic qualified ids remain rejected.
- Headroom source-map projection is safe and lossy shapes fail open atomically.
- Existing non-native adapters retain prior behavior.
- Both registries share the same runtime/adapter identity and root remains sole close authority.

Targeted TDD:

- Red command: `uv run pytest tests/unit/test_ollama_messages.py tests/integration/test_ollama_claude_launcher.py -q`
- Required red: absent native Messages route/launcher assertion.
- Green command: `uv run pytest tests/unit/test_ollama_messages.py tests/unit/test_anthropic_translate.py tests/unit/test_headroom_compression.py tests/unit/test_claude_code_sync.py tests/integration/test_ollama_claude_launcher.py -q`
- Evidence: `.ai/evidence/ollama-reverso-provider-g2-tdd.md`

`verification[]`:

```text
uv run pytest tests/unit/test_ollama_messages.py tests/unit/test_anthropic_translate.py tests/unit/test_anthropic_stream.py tests/unit/test_headroom_compression.py -q
uv run pytest tests/unit/test_claude_code_sync.py tests/unit/test_client_convergence_contract.py -q
uv run pytest tests/integration/test_ollama_claude_launcher.py tests/integration/test_anthropic_messages_parity.py tests/integration/test_anthropic_messages_streaming.py -q
uv run pytest tests/unit/test_ollama_adapter.py tests/unit/test_ollama_responses.py tests/integration/test_ollama_codex_profile.py -q
uv run ruff check .
uv run ruff format --check .
uvx prek run --all-files
uv run pytest tests/ -v --ignore=tests/integration --tb=short
```

### G3: OLLAMA-RP-G3 convergence, background refresh, and docs

Issue path:

- `.ai/work-intake/ollama-reverso-provider-g3-convergence-refresh-docs.md`

Dependencies:

- `OLLAMA-RP-G2` merged and exact-head green.

Prerequisite state:

- Both client surfaces are independently usable;
- manifest contains both Ollama client surfaces;
- attended proof has not yet been claimed.

Scope:

- Complete cross-client atomic group planning, shared inventory snapshot, background refresh, stale Cloud retention, opt-out, restore/uninstall, drift verification, and status/observability contracts.
- Complete operator documentation and both active specifications.
- Generate required context artifacts and update frozen convergence fixtures.

Exit state:

- Dry-run, apply, second apply, refresh, verify, restore, and uninstall are deterministic and prompt-free.
- Background auth-required/timeout/invalid Cloud states retain only prior marker-owned Cloud entries and report partial freshness.
- Cloud disabled yields local-only current state with zero sign-in attempt.
- Ollama failure cannot change unrelated provider hashes.
- Operator docs agree with the already authoritative replacement handoff and routed architecture.

Targeted TDD:

- Red command: `uv run pytest tests/unit/test_ollama_convergence.py tests/integration/test_ollama_convergence_runbook.py -q`
- Required red: missing refresh/freshness/atomicity contract assertion.
- Green command: `uv run pytest tests/unit/test_ollama_convergence.py tests/unit/test_catalog_refresh.py tests/unit/test_client_convergence_contract.py tests/integration/test_ollama_convergence_runbook.py -q`
- Evidence: `.ai/evidence/ollama-reverso-provider-g3-tdd.md`

`verification[]`:

```text
uv run pytest tests/unit/test_ollama_convergence.py tests/unit/test_catalog_refresh.py tests/unit/test_client_convergence_contract.py -q
uv run pytest tests/integration/test_ollama_convergence_runbook.py tests/integration/test_client_convergence_runbook.py -q
uv run reverso-client-sync dry-run --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync apply --json
uv run reverso-client-sync refresh --json
uv run reverso-client-sync verify --json
./scripts/convergence-acceptance.sh
uv run python tests/helpers/verify_isolated_convergence.py --home "${VERIFY_HOME}" --rtk-bin "${FAKE_BIN}/rtk"
uv run pytest tests/ -v --ignore=tests/integration --tb=short
```

Use isolated temporary homes for write-capable verification until the attended G4 target proof.

#### G3 implementation record

G3 adds the marker-owned shared inventory snapshot and includes it with the
Codex profile, Codex catalog, and Claude launcher in one `provider-ollama`
rollback group. Background refresh is prompt-free. Cloud opt-out is local-only,
and failed Cloud discovery retains only prior marker-owned Cloud rows as stale.
Scoped Claude publication reads the snapshot while Responses model listing
remains live, so refresh never rereads its output. Total discovery failure
preserves the full group. Marker-safe `uninstall-ollama` and idempotent
`restore` provide deterministic removal and repair. The isolated verification
wrapper supplies temporary homes and fake executable and gateway seams through
an explicit test-only verifier, so it never uses operator credentials or
client state and cannot bypass production deployment drift. G4 target proof
still runs the production deployment-drift command.

### G4: OLLAMA-RP-G4 attended proof and hardening

Issue path:

- `.ai/work-intake/ollama-reverso-provider-g4-attended-proof-hardening.md`

Dependencies:

- `OLLAMA-RP-G3` merged and exact-head green;
- explicit authorization for attended sign-in and target-machine proof;
- user-owned Ollama already installed and running.

Prerequisite state:

- deterministic CI and convergence gates pass;
- docs and replacement handoff are current;
- no live proof has pulled a model or changed daemon state.

Scope:

- Run bounded attended auth recovery when required.
- Prove one already installed compatible local model and one eligible Cloud model through Codex/Responses and Claude/Messages when prerequisites exist.
- Run full regression, security/redaction, streaming, lifecycle, restore, and exact-head hosted checks.
- Harden only defects found by proof; no architecture expansion.

Exit state:

- Both clients succeed for local and Cloud paths, or an exact external prerequisite is recorded without weakening deterministic acceptance.
- No pull, daemon/start/stop/install action, key read, secret persistence, direct client route, extra port, or prompt/response capture occurs.
- Restore verifies unrelated-provider hashes.
- Hosted CI is green for exact head and all review threads are resolved.

Targeted TDD:

- Red command: `uv run pytest tests/integration/test_ollama_live_contract.py -q`
- Required red: deterministic live-contract harness gap or a reproduced proof defect; an unavailable external credential is not valid red evidence.
- Green command: `uv run pytest tests/integration/test_ollama_live_contract.py -q`
- Evidence: `.ai/evidence/ollama-reverso-provider-g4-tdd.md`

`verification[]`:

```text
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run ruff check .
uv run ruff format --check .
uvx prek run --all-files
uv run python -m compileall -q src tests
bash scripts/validate-rules.sh
bash scripts/archgate.sh structural .rules.ts
./scripts/convergence-acceptance.sh
uv run python scripts/check-deployment-drift.py --phase acceptance
uv run pytest tests/ -v --ignore=tests/integration --tb=short
```

Attended live commands and evidence fields follow the companion test specification and are not run in unattended CI.

## Pre-mortem

### 1. Native Messages path bypasses Headroom or feature gating

Failure: an Ollama shortcut dispatches the raw payload before existing preparation.

Prevention: native dispatch selection occurs only after prepare and compression; order tests use spies; no native method is accessible from routing code before that point.

Detection: per-surface metrics show prepare, compression, projection, and dispatch counts; a missing stage blocks release.

### 2. Compression corrupts tool or image structure

Failure: reconstructed Messages payload changes block order, ids, images, or tool results.

Prevention: address only compressible text leaves, require exact shape/cardinality, preserve all untouched blocks byte-equivalently, and fail open atomically.

Detection: property and fixture tests compare structured subtrees and record bounded `projection_fail_open` without content.

### 3. Cloud auth triggers a browser in background gateway work

Failure: a request or refresh calls `ollama signin`.

Prevention: sign-in exists only in an attended foreground coordinator; gateway and sync receive a noninteractive capability that cannot call it; tests install a sign-in tripwire.

Detection: stable `auth_required` status and zero sign-in invocation in every background mode.

### 4. Raw model ids collide or aliases leak upstream

Failure: an `anthropic-ollama-` alias or old `ollama/` prefix reaches Ollama, or raw ids collide with another provider.

Prevention: isolated Codex catalog, catalog-scoped Anthropic aliases, exact alias map, raw-id assertion at both outbound clients, and surface registry cross-check.

Detection: wire-capture tests assert exact model ids; unknown aliases return 404 before dispatch.

### 5. Runtime is duplicated or closed while the other surface is active

Failure: independent app builders create or close different clients, or shutdown closes the shared client before streams quiesce.

Prevention: `CompositionRoot` alone constructs and closes one `OllamaRuntime`; registries are non-owning; shutdown awaits tracked work before one close call.

Detection: identity, creation-count, close-count, startup-failure cleanup, active-stream shutdown, and registry-no-close tests.

## Observability

Use existing prompt-free Headroom and provider metrics. Add bounded Ollama dimensions without recording payloads:

- inbound surface: `responses` or `anthropic_messages`
- outbound protocol: `ollama_responses` or `ollama_messages`
- provider: `ollama`
- catalog state: local count, Cloud count, `current`, `stale`, `disabled`, or `unavailable`
- auth state: `current`, `required`, `disabled`, or `failed`
- Headroom outcome: existing compressed/pass-through/fail-open plus `projection_fail_open`
- dispatch outcome: success, timeout, connect failure, auth required, invalid upstream, pre-stream failure, mid-stream failure
- latency and token aggregates only

Never log prompts, responses, tool arguments/results, images, API keys, device keys, enrollment URLs, full environment, or raw upstream error bodies.

## Cross-goal acceptance criteria

1. Ollama is registered as a Reverso provider on both inbound surfaces.
2. No new port and no `ProviderAdapter` signature change occurs.
3. Responses use the frozen adapter facet; Messages use the optional internal native facet; both receive the identical composition-owned runtime adapter.
4. Headroom runs once before dispatch on both surfaces; Messages projection uses translation-produced source addresses and lossy shapes fail open atomically.
5. Codex Ollama profile/catalog uses raw ids only and reaches Reverso.
6. Claude Code launcher/catalog reaches Reverso; only header-bound full exact aliases route Ollama, bare raw ids are rejected, and the exact raw id is restored byte-for-byte from scoped authority.
7. Inventory is current supported Ollama authority plus `/api/tags`, with no static model list.
8. Cloud opt-out is absolute and local routing remains available.
9. Background paths never sign in; only explicit attended recovery may invoke `ollama signin` once and retry once.
10. No shell profile, device key, or API key is read or persisted; inherited API-key allowlist starts empty.
11. Reverso never starts Ollama or pulls a model.
12. Provider mutations are atomic, idempotent, marker-safe, and isolated from unrelated providers.
13. CI requires no Ollama installation, Cloud account, browser, or network.
14. Live proof records only versions, raw model ids, surface, protocol, status, and non-secret timing.

## Risks and stop rules

- If supported current Cloud eligibility cannot be obtained machine-readably, block Cloud catalog delivery rather than scrape or hard-code.
- If native Messages projection cannot preserve structured blocks exactly, retain the Responses translation path for Ollama and return to ADR review before claiming native fidelity.
- If Ollama 0.32.14 protocol behavior differs from official evidence, record the tested minimum version and revise the ADR.
- If any background path can invoke sign-in, block release.
- If a credential canary appears in any file, log, exception, fixture, argv, or output, block release and remove it from all artifacts.
- If an Ollama failure changes an unrelated provider hash, block release.
- If live local proof requires a pull, report `local_model_required`; do not pull.
- If three unresolved attempts already exist, apply the repository circuit breaker.

## Available agents and staffing

Available roles include `explore`, `researcher`, `dependency-expert`, `architect`, `executor`, `test-engineer`, `debugger`, `verifier`, `code-reviewer`, `critic`, `writer`, `git-master`, and `code-simplifier`.

Recommended durable path: `$ultragoal` with G1 through G4 as sequential goals.

- G1: high-reasoning executor plus architect and test-engineer.
- G2: high-reasoning executor because protocol and structured-data fidelity are critical.
- G3: medium-reasoning executor plus convergence test-engineer.
- G4: verifier, writer, and code-reviewer with high reasoning.
- Architect review precedes code review on G1 and G2.
- Every goal gets independent red-green evidence and exact-head verification.

Team may parallelize tests, documentation, and verification inside one goal, but goals remain sequential because each consumes the prior contract. Example attached-runtime hint:

```text
$team 4:executor "Execute the current approved Ollama routed-provider goal only; keep production, test, review, and verification ownership separate"
```

Use `$ralph` only as an explicit single-goal persistence fallback. Use `$autoresearch-goal` if Cloud authority or protocol behavior remains unresolved. `$performance-goal` is appropriate only for a later measurable latency/throughput goal.

## Handoff status

This is a Planner draft for Architect then Critic review. It does not authorize implementation until the durable Ralplan consensus gate is complete.
