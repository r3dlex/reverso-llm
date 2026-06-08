"""Documentation regression tests for Reverso installation guidance."""

from __future__ import annotations

from pathlib import Path


def test_readme_documents_reverso_profiles_and_direct_minimax() -> None:
    text = Path("README.md").read_text()

    expected = [
        "MiniMax is direct Codex-only",
        'model_provider = "minimax"',
        'model = "MiniMax-M3"',
        "model_context_window = 512000",
        "MINIMAX_ANTHROPIC_API_KEY",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "Reverso profile files keep GPT-level model names",
    ]
    for needle in expected:
        assert needle in text


def test_readme_reverso_profile_examples_do_not_use_provider_model_ids_as_codex_models() -> (
    None
):
    text = Path("README.md").read_text()

    forbidden_reverso_examples = [
        'model_provider = "reverso_deepseek"\nmodel = "deepseek-v4-pro"',
        'model_provider = "reverso_deepseek"\nmodel = "deepseek-v4-flash"',
        'model_provider = "reverso_claude"\nmodel = "claude-opus-4-8"',
        'model_provider = "reverso_claude"\nmodel = "claude-sonnet-4-6"',
        'model_provider = "reverso_claude"\nmodel = "claude-haiku-4-6"',
    ]
    for needle in forbidden_reverso_examples:
        assert needle not in text
