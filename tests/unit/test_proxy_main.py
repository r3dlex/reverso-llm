"""Unit tests for the Reverso proxy entrypoint."""

from __future__ import annotations

import pytest

from reverso.proxy.main import _CONFIG_PATH, _resolve_host


def test_config_path_points_to_reverso_config() -> None:
    assert _CONFIG_PATH.name == "litellm_config.yaml"
    assert _CONFIG_PATH.parent.name == "config"
    assert _CONFIG_PATH.exists()


def test_resolve_host_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVERSO_HOST", raising=False)

    assert _resolve_host() == "127.0.0.1"


def test_resolve_host_rejects_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVERSO_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="non-loopback binds are forbidden"):
        _resolve_host()
