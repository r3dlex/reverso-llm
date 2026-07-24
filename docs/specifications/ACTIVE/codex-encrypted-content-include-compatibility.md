---
title: Codex encrypted-content include compatibility
status: active
slug: codex-encrypted-content-include-compatibility
---

# Codex encrypted-content include compatibility

| | |
|---|---|
| **Slug** | `codex-encrypted-content-include-compatibility` |
| **Repo** | `r3dlex/reverso-llm` |
| **Status** | active |
| **Raised** | 2026-07-24 via `northstar` |
| **Spec owner** | unassigned |
| **Affected providers** | Auggie, Claude, DeepSeek, Kimi |
| **Control providers** | Copilot, Codex-direct |

## A -> B

**A.** Codex 0.145.0 sends
`include: ["reasoning.encrypted_content"]` on every Responses request. Reverso
extracts any non-empty `include` as the generic `include` capability and rejects
it before normalization for Auggie, Claude, DeepSeek, and Kimi. The normalizer
would otherwise remove the field before dispatch to these translated providers.
Copilot and Codex-direct accept the field.

**B.** Reverso recognizes the exact Codex encrypted-content sentinel as a
provider-neutral compatibility hint. The hint is accepted and removed by the
existing shared normalizer as a documented no-op before every routed adapter,
including Copilot. Codex-direct remains unchanged. Every other non-empty or
mixed `include` request continues through the existing provider classification:
it fails closed for Auggie, Claude, DeepSeek, and Kimi, while Copilot preserves
its current generic-include gate behavior.

## Live evidence

| Profile | With Codex include | Without include | Separate provider condition |
|---|---|---|---|
| `reverso-auggie` | 400 `unsupported_feature` | 200 | quota-exceeded response text |
| `reverso-claude` | 400 `unsupported_feature` | 502 | `ClaudeAuthError` |
| `reverso-codex` | 200 | 200 | response output was empty |
| `reverso-copilot` | 200 | 200 | healthy assistant response |
| `reverso-deepseek` | 400 `unsupported_feature` | timeout after 40 seconds | downstream latency or availability |
| `reverso-kimi` | 400 `unsupported_feature` | 502 | missing Kimi credentials |

The include compatibility defect affects four of six installed profiles.
Provider-health failures observed after removing `include` are independent and
must be reported separately.

## Compatibility contract

1. The only compatibility value granted special handling is the exact list
   `["reasoning.encrypted_content"]`.
2. For Auggie, Claude, DeepSeek, and Kimi, the exact value is accepted before
   normalization and then removed by the existing Responses normalizer.
3. Acceptance does not claim that these providers produce or preserve encrypted
   reasoning content.
4. Copilot continues to accept the exact value at the feature gate, but the
   shared normalizer removes it before adapter dispatch. This plan makes no
   Copilot upstream pass-through claim.
5. Codex-direct route behavior is unchanged. It is a control route, not the
   rectangular `codex` capability-table column, and this work must not add a
   `/codex/v1/responses` route.
6. Empty `include` retains its current no-feature behavior.
7. Unknown values, multiple values, mixed values, and future include
   capabilities are not granted by this compatibility exception. They retain
   the existing generic-include classification for each provider.
8. Non-list values receive no special compatibility grant and continue through
   the existing request parsing contract.
9. The frozen `ProviderAdapter` protocol is unchanged.

## Governed capability model

The implementation must preserve the parity surface as the source-owned
capability seam:

1. Add a distinct feature key for the exact sentinel, named
   `include.reasoning.encrypted_content`.
2. `extract_features` emits that key only when `include` is structurally equal
   to `["reasoning.encrypted_content"]`.
3. Every other non-empty list-valued `include`, including mixed or multiple
   values, continues to emit generic `include`.
4. Generic `include` remains unsupported for Auggie, Claude, DeepSeek, and Kimi
   and remains `native` for Copilot. Copilot's shared-normalizer omission is
   unchanged and is not broadened by the exact-sentinel exception.
5. The exact feature is classified as `partial` for all five routed Responses
   providers: Auggie, Claude, Copilot, DeepSeek, and Kimi. The documented
   partial boundary is request-shape acceptance without encrypted-content
   forwarding.
6. The exact feature is classified as `unsupported` for the rectangular,
   non-routing `codex` column. This is table completeness only, must not affect
   `codex-direct`, and must not grant a Responses route.

## Multi-turn semantics

Translated providers do not emit OpenAI encrypted reasoning items. Ignoring the
exact sentinel therefore supplies request-shape compatibility, not encrypted
reasoning continuity. Multi-turn behavior continues to use the existing
provider translation, response replay, and input-item mechanisms. Documentation
and tests must not imply stronger semantics.

## Provider-health boundaries

The compatibility acceptance criteria stop at the provider adapter dispatch
boundary unless a provider is available during an optional live smoke test.
These observations remain separate follow-ups:

- Auggie: quota-exceeded response text despite HTTP 200.
- Claude: authentication failure after passing the gate.
- DeepSeek: no-include request timed out after 40 seconds.
- Kimi: the default credential artifact is absent and the running gateway has
  neither `KIMI_CODE_HOME` nor `KIMI_BEARER_TOKEN`.
- Codex-direct: accepted the field but returned an empty output list.

No credential creation, quota purchase, or provider-account mutation belongs to
this Northstar.

## Acceptance criteria

1. Non-streaming and streaming requests with the exact sentinel reach the
   adapter for Auggie, Claude, DeepSeek, and Kimi instead of returning the
   include-specific 400.
2. The exact sentinel is absent from the normalized request observed by those
   translated adapters.
3. Copilot still accepts the exact sentinel without an include-specific 400,
   and the normalized adapter request omits it as it does today.
4. Codex-direct route behavior remains unchanged, `/codex/v1/responses` remains
   excluded, and no `codex-direct` capability-table row is introduced.
5. Unsupported or mixed `include` values still return the canonical structured
   400 before adapter invocation for Auggie, Claude, DeepSeek, and Kimi.
6. Copilot non-exact include values retain their existing generic-include gate
   and normalization behavior.
7. Requests without `include` and requests with an empty list retain current
   behavior.
8. Non-list `include` values receive no new behavior and remain governed by the
   existing request parsing contract.
9. Tests explicitly separate compatibility-gate success from provider-health
   success so auth, quota, credential, and timeout failures cannot create false
   compatibility regressions.
10. The parity surface and architecture documentation state the exact-value
   exception and its no-op semantics for translated providers.
11. The `ProviderAdapter` protocol and provider-specific credential handling are
   unchanged.
12. Targeted feature-policy and Responses integration tests pass, followed by
   the complete unit and integration suites.

## Governed Northstar reconciliation

Before product implementation begins, a repo-owned command must publish this
Northstar deterministically:

```sh
uv run python scripts/reconcile_northstar_handoff.py \
  --root . \
  --spec docs/specifications/ACTIVE/codex-encrypted-content-include-compatibility.md \
  --work-item .ai/work-intake/codex-encrypted-content-include-compatibility.md \
  --slug codex-encrypted-content-include-compatibility
```

The command owns one recoverable transaction over the workflow manifest,
traceability graph, and handoff Markdown. It must:

1. Validate every input and build all target documents before writing.
2. Upsert, rather than append blindly, the manifest branch plus exact issue,
   plan, and handoff nodes.
3. Reconcile `issue -> plan` with relation `planned-by` and
   `plan -> handoff` with relation `summarized-by`, plus plan-to-issue and
   handoff-to-plan backlinks, without duplicates.
4. Write a handoff with required YAML frontmatter containing `title`, `status`,
   and `slug`.
5. Replace files from temporary siblings in a documented fixed order.
6. Converge byte-for-byte when rerun after success or after injected failure at
   any replacement boundary.
7. Fail closed on slug, path, node-type, edge, or conflicting-ID mismatches.

The exact issue node is
`issue:reverso-root:codex-encrypted-content-include-compatibility`. The plan and
handoff node IDs use the same slug with the existing `northstar-` prefix. Node
statuses are `ready-for-agent`, `active`, and `active`, respectively. The
workflow-manifest record is `available` until Autobahn completion.

## References

- Codex 0.145.0 `client.rs`, lines 865-873.
- Codex 0.144.1 `client.rs`, lines 873-878.
- `src/reverso/protocols/feature_policy.py`
- `src/reverso/protocols/responses_app.py`
- `src/reverso/middleware/codex_responses_normalizer.py`
- `src/reverso/protocols/data/responses_parity_surface.json`
- `docs/architecture/codex-responses-parity-matrix.md`
