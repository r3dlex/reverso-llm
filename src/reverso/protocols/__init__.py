"""First-party Responses gateway protocol layer (ADR 0002).

This package owns the Reverso-native OpenAI Responses contract for the Claude
and Copilot provider paths. It MUST NOT import reverso.proxy.app (the legacy
LiteLLM wrapper); the quarantine guard test asserts that invariant.
"""

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ProviderAdapter,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.auth import (
    AuthResolution,
    ProviderAuth,
    fake_auth,
    redact_secret,
)
from reverso.protocols.store import ResponseStore

__all__ = [
    "AuthResolution",
    "InputItemList",
    "ModelList",
    "ProviderAdapter",
    "ProviderAuth",
    "ResponseEnvelope",
    "ResponseStore",
    "ResponsesRequest",
    "SSEEvent",
    "fake_auth",
    "redact_secret",
]
