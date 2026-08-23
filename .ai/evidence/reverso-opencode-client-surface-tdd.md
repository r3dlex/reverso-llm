---
title: reverso-opencode-client-surface red-green evidence
goal: reverso-opencode-client-surface (slices B1-B5)
date: 2026-08-23
---

# reverso-opencode-client-surface red-green evidence

Shipped as one PR: B1's manifest contract wiring imports
`opencode_sync.LAUNCHER_CATALOGS`, so validator, sync writer, profiles, tests,
and docs are one atomic, self-consistent unit. Splitting slices across PRs
would leave intermediate states that fail `validate_supported_surface_manifest`
fail-closed by design.

## B1 (surface declaration)

Red command:

```text
uv run pytest tests/unit/test_opencode_surface_contract.py tests/unit/test_client_sync.py -q
```

Result: 3 passed (new contract tests), 17 failed. The 17 failures were the
manifest validator rejecting the shipped surfaces
(`EXPECTED_SURFACES` had no opencode fragment entries), which fail-closed every
`run()` payload and cascaded into the planner/writer suite.

Green: after extending `EXPECTED_SURFACES` with the eight fragment entries and
the `opencode_launchers` drift/inventory checks, the same selection exits 0 with
129 passed on the pre-rebase tree, and the layered rebase onto origin/main
(308bfdf) keeps the full unit suite green (1456 passed).

## B2+B3 (sync writer + prefix-correct profiles)

Red: `tests/unit/test_opencode_sync.py` collection error - the module API under
test did not exist yet (`AttributeError: MANAGED_MARKER`). This is the required
harness gap.

Green: `uv run pytest tests/unit/test_opencode_sync.py -q` exits 0 with 12
passed. Covered invariants mirroring the codex/claude sync families: prepare is
byte-nonmutating; discovery error / empty listing / unroutable bare id each fail
closed with no mutations; unmanaged-fragment conflict fails closed preserving
user bytes; apply requires a live shared-lock token; applied fragments carry the
managed marker; second run is idempotent (`changed=false`); user
`opencode.json`/`opencode.jsonc` untouched with a printed manual step; fragment
binds provider `reverso` to `http://127.0.0.1:64946`; per-route fragments expose
exactly their backend's selectors with no new prefix namespaces.

## B4 (Headroom-on-path contract)

Red then green: driving `POST /v1/messages` with a fragment selector initially
returned 404 for `kimi-k3` because the test harness stub set omitted a kimi
adapter; after adding it, all five selector families pass with exactly one
`compress_responses_request(surface="anthropic_messages")` call per request.
A poisoned-spy negative control fails loudly if compression leaves the path,
so the suite structurally detects a future bypass.

Green: `uv run pytest tests/unit/test_opencode_headroom_path.py -q` exits 0
with 6 passed.

## B5 (live proof script)

The OCG series already owns `scripts/opencode-live-proof.py` (backend proof),
so this slice ships the client-side mirror as
`scripts/opencode-client-live-proof.py` plus importable module
`src/reverso/opencode_client_live_proof.py`, opt-in via
`REVERSO_OPENCODE_CLIENT_LIVE_PROOF=1`.

Green: `uv run pytest tests/unit/test_opencode_client_live_proof.py -q` exits 0
with 6 passed (transport injected; secret-free report; request shape matches a
generated fragment). The skipped lane is exercised by running the script
without opt-in.

## Full gates (branch feat/opencode-client-surface @ post-rebase)

```text
uv run pytest tests/ -q --ignore=tests/integration   # 1456 passed
uv run python -m py_compile src/**/*.py              # compile OK
uvx prek run --all-files                             # all hooks Passed
bash scripts/validate-rules.sh                       # OK
bash scripts/archgate.sh structural .rules.ts        # pass
```

## Rebase note

The branch was rebased from the stale `feat/ollama-cloud-catalog-authority`
base onto origin/main after discovering the merged OpenCode Go provider work
(`provider-opencode`, #133 through #141). Manifest additions were re-layered
onto the new groups/surfaces; the client layer remains complementary and the
spec carries a dated scope note separating the two efforts.
