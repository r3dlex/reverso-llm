"""Unit tests for LiteLLM proxy config."""

from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parents[2] / "config" / "litellm_config.yaml"
MODELS_PATH = Path(__file__).parents[2] / "config" / "models.yaml"


def _model_map(path: Path) -> dict[str, dict]:
    cfg = yaml.safe_load(path.read_text())
    return {item["model_name"]: item for item in cfg["model_list"]}


def test_litellm_uses_programmatic_extensions() -> None:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())

    assert cfg["litellm_settings"]["drop_params"] is True
    assert "custom_provider_map" not in cfg["litellm_settings"]
    assert "success_callback" not in cfg["litellm_settings"]


def test_minimax_is_not_in_reverso_litellm_config() -> None:
    models = _model_map(CONFIG_PATH)

    assert "MiniMax-M3" not in models
    assert "MiniMax-M2.7-highspeed" not in models
    assert "MiniMax-M2.7" not in models
    assert "minimax-fast" not in models
    assert "minimax" not in models


def test_minimax_is_not_in_reverso_registry() -> None:
    models = _model_map(MODELS_PATH)

    assert "MiniMax-M3" not in models
    assert "MiniMax-M2.7-highspeed" not in models
    assert "MiniMax-M2.7" not in models
    assert "minimax-fast" not in models
    assert "minimax" not in models
