---
title: Ollama Cloud model authority and routing alias
status: accepted
date: 2026-08-22
---

# ADR 0019: Ollama Cloud model authority and routing alias

## Context

ADR 0018 shipped the Ollama runtime with local inventory only. The
`ollama-reverso-provider` specification blocked Cloud publication outright: it
required "a supported machine-readable Ollama Cloud authority" and recorded that
none was established, while barring suffix inference over local `/api/tags`
rows, HTML scraping of the Cloud search page, and static shipped lists. The
consequence was a permanently `unavailable` Cloud status: the catalog carried a
`cloud` flag that nothing could ever set, and both client surfaces (the Codex
`reverso-ollama` profile and the Claude Code `ollama` catalog) published local
models only.

Ollama now documents a machine-readable authority. Two facts were verified
against the live service at ollama version 0.32.14:

1. `GET https://ollama.com/api/tags` returns a validated model list and is
   documented under "Listing models" at <https://docs.ollama.com/cloud>. A
   parallel `GET https://ollama.com/v1/models` returns the identical set.
2. The authority publishes bare ids (`gpt-oss:120b`). The local Ollama service
   routes the same model only under the documented `-cloud` alias. A bare id is
   rejected locally with `model 'gpt-oss:120b' not found`, while
   `gpt-oss:120b-cloud` succeeds on both surfaces Reverso dispatches to
   (`/v1/responses` and `/v1/messages`) with no prior `ollama pull`.

## Decision

`GET https://ollama.com/api/tags` is the sole permitted Cloud authority. The
catalog probes it exactly once per refresh, bounded and prompt-free, alongside
the existing local `/api/tags` read. `ollama signin` is never invoked from a
background refresh; `OLLAMA_API_KEY` is forwarded as a bearer credential when
present.

Each authority-published id is published as its documented local routing alias
`<authority-id>-cloud`, and an authority id already carrying the alias is not
double-suffixed. This is a documented alias applied to an authority-published
id, which is categorically different from the barred practice of inferring
Cloud eligibility from an unlabelled local row: a local `/api/tags` row ending
in `-cloud` remains `local`, never `cloud`.

Routing is unchanged. Cloud models reach the same validated loopback endpoint,
so the loopback-only invariant and the frozen `ProviderAdapter` contract both
hold, and no second upstream client or non-loopback origin is introduced. A
direct authenticated `https://ollama.com` upstream was rejected for exactly this
reason.

Probe outcomes map to the existing status taxonomy, and every non-`current`
outcome preserves the full validated local inventory:

| Outcome | Status |
|---|---|
| `2xx` with at least one validated row | `current` |
| `401` / `403` | `auth_required` |
| other `4xx` / `5xx`, transport failure | `unavailable` |
| timeout | `timeout` |
| malformed payload, unvalidatable id, empty model list | `invalid` |

An empty model list is deliberately `invalid` rather than `current`. Treating it
as `current` would let one degraded authority response silently retire every
Cloud row; `invalid` routes it through the existing stale-retention path
instead.

Live Cloud status now belongs to the catalog, not to `OllamaAuthState`. The auth
state carries the *requested* posture (`disabled` when opted out, `unavailable`
before any probe); `OllamaCatalog.cloud_status` carries the observed result and
feeds `model_discovery_source`.

## Consequences

Both client surfaces gain Cloud models with no per-surface work, because both
already derive from the one shared inventory: `client_sync` reads the gateway's
`/ollama/v1/models`, plans one inventory snapshot, and publishes it to the Codex
profile/catalog and the Claude Code catalog inside a single rollback group. The
existing twice-daily `com.user.reverso-catalog-refresh` LaunchAgent supplies the
dynamic update; no new scheduler is added.

`OLLAMA_NO_CLOUD=1` and `REVERSO_OLLAMA_CLOUD=0` remain absolute opt-outs and
skip the probe entirely. `REVERSO_OLLAMA_CLOUD_AUTHORITY` allows overriding the
authority URL, validated to a plain credential-free `ollama.com` HTTPS URL so
the override cannot redirect discovery to an arbitrary host.

Each refresh now performs one outbound HTTPS request to `ollama.com`. Cloud
models are usable only while the machine holds valid Cloud credentials; a
credential loss degrades to `auth_required` and the existing request-time
revalidation returns `409 auth_required` rather than routing elsewhere.

Cloud rows inherit the provider-wide Codex catalog context window of 2048
tokens from `model_exposure.codex_catalog_context_window`, which is far below
their real limits. `POST /api/show` reports authoritative per-model context
lengths and capabilities for both local and Cloud models through the same
loopback endpoint, so correcting this is tractable, but it is out of scope here
and left as follow-up.
