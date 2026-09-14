---
title: OR-G0 red-green evidence
type: tdd-evidence
status: complete
created: 2026-08-22
scope: reverso
slug: openrouter-reverso-provider-or-g0
goal: .ai/work-intake/openrouter-reverso-provider-or-g0.md
spec: docs/specifications/ACTIVE/openrouter-reverso-provider.md
test_spec: .omx/plans/test-spec-openrouter-reverso-provider.md
focused_command: uv run pytest tests/unit/test_openrouter_fixture_contract.py -q
verdict: GO
---

# OR-G0 red-green evidence

## Focused command

```bash
uv run pytest tests/unit/test_openrouter_fixture_contract.py -q
```

Equivalent in the worktree (when `uv` is not on PATH):

```bash
.venv/bin/python -m pytest tests/unit/test_openrouter_fixture_contract.py -q
```

## Red phase

Before any spike capture, the test produced 14 failures because no fixtures,
manifest, or `docs/spike-notes.md` existed yet. The first failure was:

```text
E   AssertionError: OR-G0 fixture manifest is missing at tests/fixtures/openrouter/manifest.json
E    +  where False = is_file()
E     +    where is_file = PosixPath('/Users/.../tests/fixtures/openrouter/manifest.json').is_file
```

Subsequent failures covered missing required fixtures, missing envelope keys,
missing redaction contract, missing endpoint-binding proof, missing capability
carrier proof, missing Claude alias grammar, missing Responses replay grammar,
missing spike notes verdict, and missing probe-budget field.

## Green phase

After the attended spike:

```text
..............                                                           [100%]
14 passed in 0.02s
```

All 14 contract checks pass:

- `test_manifest_present_and_schema_valid`
- `test_required_fixtures_present[list_models_authenticated]`
- `test_required_fixtures_present[responses_unary]`
- `test_required_fixtures_present[responses_streaming]`
- `test_required_fixtures_present[messages_unary]`
- `test_required_fixtures_present[messages_streaming]`
- `test_fixture_envelopes_use_canonical_shape`
- `test_fixtures_are_redacted`
- `test_endpoint_binding_proof_recorded`
- `test_capability_carriers_recorded`
- `test_claude_alias_grammar_decided`
- `test_responses_replay_grammar_decided`
- `test_spike_notes_publish_explicit_verdict`
- `test_probe_budget_isolated_from_fixtures`

## Probe provenance

- Five captures were recorded under `scripts/spike/captures/` by
  `scripts/spike/openrouter_probe.py`. The script sources
  `OPENROUTER_API_KEY` from `$HOME/.zsh_exports` via a subshell `printf`; the
  key never appears in any captured file, command argument, log line, or
  fixture.
- Five normalized fixtures and the canonical manifest live under
  `tests/fixtures/openrouter/`.
- A redaction sweep across the fixture and capture directories confirmed zero
  leaks of `sk-or-v1-...` secrets, `cap_...` capability handles, and
  `/Users/...` or `~/...` local paths.

## Probe budget

- Approved ceiling: US$0.10.
- Cumulative reported spend: US$0.00 (catalog reports zero prompt and zero
  completion prices; OR-G1 must revalidate before every dispatch).
- All five captures returned HTTP 200 with `stealth/ox-alpha` echoed in the
  response body.

## Decision

OR-G0 verdict is GO. The architecture in
`docs/specifications/ACTIVE/openrouter-reverso-provider.md` is admitted to
implementation. OR-G1 may proceed.
