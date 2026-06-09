---
type: team-handoff
project: reverso
stage: team-plan -> team-exec
team: reverso-responses-providers
date: 2026-06-09
---

## Handoff: team-plan -> team-exec

- **Decided**: Foundation-first DAG. Lane A1 (task #1) is the frozen provider
  contract (ProviderAdapter Protocol + shared Responses types + in-memory store +
  auth interface) that Lanes B/C/D import. ADR 0002 already froze the interface
  (create_response, stream_response, list_models, get_response, list_input_items),
  so adapters build against it in parallel once A1 lands. Single loopback port
  127.0.0.1:64946 with path prefixes /claude and /copilot; copilot prefix is
  net-new and added in the first-party app, not in legacy PROVIDER_PREFIXES.
- **Rejected**: Mutating legacy src/reverso/proxy/profile_routing.py (keep legacy
  quarantined). github-copilot-sdk for Copilot (JSON-RPC-over-CLI, fails Responses
  parity per ADR 0002 D4) - use the ported direct-forward spine. Auth-by-elimination
  for Claude (defeats amendment 1) - read claudeAiOauth artifact directly.
- **Risks**: B/C import churn if A1 symbol names drift -> worker-a must report exact
  public symbol names to team-lead when #1 completes. Ported Copilot code must drop
  the token log (was main.py:276) and wildcard CORS (was main.py:284-292).
- **Files**: src/reverso/protocols/{__init__,adapter,store,auth,responses_app}.py,
  src/reverso/protocols/adapters/{claude,copilot}.py, tests/fixtures/responses/,
  tests/integration/test_responses_provider_contract.py,
  tests/unit/test_litellm_quarantine.py.
- **Remaining**: team-exec (#1-#7), then team-verify (#8). No commit/PR without
  user approval. Hard rules: loopback-only bind, no secrets in VCS/logs, uv, md
  frontmatter, no em/en dashes, never delete spec content.
