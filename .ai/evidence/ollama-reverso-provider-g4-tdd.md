---
title: OLLAMA-RP-G4 red-green evidence
goal: OLLAMA-RP-G4
date: 2026-08-21
---

# OLLAMA-RP-G4 red-green evidence

## Legacy-safe TDD selection

- Coverage percent: 0
- Legacy-safe TDD: true
- Reason: foreground authentication and live client subprocess safety boundary
- Readiness gate: passed for implementation-ready goal `OLLAMA-RP-G4`
- Prerequisite gate: passed because G3 is present in the branch base

## Red

Command:

```text
uv run pytest tests/integration/test_ollama_live_contract.py -q
```

Result: exit 4 because the deterministic live-contract test module did not
exist. This is the required harness gap and does not depend on credentials.

## Green

The exact targeted command exited 0 with 9 passed. The focused integration and
unit selection exited 0 with 26 passed. Repository regression evidence is 1211
unit tests passed and 547 integration tests passed with 6 skipped. Ruff, Ruff
format, prek, compileall, rules validation, and structural archgate also passed.

The attended proof then exposed a hardcoded account name in the deployment
authority. A regression selection covering installer ordering and canonical
checkout derivation first exited 1 with 2 failures. After deriving the account
home from the operating system password database, the full deployment drift
integration module exited 0 with 129 passed.

The governed checkout deployed the G4 implementation. The attended run selected
the marker-owned local model `qwen3.8:27b-mlx` and
exited 2 with the exact external prerequisite `cloud_model_required`. No model
request ran because the current marker-owned inventory contained no Cloud
candidate. The bounded evidence file was written with mode 0600. Production
deployment drift remained blocked by unrelated live Kimi discovery returning a
fallback source rather than a live source.

Independent review found that absolute paths alone did not establish executable
identity and that proof clients needed explicit tool restrictions. The repaired
preflight requires executable files, the managed Claude launcher marker, and
bounded product-specific version formats. Child environments now use a minimal
non-credential allowlist. Codex runs read-only with approvals disabled, and
Claude runs with no tools and non-interactive permissions. Regression matrices
lock every sign-in gate, unsafe evidence targets, identity substitution, and
bounded timeout behavior.

The clean governed checkout ran both production gates. Deployment drift exited
2 because unrelated live Kimi discovery returned a fallback source rather than
a live source. Convergence exited 2 because the dry run could not discover the
unrelated Codex direct provider. The exact non-integration regression command
completed with 1211 passed. These external provider failures did not weaken the
deterministic G4 gates or trigger a model request.
