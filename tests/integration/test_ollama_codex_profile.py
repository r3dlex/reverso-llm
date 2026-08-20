from __future__ import annotations

import json
import tomllib
from pathlib import Path

from reverso import codex_sync


def test_local_and_cloud_raw_ids_generate_isolated_ollama_profile(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    models = codex_sync.ProviderModels(
        "ollama", ("qwen3:8b", "deepseek-v3.1:671b-cloud")
    )

    codex_sync.sync(
        target=config,
        prefixes=("ollama",),
        fetcher=lambda _prefix: list(models.models),
        base_url="http://127.0.0.1:64946",
        lock_path=tmp_path / "sync.lock",
    )

    profile = tomllib.loads((tmp_path / "reverso-ollama.config.toml").read_text())
    assert profile["model"] == "qwen3:8b"
    assert profile["model_provider"] == "reverso_ollama"
    gateway = tomllib.loads(config.read_text())["model_providers"]["reverso_ollama"]
    assert gateway["base_url"] == "http://127.0.0.1:64946/ollama/v1"
    catalog = json.loads((tmp_path / "reverso/ollama.json").read_text())
    assert [row["slug"] for row in catalog["models"]] == [
        "qwen3:8b",
        "deepseek-v3.1:671b-cloud",
    ]
    assert all(not row["slug"].startswith("ollama/") for row in catalog["models"])
    assert all(row["input_modalities"] == ["text"] for row in catalog["models"])
    assert all(
        row["supports_parallel_tool_calls"] is False for row in catalog["models"]
    )
    assert all(row["context_window"] == 2048 for row in catalog["models"])
    assert all(row["max_context_window"] == 2048 for row in catalog["models"])
