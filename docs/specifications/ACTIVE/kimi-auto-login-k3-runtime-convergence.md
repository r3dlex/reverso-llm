---
title: Kimi automatic login and K3 runtime convergence
status: complete
slug: kimi-auto-login-k3-runtime-convergence
---

# Kimi automatic login and K3 runtime convergence

| | |
|---|---|
| **Slug** | `kimi-auto-login-k3-runtime-convergence` |
| **Repo** | `r3dlex/reverso-llm` |
| **Status** | complete |
| **Raised** | 2026-07-24 via `northstar` |
| **Category** | enhancement |
| **Owner** | unassigned |

## A to B

**A.** Missing Kimi OAuth fails every request with retryable HTTP 502. The
accepted ADR, active specification, tests, and legacy handoff require users to
run `kimi login` separately. Responses and Anthropic construct separate Kimi
auth instances. The installed LaunchAgent runs a stale checkout that exposes
`kimi-k2.5`, and Codex sync has regenerated a K2.5 profile and catalog with
inconsistent context metadata.

**B.** Reverso owns a gateway-wide, bounded, single-flight supervisor for the
official `kimi login` command. All missing-auth callers across both protocol
surfaces wait for the same child, reload the CLI-owned artifact, and resume.
Shutdown and every failure path reap the child without exposing secrets. The
governed checkout, installed LaunchAgent, live discovery endpoint, generated
profile, and generated catalog converge on Codex-facing `kimi-k3`, upstream
`k3`, and context window `1048576`.

## Product contract

1. Resolve the Kimi data root from `KIMI_CODE_HOME`, otherwise
   `~/.kimi-code`.
2. Start exactly one asynchronous `kimi login` subprocess for the gateway only
   during pre-upstream credential resolution when `KIMI_BEARER_TOKEN` is absent
   and the resolved artifact is absent, or the existing credential-schema
   parser determines that the artifact contains neither a usable access
   credential nor a usable refresh credential.
3. Do not call Moonshot device authorization or token endpoints from Reverso.
4. Let Kimi CLI open the browser, display the verification information, poll
   authorization, handle individual device-code expiry, and securely persist
   the credential.
5. Make concurrent missing-auth requests from Responses and Anthropic await the
   same login task.
6. Bound Reverso's wait to the Kimi CLI device-flow bound and a small cleanup
   allowance.
7. After successful exit, reload
   `<resolved-kimi-home>/credentials/kimi-code.json` and resume each original
   request.
8. Reap or terminate the child on success, nonzero exit, timeout,
   cancellation, and gateway shutdown.
9. Return bounded actionable errors for missing executable, timeout,
   cancellation, nonzero exit, missing post-login artifact, and malformed
   post-login artifact.
10. Never log credential contents, access or refresh tokens, device codes, or
    authorization headers.
11. Preserve `KIMI_BEARER_TOKEN`, refresh locking, one retry on HTTP 401, and
    existing `KimiOAuthAuth` refresh rotation plus atomic persistence. The
    official CLI owns interactive authorization and initial artifact creation.
12. Expose only Codex-facing `kimi-k3` and translate it to upstream `k3`.
13. Keep profile and catalog context metadata consistent at `1048576`.
14. Install and sync from one governed canonical checkout so future pull,
    restart, install, and sync operations cannot restore K2.5 behavior.
15. An artifact with a usable refresh credential remains on the existing
    refresh path even if its access credential is absent or expired.
16. Never start interactive login for refresh transport failure, refresh
    endpoint non-2xx, malformed refresh response, HTTP 401 after the existing
    refresh and single-retry policy, upstream 4xx/5xx, timeout, DNS, TLS,
    connection, cancellation, rate limiting, provider failure, or another
    transient runtime failure.
17. If post-login reload finds the artifact absent or structurally unable to
    provide a usable access or refresh credential, fail with the bounded
    post-login artifact error and do not recursively start login.
18. The governed LaunchAgent installer creates
    `~/Library/Application Support/reverso/kimi-code` with mode `0700`, binds
    that exact path to the proxy as `KIMI_CODE_HOME`, and records it in
    deployment provenance. The daemon does not receive `KIMI_CODE_HOME`, and
    the user's default `~/.kimi-code` remains untouched.

## Architecture decision

Add a gateway-owned Kimi login coordinator that is independent of the frozen
`ProviderAdapter` protocol. The composition root constructs one coordinator and
injects it into the Kimi auth used by both Responses and Anthropic adapters.
The composition root also owns the coordinator's shutdown lifecycle.

The coordinator owns only:

- single-flight task creation and waiter coordination;
- `kimi login` child supervision;
- timeout, cancellation, and process reaping;
- post-exit artifact reload signaling;
- secret-free error classification.

`KimiOAuthAuth` remains responsible for token resolution, refresh locking,
refresh rotation, and atomic persistence of refreshed credentials. Kimi CLI
remains responsible for interactive OAuth protocol handling and initial
credential artifact creation.

## Governance changes

1. Amend ADR 0017 to authorize supervised local login through the official
   Kimi CLI.
2. Amend the active Kimi provider specification and acceptance criteria.
3. Update troubleshooting documentation to use `kimi login`.
4. Update `.ai/handoff/kimi-provider-implementation-handoff.md` to remove
   obsolete slash-command guidance and point to the new contract.
5. Keep interactive OAuth implementation inside Reverso explicitly prohibited.

## Sliced goals

| Slice | Title | Type | Status | Blocked by |
|---|---|---|---|---|
| S1 | Govern shared login coordinator and first resumed Responses request | AFK | ready-for-agent | none |
| S2 | Prove cross-surface single-flight and bounded lifecycle cleanup | AFK | blocked | S1 |
| S3 | Converge K3 model exposure, profile, catalog, and context metadata | AFK | ready-for-agent | none |
| S4 | Govern canonical LaunchAgent provenance and deployment drift checks | AFK | blocked | S1, S3 |
| S4A | Govern an isolated Kimi home in deployment provenance | AFK | blocked | S4 |
| S5 | Deploy, bootstrap live OAuth, sync, and publish evidence | HITL | blocked | S2, S4A |

Each slice is one future PR. S1 and S3 can proceed in parallel in isolated
worktrees. S5 is HITL only for browser-side OAuth authorization and for any
host-policy merge authority that cannot be established automatically.

## Acceptance criteria

1. The first missing-auth Responses request waits for `kimi login`, reloads the
   artifact, and succeeds rather than returning the current missing-auth 502.
2. Concurrent missing-auth requests across Responses and Anthropic create
   exactly one child process and resume after the same task completes.
3. `KIMI_CODE_HOME` and default-home artifact resolution are both tested.
4. Timeout, cancellation, missing executable, nonzero exit, successful exit
   without an artifact, malformed artifact, and shutdown are bounded and
   secret-free.
5. The child is reaped on every terminal path.
6. Refresh locking, `KIMI_BEARER_TOKEN`, one retry on HTTP 401, and credential
   redaction remain correct.
7. Model exposure contains only `kimi-k3`; adapter dispatch translates it to
   upstream `k3`.
8. The generated Kimi profile selects `kimi-k3`, uses provider
   `reverso_kimi`, references the Kimi catalog, and has context window
   `1048576`.
9. The generated Kimi catalog contains exactly one slug, `kimi-k3`, with
   context window `1048576`.
10. Deployment-drift tests prove the LaunchAgent project path, installed code,
    live endpoint, profile, and catalog use one governed authority.
11. Live discovery reports `model_discovery_source: "live"` and never exposes
    K2.5.
12. With credentials absent, one live Codex request opens exactly one official
    login flow, resumes after authorization, and returns an assistant response
    without reconnect events.
13. A second live request does not reopen login.
14. Local tests, hosted CI, review resolution, and host-policy merge authority
    are independently green before automatic merge.
15. Missing artifact and structurally unusable artifact states trigger one
    login before any upstream request when the bearer fallback is absent.
16. Usable refresh material, refresh failures, exhausted HTTP 401 policy,
    upstream errors, and transient network or provider failures never trigger
    interactive login and preserve existing bounded secret-free behavior.
17. S5 installs and restarts governed code, then runs the first request from an
    isolated temporary Codex configuration that explicitly sets
    `model_provider = "reverso_kimi"` and `model = "kimi-k3"` without using the
    generated profile or catalog. Only after that request resumes does S5 run
    `pre-sync` and `reverso-codex-sync`, validate generated metadata, and run a
    second normal profile request that does not reopen login.
18. The installer creates the isolated Reverso Kimi home with mode `0700`;
    deployment provenance, the rendered proxy LaunchAgent, and the running
    proxy agree on its exact path. Any mismatch fails closed.
19. The daemon has no `KIMI_CODE_HOME`, and live bootstrap does not read,
    copy, delete, or otherwise mutate the user's default `~/.kimi-code`.

## Red-green proof

Before implementation, add failing tests for:

- single-flight subprocess creation;
- concurrent callers on one and both protocol surfaces;
- successful credential reload;
- `KIMI_CODE_HOME` resolution;
- timeout and cancellation;
- missing executable and nonzero exit;
- successful exit without an artifact;
- malformed post-login artifact;
- child cleanup and gateway shutdown;
- the original ASGI request waiting and succeeding;
- K3-only model exposure and upstream translation;
- generated Kimi profile and catalog consistency;
- LaunchAgent project provenance and sync authority.
- isolated Kimi home provenance, mode, rendered proxy state, running proxy
  state, and daemon exclusion;
- positive login triggers for missing and structurally unusable artifacts;
- negative login-trigger coverage for usable refresh material, refresh
  failures, exhausted HTTP 401 handling, upstream failures, and transient
  network or provider failures;
- credential-free K3 bootstrap request before sync and generated-profile use.

Preserve all existing secret-redaction assertions.

## Mandatory verification

```sh
uv run pytest tests/unit/test_kimi_adapter.py -q
uv run pytest tests/integration/test_kimi_surfaces.py -q
uv run pytest tests/unit/test_codex_sync.py -q
uv run pytest tests/unit/test_model_exposure.py -q
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run python -m compileall -q src/reverso
git diff --check
```

Run configured lint and repository validation. If Ruff is unavailable, record
the exact gap without adding it as a dependency. Scan changed files and
captured evidence for debug markers and leaked secrets.

## Deployment acceptance

1. Install and restart LaunchAgents from the governed canonical checkout.
   Confirm the installer-created isolated Kimi home has mode `0700`, proxy
   provenance converges on it, and `~/.kimi-code` is not used or mutated.
2. With `KIMI_BEARER_TOKEN` absent and the artifact absent or structurally
   unable to provide usable access or refresh material, create a mode-700
   temporary `CODEX_HOME` whose `config.toml` explicitly sets
   `model_provider = "reverso_kimi"`, `model = "kimi-k3"`, and the local
   `http://127.0.0.1:64946/kimi/v1` provider with `wire_api = "responses"` and
   `requires_openai_auth = false`. Do not use the generated Kimi profile or
   catalog.
3. Run `CODEX_HOME="$bootstrap_dir" codex exec --skip-git-repo-check "Reply
   with exactly: KIMI_BOOTSTRAP_OK"` and verify exactly one official shared
   login flow. The user may complete browser authorization. Verify the original
   request resumes without reconnect events and returns `KIMI_BOOTSTRAP_OK`.
4. Only after the first request succeeds, run the `pre-sync` drift phase and
   `reverso-codex-sync`.
5. Read back the live Kimi models endpoint, generated profile, and generated
   catalog without exposing secrets. Require live discovery, `kimi-k3`, and
   context window `1048576`.
6. Run the acceptance drift phase.
7. Run a second request through the normal generated Kimi profile and verify
   that login is not reopened.

## Planning and handoff sequencing

Publish the Northstar A to B handoff after RALPLAN consensus and before
Autobahn prerequisite discovery. Its absence during Critic review is expected
and is not a circular prerequisite. Autobahn prerequisite discovery must not
begin until the consensus-approved handoff exists. This ordering does not
claim or alter runtime preflight support.

## Non-goals

- Reimplementing interactive OAuth or device polling in Reverso, or moving
  initial artifact creation away from the official CLI.
- Changing the frozen `ProviderAdapter` protocol.
- Changing loopback binding.
- Changing provider-agnostic Headroom behavior.
- Adding a runtime dependency without separate approval.
- Reading or recording credentials, tokens, device codes, or authorization
  headers.
- Reusing the archived `codex-oauth-provider-reverso` Northstar handoff.
- Modifying or accidentally committing unrelated dirty work.

## Merge policy

Automatic merge is requested but remains fail-closed. Green local tests and
hosted CI are necessary but not sufficient. Every review comment must be
resolved and a valid host-policy-approved merge verdict or token must authorize
the merge. Without that authority, the terminal state is `ready-for-human`.

## Completion

The implementation, deployment acceptance, compatibility repairs, installation
convergence, and dependency refresh shipped in PRs #97 through #107. The
sanitized live and post-refresh evidence is recorded in
`.ai/handoff/kimi-auto-login-k3-runtime-convergence-s5-evidence.md`.

## References

- `.omx/context/kimi-auto-login-k3-runtime-convergence-20260724T143725Z.md`
- `docs/architecture/adr/0017-kimi-code-oauth-provider.md`
- `docs/specifications/ACTIVE/kimi-subscription-provider.md`
- `src/reverso/protocols/adapters/kimi.py`
- `src/reverso/proxy/compose.py`
- `src/reverso/protocols/anthropic_app.py`
- `src/reverso/codex_sync.py`
- `src/reverso/protocols/model_exposure.py`
- `scripts/install-launchagents.sh`
- `launchd/com.user.reverso-proxy.plist.tmpl`
- Kimi command reference:
  <https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html#kimi-login>
- Kimi data locations:
  <https://moonshotai.github.io/kimi-code/en/configuration/data-locations.html>
