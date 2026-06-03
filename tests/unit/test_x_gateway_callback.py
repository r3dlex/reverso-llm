"""Unit tests for x_gateway response envelope callback."""
from __future__ import annotations

from types import SimpleNamespace

from reverso.middleware.x_gateway_callback import success_callback


def test_success_callback_adds_deepseek_envelope() -> None:
    response = SimpleNamespace(model="custom_openai/deepseek-v4-pro")

    success_callback({}, response, None, None)

    assert response.x_gateway == {
        "session_id": None,
        "observations": [],
        "provider": "deepseek",
        "warnings": [],
    }


def test_success_callback_still_accepts_bare_model_names() -> None:
    response = SimpleNamespace(model="deepseek-v4-flash")

    success_callback({}, response, None, None)

    assert response.x_gateway["provider"] == "deepseek"
