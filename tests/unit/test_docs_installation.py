"""Documentation regression tests for Reverso installation guidance."""

from __future__ import annotations

import json
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


def test_kimi_claude_code_guide_is_provider_pinned_and_reversible() -> None:
    text = Path("docs/claude-code-kimi.md").read_text()

    expected = [
        "http://127.0.0.1:64946/kimi",
        "http://127.0.0.1:64946/kimi/v1/messages",
        "REVERSO_KIMI_MODEL",
        "kimi-k2.5",
        "model_discovery_source",
        "scripts/claude-kimi.sh",
        "Rollback",
        "does not write",
        "reverso-claude-code-sync",
    ]
    for needle in expected:
        assert needle in text

    assert "~/.claude/settings.json" in text


def test_kimi_release_runbook_covers_install_live_proof_and_rollback() -> None:
    text = Path("docs/kimi-release-runbook.md").read_text()

    expected = [
        "http://127.0.0.1:64946",
        "./scripts/install-launchagents.sh",
        "launchctl kickstart -k",
        "REVERSO_KIMI_LIVE_PROOF=1",
        "REVERSO_CODEX_CONFIG",
        "REVERSO_CODEX_CATALOG_DIR",
        "$HOME/.codex-reverso/auth.json",
        "chmod 600",
        "trap cleanup_codex_home EXIT",
        "trap restore_launchagents EXIT",
        "scripts/kimi-live-proof.py",
        ".omx/evidence/kimi-live-proof.json",
        "uv run reverso-codex-sync --dry-run",
        "Rollback",
        "com.user.reverso-proxy.plist",
        "com.user.reverso-daemon.plist",
        "scripts/claude-kimi.sh",
    ]
    for needle in expected:
        assert needle in text

    assert "KIMI_BEARER_TOKEN=" not in text


def test_kimi_traceability_chain_has_no_dangling_links() -> None:
    graph = json.loads(Path(".ai/traceability/graph.json").read_text())
    index = Path(".ai/traceability/index.md").read_text()
    report = Path(".ai/traceability/validation-report.md").read_text()
    nodes = {node["id"]: node for node in graph["nodes"]}
    required = {
        "issue:reverso-root:kimi-subscription-provider",
        "spec:reverso-root:kimi-subscription-provider",
        "adr:reverso-root:kimi-code-oauth-provider",
        "handoff:reverso-root:kimi-provider-implementation",
        "plan:reverso-root:northstar-kimi-subscription-provider",
        "handoff:reverso-root:northstar-kimi-subscription-provider",
        "pr:reverso-root:86",
    }

    assert required <= nodes.keys()
    for edge in graph["edges"]:
        assert edge["source"] in nodes
        assert edge["target"] in nodes
    for node in nodes.values():
        assert set(node.get("backlinks", ())) <= nodes.keys()
        assert f"`{node['id']}`" in index

    assert f"node_count: `{len(nodes)}`" in report
    assert f"edge_count: `{len(graph['edges'])}`" in report

    relations = {
        (edge["source"], edge["target"], edge["relation"]) for edge in graph["edges"]
    }
    assert (
        "issue:reverso-root:kimi-subscription-provider",
        "spec:reverso-root:kimi-subscription-provider",
        "defined-by",
    ) in relations
    assert (
        "adr:reverso-root:kimi-code-oauth-provider",
        "spec:reverso-root:kimi-subscription-provider",
        "constrains",
    ) in relations
    assert (
        "plan:reverso-root:northstar-kimi-subscription-provider",
        "handoff:reverso-root:northstar-kimi-subscription-provider",
        "summarized-by",
    ) in relations
    assert (
        "handoff:reverso-root:northstar-kimi-subscription-provider",
        "pr:reverso-root:86",
        "implemented-by",
    ) in relations
