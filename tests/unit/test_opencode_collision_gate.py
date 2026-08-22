"""OCG-G6: which gate catches which kind of drift.

Writing these tests corrected a wrong assumption worth recording. A NEW backend
claiming an id OpenCode publishes does NOT raise the G1 conflict error: config
rows and static seeds are claimed first, and the catalog-owning seed then DEFERS
to whatever is already in the index. Incumbency winning is ADR 0020 working as
designed, but the consequence is that the id moves to the new claimant silently,
changing which subscription and whose bill serves that model.

So the two gates divide the work:

* G1's ModelIndexConflictError catches two INDEX-CLAIMING backends colliding, and
  is the only case that can fail at import time.
* G3's exposure artifact --check catches an id LEAVING OpenCode's bare set, which
  is exactly the silent-transfer case above.

Asserting both here is what makes the pair complete rather than assumed.
"""

from __future__ import annotations

import pytest

from reverso.opencode_exposure import (
    ExposureDriftError,
    compute_exposure,
    load_catalog_ids,
    verify_artifact,
)
from reverso.protocols import surface_registry
from reverso.protocols.surface_registry import (
    ModelIndexConflictError,
    _build_model_index,
)
from reverso.opencode_catalog_artifact import repo_root


def test_two_index_claiming_backends_collide_and_name_both(monkeypatch) -> None:
    """kimi claiming a deepseek config id is a genuine two-claimant collision."""
    monkeypatch.setattr(
        surface_registry, "_KIMI_MODELS", frozenset({"deepseek-reasoner"})
    )
    with pytest.raises(ModelIndexConflictError) as excinfo:
        _build_model_index()
    message = str(excinfo.value)
    assert "deepseek-reasoner" in message
    assert "deepseek" in message
    assert "kimi" in message
    # A fail-closed guard that does not say how to proceed is a dead end.
    assert "provider-qualified" in message


def test_a_new_claimant_over_an_opencode_id_defers_rather_than_colliding(
    monkeypatch,
) -> None:
    """Documents the asymmetry: this is a silent transfer, not an import failure."""
    monkeypatch.setattr(surface_registry, "_KIMI_MODELS", frozenset({"glm-5"}))
    index = _build_model_index()
    assert index["glm-5"] == "kimi"


def test_the_exposure_check_is_what_catches_that_transfer(monkeypatch) -> None:
    """The other half of the pair: an id leaving the bare set fails closed."""
    monkeypatch.setattr(surface_registry, "_KIMI_MODELS", frozenset({"glm-5"}))
    drifted = compute_exposure(
        catalog_ids=load_catalog_ids(), model_index=_build_model_index()
    )
    with pytest.raises(ExposureDriftError):
        verify_artifact(repo_root(), drifted)


def test_the_real_backend_set_builds_clean() -> None:
    """Proves the guards are not vacuous: the shipped config has no collision even
    though OpenCode's catalog deliberately overlaps two incumbents."""
    index = _build_model_index()
    assert index["kimi-k3"] == "kimi"
    assert index["glm-5"] == "opencode"


def test_the_deliberate_overlaps_are_deferrals_not_collisions() -> None:
    index = _build_model_index()
    for model_id, incumbent in (
        ("deepseek-v4-flash", "deepseek"),
        ("deepseek-v4-pro", "deepseek"),
        ("kimi-k3", "kimi"),
    ):
        assert index[model_id] == incumbent
