"""OCG-G6: the catalog is data, so a refresh is not a code change.

G6 requires that a model added upstream becomes reachable after a refresh with no
code change. Before this slice the declared catalog was a Python constant, which
made a new upstream model *listed* by live discovery but not *routable*: it fell
outside the declared catalog, so ADR 0020's prefix branch failed closed on it.

Moving the catalog to a committed data artifact makes a refresh a data change,
while keeping the routing index deterministic at import time.
"""

from __future__ import annotations

import json

import pytest

from reverso.opencode_catalog_artifact import (
    CATALOG_ARTIFACT_PATH,
    CatalogArtifactError,
    load_catalog_ids,
    parse_catalog,
    render_catalog,
)


def test_the_shipped_artifact_parses() -> None:
    ids = load_catalog_ids()
    assert len(ids) == 29
    assert ids == tuple(sorted(set(ids)))


def test_the_artifact_is_the_single_source_for_the_fallback() -> None:
    """Two hand-maintained copies of 29 ids would drift against each other."""
    from reverso.protocols.adapters.opencode.catalog import FALLBACK_MODEL_IDS

    assert FALLBACK_MODEL_IDS == load_catalog_ids()


def test_the_artifact_is_the_single_source_for_routing() -> None:
    from reverso.protocols.surface_registry import _CATALOG_OWNING_BACKENDS

    assert _CATALOG_OWNING_BACKENDS["opencode"] == frozenset(load_catalog_ids())


def test_parse_sorts_and_dedupes() -> None:
    assert parse_catalog({"models": ["b", "a", "b"]}) == ("a", "b")


def test_parse_skips_unusable_rows() -> None:
    assert parse_catalog({"models": ["", None, 3, "a"]}) == ("a",)


def test_parse_fails_closed_on_a_malformed_artifact() -> None:
    """An unreadable catalog must not silently become an empty one: an empty
    declared catalog makes every qualified id fail closed, which would look like
    a routing bug rather than a corrupt file."""
    for payload in ({}, {"models": "not-a-list"}, [], {"models": []}):
        with pytest.raises(CatalogArtifactError):
            parse_catalog(payload)


def test_render_round_trips() -> None:
    rendered = render_catalog(("a", "b"))
    assert rendered.endswith("\n")
    assert parse_catalog(json.loads(rendered)) == ("a", "b")


def test_artifact_path_is_committed_reference_data() -> None:
    assert CATALOG_ARTIFACT_PATH.parts[:2] == ("docs", "reference")


def test_a_new_model_becomes_routable_from_data_alone(tmp_path, monkeypatch) -> None:
    """The criterion, stated as a test: adding an id to the artifact is enough."""
    from reverso.protocols import surface_registry

    extended = (*load_catalog_ids(), "brand-new-model-1")
    monkeypatch.setattr(
        surface_registry,
        "_CATALOG_OWNING_BACKENDS",
        {"opencode": frozenset(extended)},
    )
    monkeypatch.setattr(
        surface_registry,
        "_MODEL_INDEX",
        surface_registry._build_model_index(),
    )
    assert (
        surface_registry.resolve_anthropic_backend("opencode/brand-new-model-1")
        == "opencode"
    )
