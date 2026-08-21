"""G2 negative proof: generic Ollama forms have no Anthropic authority."""

from reverso.protocols.surface_registry import (
    SURFACE_BACKENDS,
    resolve_anthropic_backend,
)


def test_ollama_forms_are_not_anthropic_backends() -> None:
    assert "ollama" in SURFACE_BACKENDS["anthropic"]
    for model in (
        "qwen3:8b",
        "ollama/qwen3:8b",
        "anthropic-ollama-qwen3:8b",
    ):
        assert resolve_anthropic_backend(model) != "ollama"
