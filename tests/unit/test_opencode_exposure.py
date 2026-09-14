"""OCG-G3: the bare-exposure artifact and its fail-closed --check.

Under ADR 0020 a catalog-owning backend defers every contested bare id to the
incumbent and reaches it only through its own prefix. Which ids are contested is
derived from the live routing index, so it CHANGES whenever another backend adds
a model. That is precisely why it is committed as an artifact and policed: a
silent shift in the contested set would silently change which ids a user can
reach bare.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reverso.opencode_exposure import (
    ARTIFACT_RELATIVE_PATH,
    ExposureDriftError,
    compute_exposure,
    load_catalog_ids,
    load_model_index,
    main,
    render_artifact,
    verify_artifact,
)


def test_contested_ids_are_deferred_not_claimed() -> None:
    exposure = compute_exposure(
        catalog_ids=("kimi-k3", "glm-5"),
        model_index={"kimi-k3": "kimi"},
    )
    assert exposure["qualified_only"] == {"kimi-k3": "kimi"}
    assert exposure["bare_exposed"] == ["glm-5"]


def test_uncontested_ids_are_bare_exposed() -> None:
    exposure = compute_exposure(catalog_ids=("glm-5", "hy3"), model_index={})
    assert exposure["bare_exposed"] == ["glm-5", "hy3"]
    assert exposure["qualified_only"] == {}


def test_every_catalog_id_is_accounted_for() -> None:
    """No id may be dropped: bare + qualified must partition the catalog."""
    ids = ("a", "b", "c", "d")
    exposure = compute_exposure(catalog_ids=ids, model_index={"b": "kimi", "d": "x"})
    assert set(exposure["bare_exposed"]) | set(exposure["qualified_only"]) == set(ids)
    assert not set(exposure["bare_exposed"]) & set(exposure["qualified_only"])


def test_an_id_claimed_by_opencode_itself_is_not_contested() -> None:
    """Re-running after seeding must not reclassify our own ids as contested."""
    exposure = compute_exposure(
        catalog_ids=("glm-5",), model_index={"glm-5": "opencode"}
    )
    assert exposure["bare_exposed"] == ["glm-5"]
    assert exposure["qualified_only"] == {}


def test_exposure_is_deterministic_and_sorted() -> None:
    a = compute_exposure(catalog_ids=("z", "a", "m"), model_index={})
    b = compute_exposure(catalog_ids=("m", "z", "a"), model_index={})
    assert a == b
    assert a["bare_exposed"] == ["a", "m", "z"]


def test_verify_accepts_a_matching_artifact(tmp_path) -> None:
    exposure = compute_exposure(catalog_ids=("glm-5",), model_index={})
    path = tmp_path / ARTIFACT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_artifact(exposure), encoding="utf-8")
    verify_artifact(tmp_path, exposure)


def test_verify_fails_closed_on_drift(tmp_path) -> None:
    """The falsifiability proof: reintroduce a collision and --check must fail."""
    committed = compute_exposure(catalog_ids=("kimi-k3",), model_index={})
    path = tmp_path / ARTIFACT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_artifact(committed), encoding="utf-8")

    contested = compute_exposure(
        catalog_ids=("kimi-k3",), model_index={"kimi-k3": "kimi"}
    )
    with pytest.raises(ExposureDriftError):
        verify_artifact(tmp_path, contested)


def test_verify_fails_closed_when_the_artifact_is_missing(tmp_path) -> None:
    with pytest.raises(ExposureDriftError):
        verify_artifact(tmp_path, compute_exposure(catalog_ids=(), model_index={}))


def test_verify_fails_closed_on_unparseable_artifact(tmp_path) -> None:
    path = tmp_path / ARTIFACT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ExposureDriftError):
        verify_artifact(tmp_path, compute_exposure(catalog_ids=(), model_index={}))


def test_artifact_is_newline_terminated_json(tmp_path) -> None:
    rendered = render_artifact(compute_exposure(catalog_ids=("glm-5",), model_index={}))
    assert rendered.endswith("\n")
    assert json.loads(rendered)["bare_exposed"] == ["glm-5"]


def test_artifact_never_contains_a_credential(tmp_path) -> None:
    rendered = render_artifact(compute_exposure(catalog_ids=("glm-5",), model_index={}))
    assert "sk-" not in rendered


def test_main_check_returns_nonzero_on_drift(tmp_path, monkeypatch) -> None:
    """The CLI contract: --check exits non-zero rather than rewriting."""
    monkeypatch.setattr(
        "reverso.opencode_exposure.load_catalog_ids", lambda: ("kimi-k3",)
    )
    monkeypatch.setattr(
        "reverso.opencode_exposure.load_model_index", lambda: {"kimi-k3": "kimi"}
    )
    path = tmp_path / ARTIFACT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_artifact(compute_exposure(catalog_ids=("kimi-k3",), model_index={})),
        encoding="utf-8",
    )
    assert main(repo_root=tmp_path, argv=["--check"]) != 0


def test_main_write_then_check_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "reverso.opencode_exposure.load_catalog_ids", lambda: ("glm-5", "kimi-k3")
    )
    monkeypatch.setattr(
        "reverso.opencode_exposure.load_model_index", lambda: {"kimi-k3": "kimi"}
    )
    assert main(repo_root=tmp_path, argv=["--write"]) == 0
    assert main(repo_root=tmp_path, argv=["--check"]) == 0


def test_committed_artifact_is_current() -> None:
    """CI wiring: the repo's own artifact must match the live routing index.

    This is what makes the artifact a gate rather than a snapshot. Without it,
    --check would only ever run when someone remembered to type it.
    """
    repo_root = Path(__file__).resolve().parents[2]
    exposure = compute_exposure(
        catalog_ids=load_catalog_ids(), model_index=load_model_index()
    )
    verify_artifact(repo_root, exposure)


def test_the_three_contested_ids_are_the_measured_ones() -> None:
    """Pins the ADR 0020 deferral actually observed on 2026-08-22."""
    exposure = compute_exposure(
        catalog_ids=load_catalog_ids(), model_index=load_model_index()
    )
    assert exposure["qualified_only"] == {
        "deepseek-v4-flash": "deepseek",
        "deepseek-v4-pro": "deepseek",
        "kimi-k3": "kimi",
    }
    assert len(exposure["bare_exposed"]) == 26
