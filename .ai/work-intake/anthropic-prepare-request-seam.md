# Work Item: Deepen anthropic_translate with prepare_anthropic_request

- **Traceability node:** `issue:reverso-root:anthropic-prepare-request-seam`
- **Spec:** [`docs/specifications/ACTIVE/anthropic-prepare-request-seam.md`](../../docs/specifications/ACTIVE/anthropic-prepare-request-seam.md)
- **State:** `in-progress`
- **Owner:** unassigned
- **Spans repos:** `r3dlex/reverso-llm` (source + tests)
- **Surface scope:** standalone (reverso only)
- **Hosted reconciliation:** none required (local-first per `init-ai-repo` host-policy).

## Summary

`anthropic_app._dispatch` orchestrates five request-preparation concerns inline
and duplicates the Anthropic -> Responses translation in both the streaming and
non-streaming handlers. The strip -> gate -> translate invariant is enforced
only by inline ordering in the app. Consolidate it into one pure function,
`anthropic_translate.prepare_anthropic_request(payload, backend)`, returning
the `ResponsesRequest` plus the stripped payload (what the adapter observes /
what recording needs). Behavior identical on both surfaces.

## Sliced goals

| Slice | Title | Type | Status |
|---|---|---|---|
| S1 | `refactor(anthropic): deepen anthropic_translate with prepare_anthropic_request seam` | code | in-progress |

## Acceptance criteria (mirrored from the spec)

1. `prepare_anthropic_request` owns strip -> gate -> translate in exact today-order, with new unit tests.
2. All existing unit tests pass unchanged.
3. `py_compile` clean over `src`.
4. `count_tokens` pre-flight path unchanged (un-stripped, un-gated).
5. Observable error ordering unchanged.
