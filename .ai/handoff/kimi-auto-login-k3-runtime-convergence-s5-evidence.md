---
title: Kimi automatic login K3 runtime convergence S5 evidence
slug: kimi-auto-login-k3-runtime-convergence-s5-evidence
status: passed
date: 2026-07-26
deployment_commit: 14d8d96be40037d44ee0b5cd10ae2f113201c0fb
delivery_status: passed
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
  `14d8d96be40037d44ee0b5cd10ae2f113201c0fb`.
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

## Post-refresh acceptance

The final readback on 2026-07-26 reconfirmed the deployed dependency refresh
and generated client surfaces:

- canonical checkout, `origin/main`, and deployment provenance commit
  `14d8d96be40037d44ee0b5cd10ae2f113201c0fb`;
- passing `acceptance` deployment drift with provenance `valid`;
- gateway smoke result `5 passed, 0 failed`;
- required loopback listeners on ports `64946`, `58787`, `58788`, and `58789`;
- Codex profile `reverso-kimi` returned exactly
  `CODEX_REVERSO_KIMI_OK` with exit code zero;
- managed alias `claude-kimi` with model `kimi-k3` returned exactly
  `CLAUDE_ALIAS_KIMI_OK` with exit code zero;
- no `kimi login` subprocess before or after either request;
- Codex sync convergence reported `changed: false`;
- Claude sync convergence reported `changed: false`, no conflicting launchers,
  and no error;
- Kimi catalog exposure contained only `kimi-k3` with context window
  `1048576`, and the generated Kimi profile selected the same model and context;
- all managed Claude aliases existed and were executable:
  `claude-reverso`, `claude-claude`, `claude-codex`, `claude-copilot`,
  `claude-auggie`, `claude-deepseek`, and `claude-kimi`.

The post-refresh capture retained only the exact sentinel outputs and sanitized
status fields. It did not record credentials, tokens, authorization headers,
device codes, or provider output.

## Verification and delivery governance

The live acceptance result and delivery gates are tracked separately. Live
acceptance and dedicated delivery passed.

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

The original S5 local verification ran against deployment commit
`8c7b7d232a372afce7072069d4dde68a32c7eaf4` plus its evidence-only change:

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

- S5 shipped through PR #105 at exact head
  `ba183f2dbc7a3313c1b670b75aa516b4ac7f001b` and squash merge commit
  `10b43a11a8c7a80dcaacf41e870cb32c18265da0`.
- All five required hosted checks succeeded on the exact S5 PR head.
- Exact-head architecture and code reviews had no blocking findings.
- Hosted readback found zero unresolved review threads.
- Follow-up convergence and dependency work shipped through PRs #106 and #107.

### Host merge authority

- Merge authority remained a per-PR, run-specific gate and was not inferred from
  hosted green status.
- No confirmation token or private host-policy capture is recorded in this
  artifact.
- This closure record does not reuse or claim historical host-policy authority
  for PR #105.

## Result

S5 deployment and live OAuth acceptance passed. The original credential-free
request resumed after the shared official login flow, generated metadata
converged on K3 with the required context window, and the normal profile request
did not reopen login. Dedicated delivery passed, subsequent installation and
dependency refreshes remained converged, and fresh Codex and Claude alias
acceptance passed without reopening login.
