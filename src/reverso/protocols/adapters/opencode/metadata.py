"""Per-model context windows for the OpenCode Go catalog (OCG-G4).

Codex sizes its context management from the model listing, so a missing or wrong
context window is not cosmetic: it drives when the client compacts. The values
come from the ``opencode-go`` provider block of ``models.dev/api.json``, which is
a dedicated authority for this exact catalog rather than a generic model index.

Snapshot taken 2026-08-22. It covers 28 of the 29 live ids; the sole gap is
``hy3-preview``, which is also one of the ids upstream reports as unavailable, so
nothing reachable is missing a window. A model absent from this table is served
without limit metadata rather than with a guessed one: a fabricated window would
make the client compact at the wrong point, which is worse than the client
falling back to its own default.

This is a bounded snapshot and WILL drift as OpenCode adds models. It is
deliberately not fetched at runtime: a listing whose limits depend on a third
-party endpoint being reachable would make model discovery fail for reasons
unrelated to the subscription.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CONTEXT_WINDOWS", "ModelLimits", "limits_for"]


@dataclass(frozen=True)
class ModelLimits:
    """The context and max-output token limits for one model."""

    context: int
    output: int


CONTEXT_WINDOWS: dict[str, ModelLimits] = {
    "deepseek-v4-flash": ModelLimits(context=1000000, output=384000),
    "deepseek-v4-flash-vision-exp": ModelLimits(context=1000000, output=384000),
    "deepseek-v4-pro": ModelLimits(context=1000000, output=384000),
    "glm-5": ModelLimits(context=202752, output=32768),
    "glm-5.1": ModelLimits(context=202752, output=32768),
    "glm-5.2": ModelLimits(context=1000000, output=131072),
    "glm-5.3": ModelLimits(context=1000000, output=131072),
    "gpt-5.6-luna": ModelLimits(context=1050000, output=128000),
    "grok-4.5": ModelLimits(context=500000, output=500000),
    "hy3": ModelLimits(context=256000, output=64000),
    "kimi-k2.5": ModelLimits(context=262144, output=65536),
    "kimi-k2.6": ModelLimits(context=262144, output=65536),
    "kimi-k2.7-code": ModelLimits(context=262144, output=262144),
    "kimi-k3": ModelLimits(context=1048576, output=131072),
    "mimo-v2-omni": ModelLimits(context=262144, output=128000),
    "mimo-v2-pro": ModelLimits(context=1048576, output=128000),
    "mimo-v2.5": ModelLimits(context=1000000, output=128000),
    "mimo-v2.5-pro": ModelLimits(context=1048576, output=128000),
    "minimax-m2.5": ModelLimits(context=204800, output=65536),
    "minimax-m2.7": ModelLimits(context=204800, output=131072),
    "minimax-m3": ModelLimits(context=1000000, output=131072),
    "muse-spark-1.2-contributor": ModelLimits(context=1048576, output=131072),
    "ox-alpha-free": ModelLimits(context=1000000, output=131072),
    "qwen3.5-plus": ModelLimits(context=262144, output=65536),
    "qwen3.6-plus": ModelLimits(context=1000000, output=65536),
    "qwen3.7-max": ModelLimits(context=1000000, output=65536),
    "qwen3.7-plus": ModelLimits(context=1000000, output=65536),
    "qwen3.8-max": ModelLimits(context=1000000, output=131072),
}


def limits_for(model_id: str) -> ModelLimits | None:
    """Return the limits for ``model_id``, or ``None`` when unknown.

    Returning ``None`` rather than a default is deliberate: see the module
    docstring on why a guessed window is worse than no window.
    """
    return CONTEXT_WINDOWS.get(model_id.strip())
