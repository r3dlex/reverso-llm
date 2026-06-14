---
type: deep-interview-transcript
project: reverso
slug: auggie-deepseek-responses
status: complete
ported_from: .omx/interviews/auggie-deepseek-responses-20260608T224423Z.md
ported_at: 20260609
---

# Deep Interview Transcript: Auggie and DeepSeek Responses Providers

## Source context
- Context snapshot: `.omc/context/auggie-deepseek-responses-20260608T223519Z.md`
- Auggie SDK reference: https://pypi.org/project/auggie-sdk/
- DeepSeek reference: https://chat-deep.ai/docs/openai-sdk-to-deepseek/

## Rounds

### Initial user request
Use https://pypi.org/project/auggie-sdk/ to implement the OpenAI Responses API for Codex for Auggie. Indexing is always disabled by default. For DeepSeek, consider https://chat-deep.ai/docs/openai-sdk-to-deepseek/.

### User clarification
All providers should be different endpoint prefixes served under the same local port. Claude Code and Copilot follow the same same-port provider endpoint topology.

### Round 1
Question: For this interview, what is the first-milestone target now that providers should be endpoint prefixes on the same port?
Answer: Reverso Auggie+DeepSeek - Add Auggie and DeepSeek as same-port Reverso provider endpoints for Codex Responses.

### Round 2
Question: Which items must stay out of scope for the first Auggie+DeepSeek Reverso endpoint milestone?
Answer: no-new-port-process, no-oh-my-auggie-edits, no-claude-copilot-replan.

### Round 3
Question: For Auggie, what must indexing disabled by default mean in the first Reverso endpoint milestone?
Answer: hard-disabled.

### Round 4
Question: If `auggie-sdk` cannot prove indexing is hard-disabled, what should the first milestone do?
Answer: document and skip hard disable.

### Round 5
Question: When SDK-level hard-disable proof is missing, should Reverso still ship the Auggie endpoint with documentation, or skip the Auggie endpoint?
Answer: ship-with-caveat.

### Round 6
Question: For DeepSeek, what should the first Reverso Responses endpoint support by default?
Answer: full-deepseek-modes.

### Round 7
Question: When Codex sends Responses tool calls through Auggie or DeepSeek, what should Reverso allow in the first milestone?
Answer: provider-native behavior.

## Final ambiguity score
0.18. The interview is ready for ralplan. Remaining questions are implementation research tasks, not user intent blockers.
