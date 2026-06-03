"""Documentation regression tests for Reverso installation guidance."""
from __future__ import annotations

from pathlib import Path


def test_readme_documents_codex_gpt_profile_mapping_contract() -> None:
    text = Path("README.md").read_text()

    expected = [
        "MiniMax-M3",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "Do not put provider model ids in Codex profile files.",
        "Direct Codex /v1",
    ]
    for needle in expected:
        assert needle in text


def test_readme_profile_examples_do_not_use_provider_model_ids_as_codex_models() -> None:
    text = Path("README.md").read_text()

    forbidden_examples = [
        'model = "MiniMax-M3"',
        'model = "deepseek-v4-pro"',
        'model = "deepseek-v4-flash"',
        'model = "claude-opus-4-8"',
        'model = "claude-sonnet-4-6"',
        'model = "claude-haiku-4-6"',
    ]
    for needle in forbidden_examples:
        assert needle not in text
