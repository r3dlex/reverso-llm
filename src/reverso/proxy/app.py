"""ASGI app wrapper for the Reverso LiteLLM proxy."""
from __future__ import annotations

from typing import Any, cast

from reverso.proxy.bootstrap import register_litellm_extensions

register_litellm_extensions()

from litellm.proxy.proxy_server import app as litellm_app  # noqa: E402

from reverso.middleware.codex_responses_normalizer import CodexResponsesNormalizerMiddleware
from reverso.middleware.responses_sse_completion import ResponsesSSECompletionMiddleware
from reverso.middleware.responses_think_stripper import ResponsesThinkStripperMiddleware
from reverso.middleware.x_gateway_error_envelope import XGatewayErrorEnvelopeMiddleware
from reverso.proxy.profile_routing import ProfileRoutingMiddleware

app = XGatewayErrorEnvelopeMiddleware(
    CodexResponsesNormalizerMiddleware(
        ProfileRoutingMiddleware(
            ResponsesThinkStripperMiddleware(ResponsesSSECompletionMiddleware(cast(Any, litellm_app)))
        )
    )
)
