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


def test_readme_documents_prompt_retention_boundary() -> None:
    text = Path("README.md").read_text()

    assert (
        "Prompt content may be retained in process memory for response chaining" in text
    )
    assert (
        "does not intentionally persist prompt or compressed text to disk or metrics"
        in text
    )


def test_readme_development_commands_use_supported_dev_and_prek_paths() -> None:
    text = Path("README.md").read_text()
    development = text.split("## Development and community", 1)[1].split(
        "## License", 1
    )[0]

    assert "uv sync --extra dev" in development
    assert "uvx prek run --all-files" in development
    assert "uv run ruff" not in development


def test_readme_preserves_codex_selector_invariants() -> None:
    text = Path("README.md").read_text()
    selector_rules = text.split("## Codex model selector rules", 1)[1].split(
        "## Managed configuration and safety", 1
    )[0]

    expected = [
        "Built-in Codex GPT model IDs remain bare and selectable",
        'top-level `model = "gpt-5.5"` only when the user has no top-level `model`',
        "`copilot/<model>`, `auggie/<model>`, and `agy/<model>`",
        "MiniMax, DeepSeek, GPT from Codex, and Claude from Claude Code are not prefixed",
    ]
    for needle in expected:
        assert needle in selector_rules


def test_readme_install_and_update_use_unified_client_convergence() -> None:
    text = Path("README.md").read_text()
    install = text.split("### Install and start", 1)[1].split(
        "## How routing works", 1
    )[0]
    update = text.split("## Update, stop, and uninstall", 1)[1].split(
        "## Troubleshooting", 1
    )[0]

    expected = [
        "uv run reverso-client-sync dry-run --json",
        "uv run reverso-client-sync apply --json",
        "uv run reverso-client-sync verify --json",
    ]
    for command in expected:
        assert command in install
        assert command in update


def test_readme_documents_prefixed_codex_profiles_and_claude_aliases() -> None:
    text = Path("README.md").read_text()

    expected = [
        "~/.codex/reverso-claude.config.toml",
        "~/.codex/reverso-kimi.config.toml",
        "codex exec -p reverso-claude",
        "claude-reverso",
        "claude-claude",
        "claude-codex",
        "claude-copilot",
        "claude-auggie",
        "claude-deepseek",
        "claude-kimi",
        "x-reverso-model-catalog",
        "x-reverso-workspace",
    ]
    for needle in expected:
        assert needle in text


def test_readme_documents_headroom_runtime_topology_and_updates() -> None:
    text = Path("README.md").read_text()

    expected = [
        'uv tool install --python 3.13 "headroom-ai[all]"',
        "uv tool upgrade headroom-ai",
        "headroom-ai[all]==0.32.1",
        "127.0.0.1:64946",
        "127.0.0.1:58787",
        "127.0.0.1:58788",
        "127.0.0.1:58789",
        "127.0.0.1:8787",
        "HEADROOM_NET_COST_POLICY=1 headroom proxy",
        "OPENAI_TARGET_API_URL=http://127.0.0.1:64947 headroom proxy",
        "OPENAI_TARGET_API_URL=http://127.0.0.1:64946/deepseek headroom proxy",
        "--port 58787 --mode cache",
        "--port 58788 --mode token",
        "--port 58789 --mode token",
        "embedded",
        "standalone",
    ]
    for needle in expected:
        assert needle in text


def test_architecture_documents_full_headroom_extra_as_installed() -> None:
    text = Path("docs/03-architecture.md").read_text()

    assert "headroom-ai[all]==0.32.1" in text
    assert "is installed by default" in text
    assert "are not installed by default" not in text


def test_active_docs_use_product_scoped_reverso_profile_names() -> None:
    documents = [
        Path("docs/02-prd.md").read_text(),
        Path("docs/04-mvp.md").read_text(),
        Path("docs/architecture/codex-responses-parity-matrix.md").read_text(),
    ]
    combined = "\n".join(documents)

    expected = [
        "codex -p reverso-claude",
        "codex -p reverso-deepseek",
        "reverso-claude.config.toml",
        "reverso-copilot.config.toml",
        "reverso-auggie.config.toml",
        "reverso-deepseek.config.toml",
        "reverso-kimi.config.toml",
        "reverso-codex-direct.config.toml",
        "reverso-openai-pass-through.config.toml",
    ]
    for needle in expected:
        assert needle in combined

    forbidden = [
        "codex -p anthropic",
        "codex -p deepseek",
        "`claude.config.toml`",
        "`copilot.config.toml`",
        "`auggie.config.toml`",
        "`deepseek.config.toml`",
        "`kimi.config.toml`",
        "`codex-direct.config.toml`",
        "`openai-pass-through.config.toml`",
    ]
    for needle in forbidden:
        assert needle not in combined
