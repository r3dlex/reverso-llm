"""LiteLLM success callback: inject x_gateway envelope into every response.

Registered programmatically by reverso.proxy.bootstrap before LiteLLM starts.
LiteLLM calls this after each successful completion; we attach the x_gateway
dict (session_id, observations, provider, warnings) to the response object's
_hidden_params so downstream clients can read it from the JSON body.

For HTTP-forwarded DeepSeek models the custom providers do not
set _hidden_params["x_gateway"], so we synthesise a minimal envelope here
using the model name to infer the provider string.
"""

from __future__ import annotations

from typing import Any


_MODEL_TO_PROVIDER: dict[str, str] = {
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-6": "anthropic",
    "claude-opus": "anthropic",
    "claude-sonnet": "anthropic",
    "claude-haiku": "anthropic",
    "claude": "anthropic",
    "gpt-5.5": "openai",
    "gpt-5.4": "openai",
    "gpt-5.4-mini": "openai",
    "gpt-5.3-codex-spark": "openai",
    "gpt-4.1": "openai",
    "deepseek-v4-pro": "deepseek",
    "deepseek-v4-flash": "deepseek",
    "deepseek-reasoner": "deepseek",
    "deepseek-chat": "deepseek",
}


def _normalise_model(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _infer_provider(model: str) -> str:
    normalised = _normalise_model(model)
    provider = _MODEL_TO_PROVIDER.get(normalised)
    if provider is not None:
        return provider
    lowered = normalised.lower()
    if lowered.startswith("claude-"):
        return "anthropic"
    if lowered.startswith("gpt-"):
        return "openai"
    if lowered.startswith("deepseek-"):
        return "deepseek"
    return "unknown"


def success_callback(
    kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
) -> None:
    """Attach x_gateway envelope to the response hidden params.

    Called by LiteLLM after every successful completion.
    """
    if not hasattr(response_obj, "_hidden_params"):
        response_obj._hidden_params = {}

    if "x_gateway" not in response_obj._hidden_params:
        # HTTP-forwarded model - build a minimal envelope
        model = getattr(response_obj, "model", "") or ""
        response_obj._hidden_params["x_gateway"] = {
            "session_id": None,
            "observations": [],
            "provider": _infer_provider(model),
            "warnings": [],
        }

    # Populate observations from daemon response metadata if the custom provider
    # set them on _hidden_params (Phase 2+ daemon path).
    x_gw = response_obj._hidden_params["x_gateway"]
    if "observations" not in x_gw:
        x_gw["observations"] = []

    # Propagate any warnings set by the provider (e.g. daemon_unavailable).
    if "warnings" not in x_gw:
        x_gw["warnings"] = []

    # Expose x_gateway at the top level of the response so it survives
    # LiteLLM's JSON serialisation.  LiteLLM copies _hidden_params into the
    # response dict when it serialises via model_dump(); we also set it as an
    # attribute so custom code reading the object directly sees it.
    try:
        response_obj.x_gateway = x_gw
    except Exception:
        pass
