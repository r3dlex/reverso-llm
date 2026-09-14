"""OCG-G1: a bare model id claimed by two backends must fail closed.

``_build_model_index`` maps ``{bare_id: backend}`` with a plain assignment, so
before this guard a duplicate id was resolved by insertion order and the loser was
discarded with no signal. That matters beyond tidiness: ``_BACKENDS_WITH_ROWS`` is
derived from ``frozenset(_MODEL_INDEX.values())``, so a silent overwrite also moves
a backend between the rows-owning and rowless branches of ``_resolve_qualified``
(ADR 0008) and changes how every qualified id for that backend resolves.

The tests drive the builder through its ``path`` seam with synthetic config rows
rather than editing the real config, so they assert the rule and never the
repository's current model inventory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reverso.protocols import surface_registry


def _write_config(tmp_path: Path, model_names: list[str]) -> Path:
    """Write a minimal litellm_config carrying ``model_names`` and return its path."""
    path = tmp_path / "litellm_config.yaml"
    rows = [
        {"model_name": name, "litellm_params": {"model": name}} for name in model_names
    ]
    path.write_text(json.dumps({"model_list": rows}), encoding="utf-8")
    return path


def test_distinct_ids_build_clean(tmp_path: Path) -> None:
    """Two ids owned by different backends are not a conflict."""
    config = _write_config(tmp_path, ["deepseek-v4-pro", "claude-sonnet-4-6"])
    index = surface_registry._build_model_index(config)
    assert index["deepseek-v4-pro"] == "deepseek"
    assert index["claude-sonnet-4-6"] == "claude"


def test_same_id_same_backend_is_accepted(tmp_path: Path) -> None:
    """An identical re-declaration is harmless; only a cross-backend claim is fatal.

    Ordinary config duplication must not become an outage.
    """
    config = _write_config(tmp_path, ["deepseek-v4-pro", "deepseek-v4-pro"])
    index = surface_registry._build_model_index(config)
    assert index["deepseek-v4-pro"] == "deepseek"


def test_cross_backend_collision_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare id claimed by two backends raises, naming the id and both backends.

    The kimi seed loop runs after the config rows, so pointing it at an id a config
    row already resolved to deepseek reproduces exactly the shape the OpenCode Go
    catalog would introduce. Before the guard the seed silently overwrote the row.
    """
    config = _write_config(tmp_path, ["deepseek-collide-1"])
    monkeypatch.setattr(
        surface_registry, "_KIMI_MODELS", frozenset({"deepseek-collide-1"})
    )
    with pytest.raises(surface_registry.ModelIndexConflictError) as excinfo:
        surface_registry._build_model_index(config)
    message = str(excinfo.value)
    assert "deepseek-collide-1" in message
    assert "deepseek" in message
    assert "kimi" in message


def test_real_backend_set_builds_clean() -> None:
    """The shipped configuration is collision-free, so the guard is not vacuous.

    Without this, a guard that rejected everything -- or a builder that never ran --
    would pass the negative test above and still be useless.
    """
    index = surface_registry._build_model_index()
    assert index, "the real index must be non-empty"
    assert index.get("kimi-k3") == "kimi"
    assert index.get("deepseek-v4-pro") == "deepseek"
