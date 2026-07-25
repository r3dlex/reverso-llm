---
title: Kimi automatic login K3 runtime convergence S5 evidence
slug: kimi-auto-login-k3-runtime-convergence-s5-evidence
status: passed
date: 2026-07-25
deployment_commit: 8c7b7d232a372afce7072069d4dde68a32c7eaf4
delivery_status: pending
---

# Kimi automatic login K3 runtime convergence S5 evidence

## Scope

This sanitized handoff records the governed S5 deployment and live acceptance
for the Northstar `kimi-auto-login-k3-runtime-convergence` handoff. It contains
no OAuth output, credentials, tokens, authorization headers, device codes, or
private command captures.

## Governed deployment

- The canonical checkout at
  `/Users/andresilvaburgstahler/.local/share/reverso` was clean at merged commit
  `8c7b7d232a372afce7072069d4dde68a32c7eaf4`.
- The `pre-install`, `pre-restart`, `post-restart`, `pre-sync`, and `acceptance`
  deployment drift phases passed.
- The proxy and daemon were reinstalled and restarted from the canonical
  checkout. Their running commands use `uv run --project` with that checkout.
- The deployment provenance record identifies the exact merged commit.
- The deployment provenance record uses schema version `2`; the acceptance
  validator reported provenance `valid` and status `passed`.
- The proxy and daemon LaunchAgents were running after restart.

## Isolated Kimi home

- The proxy uses
  `~/Library/Application Support/reverso/kimi-code` as its exact
  `KIMI_CODE_HOME`.
- The isolated Kimi home exists with mode `0700` and is not a symlink.
- The daemon does not receive `KIMI_CODE_HOME`.
- The initial isolated credential state was absent.
- The completed official login created the isolated credential artifact with
  mode `0600`.
- The acceptance flow did not inspect, copy, delete, or mutate the user's
  default `~/.kimi-code`.

## Credential-free bootstrap

- The first request used a temporary mode-`0700` `CODEX_HOME`.
- Its configuration explicitly selected provider `reverso_kimi`, model
  `kimi-k3`, the loopback Kimi Responses endpoint, and no generated profile or
  catalog.
- `KIMI_BEARER_TOKEN` was absent.
- One shared official `kimi login` flow completed.
- The original request resumed and exited successfully with the exact result
  `KIMI_BOOTSTRAP_OK`.
- The captured request contained zero reconnect or error markers.
- No login subprocess remained after completion.

## Discovery, sync, and generated metadata

- Before authentication, discovery used the governed fallback and exposed only
  `kimi-k3`.
- After authentication, live discovery reported
  `model_discovery_source: "live"` and exposed only `kimi-k3`.
- The `pre-sync` drift phase passed before `reverso-codex-sync` ran.
- The generated Kimi profile selects model `kimi-k3`, provider
  `reverso_kimi`, and context window `1048576`.
- The generated Kimi catalog contains exactly one model, `kimi-k3`, with
  context window and maximum context window `1048576`.
- The adapter verification proved that public `kimi-k3` is dispatched upstream
  as `k3`; no stale public or upstream fallback id is accepted.
- The `acceptance` drift phase passed after sync.

## Normal generated-profile request

- A second request ran through the generated `kimi` profile.
- The request exited successfully with the exact result `KIMI_PROFILE_OK`.
- The captured request contained zero reconnect or error markers.
- The maximum observed `kimi login` process count during the second request was
  zero.

## Final sanitized readback

The final readback on 2026-07-25 reconfirmed:

- canonical checkout and deployment provenance commit
  `8c7b7d232a372afce7072069d4dde68a32c7eaf4`;
- passing `acceptance` deployment drift;
- running proxy and daemon from the canonical checkout;
- live discovery with only `kimi-k3`;
- profile provider `reverso_kimi`, model `kimi-k3`, and context window
  `1048576`;
- one catalog entry, `kimi-k3`, with context window and maximum context window
  `1048576`;
- isolated Kimi home mode `0700`, credential mode `0600`, and daemon exclusion;
- no remaining `kimi login` subprocess.

## Verification and delivery governance

The live acceptance result and the delivery gates are tracked separately. Live
acceptance passed; delivery remains fail-closed until the dedicated PR reaches
hosted green and receives run-specific merge authority.

### Deployment and redaction verification

- `uv run python scripts/check-deployment-drift.py --phase pre-install`,
  `--phase pre-restart`, `--phase post-restart`, `--phase pre-sync`, and
  `--phase acceptance` all passed in the required execution order.
- The final acceptance result was provenance `valid`, phase `acceptance`, and
  status `passed`.
- The sanitized evidence scan passed for credential material, tokens,
  authorization headers, device codes, private keys, JWT-shaped values, raw
  child output, and debug markers.
- `git diff --check` and the forbidden U+2013/U+2014 dash scan passed.
- No credential contents were read or captured.

### Local verification

All local verification ran against deployment commit
`8c7b7d232a372afce7072069d4dde68a32c7eaf4` plus this evidence-only change:

- `uv run pytest tests/unit/test_kimi_adapter.py -q`: 58 passed.
- `uv run pytest tests/integration/test_kimi_surfaces.py -q`: 31 passed.
- `uv run pytest tests/unit/test_codex_sync.py -q`: 83 passed.
- `uv run pytest tests/unit/test_model_exposure.py -q`: 10 passed.
- `uv run pytest tests/integration/test_deployment_drift.py -q`: 62 passed.
- `uv run pytest tests/unit -q`: 863 passed.
- `uv run pytest tests/integration -q`: 436 passed, 6 skipped.
- `uvx --from ruff==0.6.0 ruff check src tests`: passed.
- `uvx --from ruff==0.6.0 ruff format --check src tests`: passed.
- `uvx prek run --all-files`: passed.
- `uv run python -m compileall -q src tests scripts`: passed.
- `bash scripts/validate-rules.sh`: passed.
- `bash scripts/archgate.sh structural .rules.ts`: passed.
- The frozen `src/reverso/protocols/adapter.py` matched the governed base.

### Hosted and review gates

- Hosted CI: `not-run`. The S5 branch and dedicated PR do not exist on the host
  at the time of this evidence commit. All five required checks must pass on the
  exact PR head before merge.
- Architecture review: `changes-requested` on the predecessor evidence commit
  because this governance ledger was missing; the finding is addressed here.
  Exact-head follow-up review is `pending`.
- Code review: `changes-requested` on the predecessor evidence commit for the
  same omission; the finding is addressed here. Exact-head follow-up review is
  `pending`.
- Review threads: `not-run` until the dedicated PR exists; zero unresolved
  threads are required before merge.

### Host merge authority

- State: `not-run`.
- A fresh run-specific host-policy verdict may be generated only after local
  verification, hosted checks, exact-head reviews, and review-thread readback
  are green.
- No prior confirmation token or verdict is reused. No token value is recorded
  in this artifact.
- Unless that fresh verdict is approved with policy-reported valid authority,
  the merge remains fail-closed.

## Result

S5 deployment and live OAuth acceptance passed. The original credential-free
request resumed after the shared official login flow, generated metadata
converged on K3 with the required context window, and the normal profile request
did not reopen login. Dedicated-PR delivery remains pending until hosted CI,
exact-head reviews, resolved-thread readback, and fresh host merge authority are
all green.
