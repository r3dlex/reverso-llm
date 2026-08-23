---
title: "claude-opencode launcher cannot size the context window per model"
status: ready-for-agent
state: ready-for-agent
category: enhancement
slug: opencode-per-model-context-window
owner: unassigned
execution_type: AFK
---

# The claude-opencode launcher window is a safe floor, not the real limit

Known limitation from OCG-G6, filed so it is tracked rather than remembered.

## Current behaviour

`claude-opencode` sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` to `202752`, the MINIMUM context window in the
OpenCode Go catalog. The catalog actually spans 202752 to 1050000, so a session on
`gpt-5.6-luna` compacts at roughly a fifth of the context it really has.

The minimum was chosen deliberately: compacting early wastes tokens and is
recoverable, whereas a window larger than the model's real context is a hard
failure mid-session. Given one static value, the floor is the only safe choice.

## Why it is static

A launcher is rendered ONCE, by `_render_launcher`, and cannot know which of the
29 models the user will select at runtime. Unlike `claude-kimi`, this backend is
multi-model, so it deliberately pins no `ANTHROPIC_MODEL`; pinning one id would
make the other 28 unreachable from its own launcher.

Note the contrast with the Codex side: `reverso_codex_profile_spec` DOES size the
window exactly, because a Codex profile pins one model. The asymmetry is real and
not an oversight.

## What is unknown, and must be established first

The obvious fix is to publish a per-model context window in the Anthropic
`GET /v1/models` listing and let the client size itself. **Whether Claude Code
consumes a context window from gateway model discovery is NOT established.**

Two reasons not to just add the field:

1. The listing deliberately mirrors the real Anthropic Models API shape
   (`type`, `id`, `display_name`, `created_at`), which has no context-window
   field. Adding a nonstandard key changes a contract that other code and tests
   pin.
2. If the client ignores it, the change is inert while looking like a fix, which
   is worse than the documented floor.

This exact pattern, assuming a capability rather than measuring it, produced three
wrong conclusions during the OpenCode Go work (string versus block `content`, the
seven `ocgo` strips, and tool-free versus tool-bearing probes). It should be
measured before it is built.

## Suggested first step

Determine empirically whether Claude Code reads a per-model window from
`GET /v1/models` under `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`: publish a
distinctive window for one model, run an attended session, and observe where
auto-compact triggers.

If it does read it, publish the windows already available in
`opencode/metadata.py` and drop the static floor. If it does not, the floor is the
correct answer and this record should be closed as won't-fix with that finding
written down.

## Acceptance criteria

- [ ] Whether the client consumes a discovery-published context window is established by observation, not assumption.
- [ ] If it does, per-model windows are published and the static floor is removed.
- [ ] If it does not, this is closed as won't-fix with the evidence recorded.
- [ ] The Anthropic listing contract is either left unchanged or changed deliberately, with its pinning tests updated.
