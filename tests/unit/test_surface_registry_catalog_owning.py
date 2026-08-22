"""OCG-G2: the catalog-owning backend kind (ADR 0020).

ADR 0008 gave the surface two backend kinds. A ROWLESS backend (copilot, auggie)
owns no taxonomy, so its prefix is authoritative for any bare id. A ROWS-OWNING
backend (codex, deepseek, claude, kimi) must name a model indexed to itself, so a
qualified id pointing at another backend's model fails closed.

Neither can express a backend that has a DISCOVERABLE catalog which OVERLAPS the
incumbents: seeding the catalog for bare routing makes it rows-owning, at which
point the overlapping ids become unreachable even when qualified; not seeding it
leaves it rowless, which forbids bare routing altogether.

These tests pin the third kind against a synthetic fixture backend rather than a
real provider, so they assert the rule and not any shipped catalog.
"""

from __future__ import annotations

import pytest

from reverso.protocols import surface_registry
from reverso.protocols.surface_registry import resolve_anthropic_backend

FIXTURE = "fixtureprov"


@pytest.fixture()
def catalog_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a catalog-owning fixture backend on the Anthropic surface.

    Its catalog deliberately straddles the boundary: ``kimi-k3`` is indexed to the
    kimi backend, ``deepseek-v4-pro`` to deepseek, and ``fixture-only-model`` is
    claimed by nobody. That is exactly the shape of a real overlapping catalog.
    """
    catalog = frozenset({"kimi-k3", "deepseek-v4-pro", "fixture-only-model"})
    monkeypatch.setitem(
        surface_registry.SURFACE_BACKENDS,
        "anthropic",
        surface_registry.SURFACE_BACKENDS["anthropic"] | {FIXTURE},
    )
    monkeypatch.setattr(
        surface_registry, "_CATALOG_OWNING_BACKENDS", {FIXTURE: catalog}, raising=False
    )
    # Bare routing is granted by the index, so rebuild it with the fixture present.
    monkeypatch.setattr(
        surface_registry, "_MODEL_INDEX", surface_registry._build_model_index()
    )


def test_qualified_id_reaches_the_catalog_owner_over_an_incumbent(
    catalog_backend: None,
) -> None:
    """The prefix wins for a catalog id another backend owns bare.

    Without this, the overlapping half of a real catalog would be unreachable: the
    whole point of the qualifier is naming which subscription you meant.
    """
    assert resolve_anthropic_backend(f"{FIXTURE}/kimi-k3") == FIXTURE
    assert resolve_anthropic_backend(f"{FIXTURE}/deepseek-v4-pro") == FIXTURE


def test_qualified_id_outside_the_declared_catalog_fails_closed(
    catalog_backend: None,
) -> None:
    """The prefix is authoritative for the catalog, not for anything at all.

    This is the difference from a rowless backend, whose prefix trusts any bare id.
    A catalog owner knows what it serves, so an unknown id must not fail open.
    """
    assert resolve_anthropic_backend(f"{FIXTURE}/totally-made-up") is None
    assert resolve_anthropic_backend(f"{FIXTURE}/") is None


def test_bare_id_unique_to_the_catalog_owner_resolves_to_it(
    catalog_backend: None,
) -> None:
    """An id nobody else claims is reachable without the prefix."""
    assert resolve_anthropic_backend("fixture-only-model") == FIXTURE


def test_incumbency_wins_for_a_contested_bare_id(catalog_backend: None) -> None:
    """A bare id an incumbent already serves keeps resolving to the incumbent.

    The catalog owner never takes a bare id from an established backend: bare
    routing is a convenience layered on the qualified id, never an override. A
    regression here silently moves traffic to another subscription and bill.
    """
    assert resolve_anthropic_backend("kimi-k3") == "kimi"
    assert resolve_anthropic_backend("deepseek-v4-pro") == "deepseek"


def test_registering_a_catalog_owner_does_not_trip_the_g1_conflict_guard(
    catalog_backend: None,
) -> None:
    """Deference is not a conflict.

    OCG-G1 made a cross-backend claim fatal. A catalog owner declining a contested
    id is a deliberate deference, so the index must build rather than raise.
    """
    index = surface_registry._build_model_index()
    assert index["kimi-k3"] == "kimi"
    assert index["fixture-only-model"] == FIXTURE


def test_claude_family_handling_is_untouched(catalog_backend: None) -> None:
    """The new kind grants no new route to the claude family.

    Note what is NOT asserted: there is no whole-id claude veto. ADR 0009 replaced
    the fail-closed claude marker with index resolution, and a prefix naming a
    claude model is legitimate for a provider that genuinely serves them (ADR 0011,
    copilot). So the guarantee here is narrower and real: a claude id absent from a
    catalog owner's declared catalog is refused because it is outside the catalog,
    bare claude ids still reach the claude backend, and the claude prefix still
    fails closed on a model it does not own.
    """
    assert resolve_anthropic_backend(f"{FIXTURE}/claude-sonnet-4-6") is None
    assert resolve_anthropic_backend("claude-sonnet-4-6") == "claude"
    assert resolve_anthropic_backend("claude/anything") is None


def test_existing_kinds_are_unchanged(catalog_backend: None) -> None:
    """Rowless and rows-owning resolution behave exactly as ADR 0008 specifies."""
    # rowless: prefix authoritative for an id indexed elsewhere
    assert resolve_anthropic_backend("copilot/gpt-5.5") == "copilot"
    # rows-owning: naming another backend's model is a conflict
    assert resolve_anthropic_backend("deepseek/gpt-5.5") is None
    # rows-owning: naming its own model resolves
    assert resolve_anthropic_backend("deepseek/deepseek-v4-pro") == "deepseek"


def test_opencode_is_the_registered_catalog_owner() -> None:
    """G2 shipped this mechanism empty; OCG-G5 registered its first consumer.

    Runs without the fixture so it observes the shipped configuration. The
    incumbency half is the part worth keeping from the original assertion: a
    registered catalog owner must not have taken a contested bare id.
    """
    assert set(surface_registry._CATALOG_OWNING_BACKENDS) == {"opencode"}
    assert resolve_anthropic_backend("kimi-k3") == "kimi"
    assert resolve_anthropic_backend("opencode/kimi-k3") == "opencode"
