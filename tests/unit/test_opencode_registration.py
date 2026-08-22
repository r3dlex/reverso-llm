"""OCG-G4: where the OpenCode Go adapter is, and is not, reachable.

The negative half matters as much as the positive. G4 ships the Responses
vertical only; the Anthropic surface lands in G5. Asserting the absence keeps a
half-built Messages path from being reachable just because the adapter exists.
"""

from __future__ import annotations

from reverso.protocols.adapter import ProviderAdapter
from reverso.protocols.adapters.opencode.adapter import OpenCodeAdapter
from reverso.protocols.headroom_compression import normalize_headroom_provider
from reverso.proxy.compose import build_adapters


def test_registered_on_the_responses_gateway() -> None:
    adapters = build_adapters(env={})
    assert isinstance(adapters["opencode"], OpenCodeAdapter)
    assert isinstance(adapters["opencode"], ProviderAdapter)


def test_registration_does_not_disturb_the_existing_backends() -> None:
    adapters = build_adapters(env={})
    for prefix in ("claude", "copilot", "auggie", "deepseek", "kimi"):
        assert prefix in adapters


def test_headroom_has_a_real_provider_dimension() -> None:
    """Without this the provider collapses into 'other' and its Headroom
    behaviour becomes unattributable in the metrics."""
    assert normalize_headroom_provider("opencode") == "opencode"


def test_reachable_on_the_anthropic_surface() -> None:
    """G4 asserted the NEGATIVE here, deliberately: the Messages vertical was
    G5's job, and the negative proof kept a half-built path from becoming
    reachable early. G5 landed it, so the assertion is inverted rather than
    deleted -- the surface is still pinned, just to its new state."""
    from reverso.protocols.anthropic_app import build_anthropic_adapters

    assert "opencode" in build_anthropic_adapters()


def test_the_responses_gateway_accepts_the_prefix() -> None:
    """The gateway fails closed on unknown prefixes, so registering an adapter
    without allowlisting its prefix breaks boot for every provider, not just
    this one."""
    from reverso.protocols.responses_app import (
        APP_PROVIDER_PREFIXES,
        ResponsesGatewayApp,
    )

    assert "opencode" in APP_PROVIDER_PREFIXES
    ResponsesGatewayApp(build_adapters(env={}))


def test_the_prefix_routes_to_a_v1_responses_path() -> None:
    from reverso.protocols.responses_app import split_provider_path

    routed = split_provider_path("/opencode/v1/responses")
    assert routed is not None
    assert routed.provider == "opencode"
    assert routed.path == "/v1/responses"
