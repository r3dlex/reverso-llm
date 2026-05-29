"""LiteLLM success callback: inject x_gateway envelope into every response.

Registered in litellm_config.yaml under litellm_settings.success_callback.
LiteLLM calls this after each successful completion; we attach the x_gateway
dict (session_id, observations, provider, warnings) to the response object's
_hidden_params so downstream clients can read it from the JSON body.

For HTTP-forwarded models (DeepSeek, MiniMax) the custom providers do not
set _hidden_params["x_gateway"], so we synthesise a minimal envelope here
using the model name to infer the provider string.
"""
from __future__ import annotations

from typing import Any


_MODEL_TO_PROVIDER: dict[str, str] = {
    "deepseek-reasoner": "deepseek",
    "deepseek-chat": "deepseek",
    "MiniMax-M2.7-highspeed": "minimax",
    "minimax-fast": "minimax",
    "MiniMax-M2.7": "minimax",
    "minimax": "minimax",
}


def _infer_provider(model: str) -> str:
    return _MODEL_TO_PROVIDER.get(model, "unknown")


def success_callback(kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
    """Attach x_gateway envelope to the response hidden params.

    Called by LiteLLM after every successful completion.
    """
    if not hasattr(response_obj, "_hidden_params"):
        response_obj._hidden_params = {}

    if "x_gateway" not in response_obj._hidden_params:
        # HTTP-forwarded model – build a minimal envelope
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
