---
title: Ollama Reverso provider attended live test
status: active
goal: OLLAMA-RP-G4
---

# Ollama Reverso provider attended live test

## Purpose

This specification defines the bounded, foreground-only proof for one already
installed Ollama local model and one current Ollama Cloud model through the two
Reverso client surfaces. It does not weaken deterministic acceptance when an
external prerequisite is absent.

## Invocation

`scripts/ollama-live-proof.py` has two modes:

- `preflight` validates executables and the marker-owned inventory without a
  model request or sign-in.
- `run` performs four sequential lanes after the same validation.

The command accepts absolute paths for the inventory, Ollama executable, Codex
executable, Claude Ollama launcher, and `reverso-client-sync` executable.
Every executable must resolve to an executable regular file. The Claude
launcher must carry the `reverso-claude-code-sync` managed marker. Ollama,
Codex, and Claude version output must match their bounded product-specific
formats; substituted executables are rejected. Relative or missing executable
paths and unsupported arguments are invalid input.
`--json` emits the bounded public report. `--evidence <path>` atomically writes
the exact same JSON bytes to an absolute path whose parent directory already
exists. The evidence target may be absent or an existing regular file; symlinks,
directories, relative paths, and missing parents are rejected with exit `64`.
The write uses a mode `0600` temporary file in the same directory, flushes it,
and atomically replaces the target. A write failure exits `1` and emits only the
public `evidence_write_failed` code.

Exit codes are stable:

- `0`: every requested proof lane passed.
- `1`: a proof lane or bounded subprocess failed.
- `2`: an external prerequisite is absent.
- `64`: invocation or input is invalid.

## Inventory authority and candidates

Only `reverso.ollama_convergence.load_inventory` may load the inventory. This
enforces the exact `reverso-client-sync/provider-ollama` ownership marker.

The first inventory row in stored order with `local=true` is the local
candidate. The first row with `cloud=true` and `stale=false` is the Cloud
candidate, but only when inventory `cloud_status` is `current`. Candidate ids
remain raw and are never rewritten.

Missing local authority reports `local_model_required`. Missing current Cloud
authority reports `cloud_model_required` or `cloud_auth_required` when the
inventory explicitly reports `auth_required`. These are external prerequisites
and exit `2`.

## Attended sign-in boundary

Sign-in is allowed only when every condition below is true:

- mode is `run`;
- both `--attended` and `--authorize-signin` are present;
- stdin and stdout are TTYs;
- the Ollama executable is an absolute path;
- Cloud authority is absent because `cloud_status` is `auth_required`.

The coordinator invokes the absolute executable once as `ollama signin`, with
`shell=False`, the inherited terminal, a bounded timeout, and `OLLAMA_API_KEY`
removed from the child environment. It never captures sign-in stdout or stderr.
After a successful sign-in it invokes exactly one
`reverso-client-sync refresh --json` with discarded output, then reloads the
inventory exactly once. No retry or second sign-in occurs.

The coordinator never reads credentials, sources shell exports, or invokes
pull, run, serve, start, stop, or daemon operations.
Every child receives only a minimal allowlist of terminal, account, locale,
path, and temporary-directory environment variables. Credential and token
variables are not inherited.

## Proof lanes

The four lanes run sequentially in this order:

1. Codex Responses with the local raw model id.
2. Claude Messages with the local raw model id.
3. Codex Responses with the Cloud raw model id.
4. Claude Messages with the Cloud raw model id.

Codex uses the `reverso-ollama` profile. Claude uses the marker-owned
`claude-ollama` launcher and the complete `anthropic-ollama-<raw-id>` selector.
Each command receives a fixed non-secret proof instruction. Model request
stdout and stderr go directly to the null device and are never captured.
Codex runs with a read-only sandbox and approval policy `never`. Claude runs
with an empty tool set and permission mode `dontAsk`.

## Public JSON schema

Every result is one JSON object with these fields:

- `schema_version`: integer `1`.
- `mode`: `preflight` or `run`.
- `status`: `passed`, `failed`, `external_prerequisite`, or `invalid`.
- `exit_code`: one of `0`, `1`, `2`, or `64`.
- `prerequisites`: sorted public prerequisite codes only.
- `versions`: executable name to bounded version string.
- `candidates`: `local` and `cloud` raw model ids or null.
- `lanes`: ordered objects containing only `model`, `surface`, `protocol`,
  `status`, and `duration_ms`.
- `duration_ms`: total non-secret elapsed milliseconds.

Protocols are derived from the fixed surface contract, not subprocess output:
Codex records `ollama_responses` and Claude records `ollama_messages`. Public
evidence never contains prompts, responses, stderr, environment values,
credentials, enrollment URLs, or upstream error bodies.

## Stop rules

The proof stops before model requests if either candidate is missing after the
single authorized recovery path. It stops after the first failed lane. It never
pulls a model, manages Ollama daemon state, changes a route, opens another port,
or reuses an existing E2E or live-proof script.
