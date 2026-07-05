"""Live-pinning integration test for the Codex Copilot picker.

ADR 0011 and commit 4507019 added Claude/Gemini serving on Copilot's
``/chat/completions`` route. The ``codex_sync`` filter
(``model_exposure.codex_responses_compatible_model_ids``) was widened to
include both routes, so ``claude-fable-5`` (and the other ten chat-route
models) must appear in the regenerated ``copilot.json`` picker.

This test pins the picker against the LIVE gateway at ``/copilot/v1/models``
to catch future drift if the filter ever narrows again. It is gated by
``RUN_LIVE_PICKER=1`` so it does not run in default CI; run it locally
after any change to the filter or to the copilot adapter to confirm the
picker is still complete.

When the gateway is unreachable the test is skipped, so a dead gateway
on a developer machine does not produce a false red.

Reference: docs/specifications/ACTIVE/copilot-picker-completeness.md (S2).
"""

from __future__ import annotations

import os

import httpx
import pytest

from reverso.protocols.model_exposure import codex_responses_compatible_model_ids

GATEWAY_BASE = os.environ.get("REVERSO_GATEWAY_BASE", "http://127.0.0.1:64946")
COPILOT_MODELS_URL = f"{GATEWAY_BASE}/copilot/v1/models"

REGRESSION_ANCHOR = "claude-fable-5"
CHAT_ROUTE_FAMILIES: tuple[str, ...] = ("claude-", "gemini-")


def _fetch_live_ids() -> list[str]:
    """Return the live ``/copilot/v1/models`` ids, or skip on any failure."""
    try:
        response = httpx.get(
            COPILOT_MODELS_URL,
            headers={
                "x-api-key": "reverso-local-loopback",
                "anthropic-version": "2023-06-01",
            },
            timeout=5.0,
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        pytest.skip(f"live gateway unreachable at {GATEWAY_BASE}: {exc}")
        raise  # unreachable; satisfies type checkers

    payload = response.json()
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        pytest.skip(f"live gateway returned no models: {payload!r}")

    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id", row)
        if isinstance(mid, str) and mid:
            ids.append(mid)
    if not ids:
        pytest.skip(f"live gateway returned empty model list: {payload!r}")
    return ids


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PICKER") != "1",
    reason="set RUN_LIVE_PICKER=1 to run the live-pinning picker test",
)
def test_copilot_picker_keeps_chat_route_models() -> None:
    """The Copilot picker must keep every chat-route model Copilot serves.

    Regression for the gpt-only filter that dropped 11 of 30 live Copilot
    models including ``claude-fable-5`` (the user's current-session model
    at the time of the fix). If this test fails, the chat-route regression
    has recurred and ``copilot.json`` will again be missing chat-route
    models in the Codex ``/model`` picker.
    """
    live_ids = _fetch_live_ids()
    kept = codex_responses_compatible_model_ids("copilot", tuple(live_ids))

    # 1. The regression anchor must be in the picker.
    assert REGRESSION_ANCHOR in kept, (
        f"{REGRESSION_ANCHOR!r} dropped from the Copilot picker. "
        f"The chat-route regression has recurred. "
        f"Live /copilot/v1/models: {sorted(live_ids)!r}, kept: {sorted(kept)!r}"
    )

    # 2. At least one model per known chat-route family must survive the
    #    filter; otherwise an entire family is gone from the picker.
    for family in CHAT_ROUTE_FAMILIES:
        family_in_kept = [m for m in kept if m.startswith(family)]
        assert family_in_kept, (
            f"chat-route family {family}* entirely dropped from the Copilot picker. "
            f"Live /copilot/v1/models: {sorted(live_ids)!r}, kept: {sorted(kept)!r}"
        )


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PICKER") != "1",
    reason="set RUN_LIVE_PICKER=1 to run the live-pinning picker test",
)
def test_copilot_picker_keeps_every_live_chat_route_model() -> None:
    """The Copilot picker must keep EVERY live chat-route model, not just one.

    Stronger form of the family-coverage check: for each id the live
    gateway returns whose route is ``chat`` (per ``copilot_model_route``),
    the picker must keep it. Catches partial regressions where one family
    is partly dropped.
    """
    from reverso.protocols.copilot_models import copilot_model_route

    live_ids = _fetch_live_ids()
    kept = set(codex_responses_compatible_model_ids("copilot", tuple(live_ids)))

    missing: list[str] = []
    for mid in live_ids:
        if copilot_model_route(mid) == "chat" and mid not in kept:
            missing.append(mid)

    assert not missing, (
        f"{len(missing)} live chat-route model(s) dropped from the Copilot picker: "
        f"{sorted(missing)!r}. Live /copilot/v1/models: {sorted(live_ids)!r}, "
        f"kept: {sorted(kept)!r}"
    )
