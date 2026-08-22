"""OpenRouter HTTP transport (OR-G2, I1, U6).

The transport owns one ``httpx.Client`` and per-call credential resolution. It
forwards Responses requests to ``POST /api/v1/responses`` and listing requests
to ``GET /api/v1/models`` after stripping caller-controlled ``store`` and
``previous_response_id`` fields. The transport never logs the key or the body.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

__all__ = [
    "HttpOpenRouterTransport",
    "OpenRouterTransportError",
]


class OpenRouterTransportError(RuntimeError):
    """An OpenRouter upstream call failed. Never carries the credential."""


class _HttpxClient(Protocol):
    def __enter__(self) -> "_HttpxClient": ...
    def __exit__(self, *args: Any) -> bool: ...
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class HttpOpenRouterTransport:
    """Direct ``httpx`` transport that strips Responses state before dispatch."""

    def __init__(
        self,
        *,
        api_base: str,
        client_factory: Callable[[], _HttpxClient] | None = None,
        credentials: Any,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._client_factory = client_factory or _default_client_factory
        self._credentials = credentials

    @property
    def api_base(self) -> str:
        return self._api_base

    # --- Adapter-facing surface ---------------------------------------------- #

    async def list_models(self) -> tuple[int, dict[str, Any]]:
        response = self._request("GET", "/api/v1/models")
        return response.status_code, _safe_json(response)

    async def create_response(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        body = _strip_stateful_fields(payload)
        response = self._request("POST", "/api/v1/responses", json=body)
        return response.status_code, _safe_json(response)

    # --- Internals -------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        api_key = self._credentials.resolve_api_key()
        headers.setdefault("Authorization", f"Bearer {api_key}")
        headers.setdefault("X-OpenRouter-Title", "Reverso")
        if method != "GET":
            headers.setdefault("Content-Type", "application/json")
        url = f"{self._api_base}{path}"
        with self._client_factory() as client:
            return client.request(method, url, headers=headers, **kwargs)


def _default_client_factory() -> _HttpxClient:
    import httpx

    return httpx.Client(timeout=60.0)


def _safe_json(response: Any) -> dict[str, Any]:
    payload = getattr(response, "payload", None)
    if isinstance(payload, dict) and payload:
        return payload
    raw = getattr(response, "text", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strip_stateful_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``previous_response_id`` and ``store`` so the stateless endpoint sees no state."""
    forbidden = {"previous_response_id", "store"}
    return {key: value for key, value in payload.items() if key not in forbidden}
