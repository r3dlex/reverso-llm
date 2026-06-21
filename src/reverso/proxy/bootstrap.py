"""LiteLLM extension bootstrap for Reverso."""

from __future__ import annotations

import litellm

from reverso.middleware.x_gateway_callback import success_callback
from reverso.proxy.anthropic_cli_provider import anthropic_cli


def register_litellm_extensions() -> None:
    """Register Reverso providers and callbacks with LiteLLM once."""
    providers = {
        "anthropic_cli": anthropic_cli,
    }
    existing = {
        item.get("provider"): item
        for item in getattr(litellm, "custom_provider_map", [])
        if isinstance(item, dict)
    }
    for provider, handler in providers.items():
        existing[provider] = {"provider": provider, "custom_handler": handler}
        if provider not in litellm.provider_list:
            litellm.provider_list.append(provider)
    litellm.custom_provider_map = list(existing.values())

    if success_callback not in litellm.success_callback:
        litellm.logging_callback_manager.add_litellm_success_callback(success_callback)
