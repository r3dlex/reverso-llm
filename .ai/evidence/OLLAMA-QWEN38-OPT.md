---
title: Ollama Qwen3.8 27B M5 optimization evidence
status: verified
date: 2026-08-27
---

# Ollama Qwen3.8 27B M5 optimization evidence

## Hardware and model

- Apple M5, 10 CPU cores, 32 GB unified memory
- Ollama server `0.32.15`
- `qwen3.8:27b-mlx`, 18 GB, MLX backend, 27.8B parameters
- Model-reported context limit: 262144 tokens

## Runtime configuration

The Homebrew Ollama LaunchAgent was backed up and configured with:

```text
OLLAMA_CONTEXT_LENGTH=8192
OLLAMA_KEEP_ALIVE=15m
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
```

The existing `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`
settings were preserved. The 8K default leaves memory for the 18 GB model,
the active Codex session, Reverso, and macOS. The model limit remains 262144
tokens; the effective daemon default is intentionally lower.

`launchctl print gui/$(id -u)/homebrew.mxcl.ollama` and the daemon process
environment both confirmed the four new variables. The daemon was restarted
through launchd and reported `{"version":"0.32.15"}`.

## Measurements

Baseline was captured before changing the runtime, with Ollama `0.32.14` and
an explicit `num_ctx=4096` request:

```text
wall=11.31s
load=6.05s
prefill=8.14 tok/s
generation=11.46 tok/s
```

After configuration, the model was unloaded, then loaded with the effective
8192-token default. A warm 32-token generation request measured:

```text
wall=2.57s
load=0.01s
prefill=34.12 tok/s
generation=17.59 tok/s
ollama ps context=8192
```

The cold post-change request measured 12.37 s total and 7.77 s load. This is a
small cold-load cost from the 0.32.15 server restart; the warm request is the
relevant interactive comparison. No implicit model pull or daemon management
was performed by Reverso.

## Reverso sync fix evidence

Before the code change, a subprocess with `CODEX_HOME=/tmp/codex-home-regression`
resolved `DEFAULT_CONFIG_PATH` to `/Users/andreburgstahler/.codex/config.toml`.
After the change, the same subprocess resolved it to
`/tmp/codex-home-regression/config.toml`.

The focused test passed:

```text
uv run --project . python -m pytest tests/unit/test_codex_sync.py -q -k default_config_path_respects_codex_home
1 passed, 137 deselected
```

The full unit file also passed:

```text
uv run --project . python -m pytest tests/unit/test_codex_sync.py -q
138 passed
```

The live `CODEX_HOME=/Users/andreburgstahler/.codex-reverso` convergence apply
updated the active Ollama profile and catalog. It returned `partial_freshness`
only because the unrelated Kimi discovery is stale; `provider-ollama` reported
`changed` and the active catalog now contains both `glm-5.2:cloud` and
`qwen3.8:27b-mlx`.

The full `tests/verify_ollama_g1.sh` wrapper passed after formatting the
Ollama-adjacent `test_feature_policy.py` change already present on this branch.
It reported `1224 passed`, all `prek` hooks green, and `167 files already
formatted`.
