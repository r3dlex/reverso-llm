---
title: OpenCode Go operator guide
---

# OpenCode Go operator guide

How to provision the subscription key, recognise a quota refusal, and tell which
subscription served a request.

## Provisioning the key

The OpenCode Go subscription uses a static key (`sk-...`); there is no OAuth flow
to drive. Store it in the Keychain under the service the proxy reads at startup:

```bash
security add-generic-password -s "reverso/OPENCODE_API_KEY" -a "$USER" -w
```

The proxy injects it into `OPENCODE_API_KEY` at startup. Three details matter:

* A pre-set `OPENCODE_API_KEY` short circuits the Keychain read, which is how
  tests and CI inject a key without touching the Keychain.
* `OCGO_API_KEY` is accepted as a READ ONLY alias, resolved second, so an
  existing `ocgo` export keeps working. Nothing ever writes it, and it is never
  promoted into the canonical variable.
* A variable set to blank or whitespace counts as ABSENT rather than as a
  credential, and falls through to the next candidate. An empty export is a
  deployment mistake; letting it mask a working alias would turn that mistake
  into a confusing upstream 401.

Both names are scrubbed from the environment of every spawned CLI. A launched
agent inherits its parent environment wholesale and no child process needs this
key.

With no key at all, dispatch fails closed before any request is issued. Model
LISTING still works, because `GET /models` is public upstream and is deliberately
sent without a credential.

## What a quota refusal looks like

An upstream `429` surfaces to the client verbatim, carrying status and body,
on both the unary and the streaming path. It is attempted exactly once: there is
no retry against another credential and no reroute to another backend. Silent
fallback would spend a different subscription and leave your quota state
unobservable.

## Three failure modes that are not quota

| Symptom | Meaning | Action |
|---|---|---|
| `403 RegionError` | The model is hosted in China and needs an explicit workspace opt in | Opt in at the URL in the error body |
| `403 DataPolicyError` | The model collects data for quality and needs an explicit opt in | Opt in at the URL in the error body |
| `400 Model is unavailable` / `Unsupported model` | Upstream outage or a retired model, not your account | Retry later, or refresh the catalog |

All 29 catalog ids stay published even when gated, so the error text and its opt
in URL reach you rather than the model quietly disappearing from the picker.

## Which subscription served a request

Two ids can name the same model across different subscriptions, so attribution
matters. Three ids in the catalog are also owned by other backends
(`deepseek-v4-flash`, `deepseek-v4-pro`, `kimi-k3`):

* The BARE id routes to the incumbent backend, not to OpenCode.
* `opencode/<id>` routes to OpenCode.
* On the Claude Code picker, the `anthropic-opencode-<id>` alias routes to
  OpenCode.

The committed `docs/reference/opencode-go-exposure.json` records exactly which
ids are reachable bare and which require the prefix. Headroom metrics carry an
`opencode` provider dimension, so compression behaviour is attributable per
backend rather than collapsing into `other`.

## Refreshing the catalog

The catalog is committed data, not code, so adding a model is not a code change:

```bash
python3 scripts/refresh-opencode-catalog.py --check   # fail closed if stale
python3 scripts/refresh-opencode-catalog.py           # rewrite, then commit
```

Runtime discovery is the authority for LISTING; the committed artifact is the
authority for ROUTING. `--check` reconciles the two and prints the added and
removed ids so an upstream change arrives as a diff to review rather than as a
silent routing change.

After refreshing, regenerate the exposure artifact as well, since a new id may
be contested:

```bash
python3 scripts/check-opencode-exposure.py            # rewrite
python3 scripts/check-opencode-exposure.py --check    # verify
```

## Endpoint selection

Every model is treated as dual protocol except a declared deny list, today
exactly `grok-4.5`, which upstream refuses in the Anthropic format. This was
measured rather than inherited: 22 of the 29 ids answer BOTH `/messages` and
`/chat/completions`. Note that the two paths authenticate differently:
`/chat/completions` requires `Authorization: Bearer` and `/messages` accepts
`X-API-Key` only.

If you probe the API by hand, send a User-Agent. The edge rejects a default HTTP
client fingerprint with Cloudflare error 1010, including on the public
`/models` path, which looks exactly like an auth failure but is not.
