// This file defines archgate code quality rules for reverso.
// See https://github.com/r3dlex/ai-sdlc-init for schema documentation.

import { defineRules } from "ai-sdlc-rules";

export const rules = defineRules({
  backend: [
    {
      id: "no-silent-except",
      severity: "warn",
      description: "Bare except clauses must not pass silently.",
      pattern: "except:\\s*pass",
      message: "Use 'except SomeError:' with explicit handling, or log and re-raise.",
    },
    {
      id: "no-async-without-await",
      severity: "warn",
      description: "Async functions must contain at least one await or async-with call.",
      pattern: "async def [a-z_]+\\([^)]*\\):\\s*\\n\\s*(?!await|async with|return await)",
      message: "Async functions should actually await something; consider sync def instead.",
    },
  ],
  frontend: [
    // No frontend rules — reverso is a Python backend gateway with no UI.
  ],
  data: [
    // No data-domain rules — reverso is a stateless proxy, not a data platform.
  ],
  general: [
    {
      id: "no-raw-credentials",
      severity: "error",
      description: "No hardcoded credentials or secrets in source code.",
      pattern: "(password|api_key|secret|token)\\s*=\\s*['\"][^'\"]{8,}['\"]",
      message: "Credentials must come from env vars or config files only.",
    },
  ],
  architecture: [
    {
      id: "no-direct-config-mutation",
      severity: "error",
      description: "Config objects must not be mutated after load.",
      pattern: "self\\.config\\.[a-z_]+\\s*=\\s*[^=]",
      message: "Config is immutable after load; pass new values explicitly.",
    },
  ],
});
