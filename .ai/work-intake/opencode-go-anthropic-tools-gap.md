---
title: "OpenCode Go: 11 of 22 models reject Anthropic-format tools on /messages"
status: ready-for-agent
state: ready-for-agent
category: defect
slug: opencode-go-anthropic-tools-gap
owner: unassigned
execution_type: AFK
---

# OpenCode Go: the native Anthropic path is unsafe for tool-bearing requests

Found by the OCG-G7 attended proof. Filed rather than fixed, per that goal's
contract.

## Summary

`OpenCodeAdapter` serves the Anthropic-native facet for every id outside the
declared deny-list, which today is exactly `{grok-4.5}`. That deny-list was
measured WITHOUT tools. With tools present, **11 of the 22 `/messages`-capable
ids return `400 invalid_request_error`**.

Claude Code sends tools on essentially every real turn, so those 11 ids are
effectively broken on the Anthropic surface while appearing healthy in the picker
and in a no-tools smoke test.

## Reproduction

```bash
export OPENCODE_API_KEY=...   # or the read-only OCGO_API_KEY alias
python3 scripts/opencode-live-proof.py --models glm-5
```

Observed: `responses glm-5 200 tools=1` and `anthropic_messages glm-5 502`.

Directly against upstream, isolating the tools field:

```bash
curl -sS -A reverso/1.0 -X POST https://opencode.ai/zen/go/v1/messages \
  -H 'Content-Type: application/json' -H "X-API-Key: $OPENCODE_API_KEY" \
  -H 'Anthropic-Version: 2023-06-01' \
  -d '{"model":"glm-5","max_tokens":128,
       "messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}],
       "tools":[{"name":"get_weather","description":"w",
                 "input_schema":{"type":"object",
                                 "properties":{"city":{"type":"string"}},
                                 "required":["city"]}}]}'
```

`400` with tools, `200` without. The same model accepts the same tool
declaration on `/chat/completions`, which is why the Responses surface works.

## The measured split

Rejecting tools on `/messages`: `glm-5`, `glm-5.1`, `glm-5.2`, `glm-5.3`, `hy3`,
`kimi-k2.5`, `kimi-k2.6`, `kimi-k2.7-code`, `mimo-v2.5`, `mimo-v2.5-pro`,
`ox-alpha-free`.

Accepting tools: `deepseek-v4-flash-vision-exp`, `gpt-5.6-luna`, `kimi-k3`,
`minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.5-plus`, `qwen3.6-plus`,
`qwen3.7-max`, `qwen3.7-plus`, `qwen3.8-max`.

The interesting part is that tool support and `output_config` support are almost
perfectly ANTI-correlated: 9 ids accept tools and reject `output_config`, 9 do
the reverse, 2 accept both, 2 accept neither. That is two different upstream
translator implementations behind one gateway endpoint, not a single strictness
rule, so neither capability can be inferred from the other.

Full data: `docs/reference/opencode-go-proof.json`.

## Suggested fix

Make the native-path decision TOOL AWARE rather than model-only:
`_serves_natively` should return False when the payload carries `tools` and the
model is not in the measured tools-supporting set. The fallback already exists
and already works: `grok-4.5` takes it today, and tools demonstrably succeed for
these ids on `/chat/completions`.

This keeps every id reachable on the Anthropic surface while routing each request
over the transport that can actually carry it.

## Why this was missed earlier

The G3 endpoint measurement and the G5 normalization measurement both probed with
a bare text message. Endpoint reachability is not the same property as feature
support on that endpoint, and only an end-to-end tool-bearing turn distinguished
them. Worth remembering for the next provider: probe the request shape the client
actually sends.

## Acceptance criteria

- [ ] A tool-bearing Anthropic request for a tools-rejecting id routes over chat-completions and completes.
- [ ] A tool-bearing request for a tools-supporting id still takes the native path.
- [ ] A tool-free request is unaffected for both groups.
- [ ] The tools-supporting set is a declared, measured constant with the same provenance discipline as the endpoint deny-list.
- [ ] The live proof shows both surfaces returning a parsed tool call for a tools-rejecting id.
