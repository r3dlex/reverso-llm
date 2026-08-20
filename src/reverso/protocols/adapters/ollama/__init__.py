"""Composition-owned Ollama Responses runtime."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from reverso.protocols.store import ResponseStore

from .adapter import OllamaAdapter
from .auth import OllamaAuthState
from .catalog import OllamaCatalog
from .responses import OllamaResponsesClient

OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


def validate_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint.rstrip("/"))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Ollama endpoint must be a plain loopback HTTP origin")
    return endpoint.rstrip("/")


@dataclass
class OllamaRuntime:
    client: httpx.AsyncClient
    catalog: OllamaCatalog
    auth: OllamaAuthState
    responses_client: OllamaResponsesClient
    adapter: OllamaAdapter
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.client.aclose()


def _close_owned_client(client: httpx.AsyncClient) -> None:
    """Synchronously finish failure-path cleanup before propagating an error."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.aclose())
        return

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, client.aclose()).result()


def build_ollama_runtime(
    endpoint: str = OLLAMA_ENDPOINT,
    *,
    client: httpx.AsyncClient | None = None,
    auth: OllamaAuthState | None = None,
) -> OllamaRuntime:
    endpoint = validate_endpoint(endpoint)
    auth_state = auth or OllamaAuthState.from_env()
    store = ResponseStore()
    owned_client = client or httpx.AsyncClient(timeout=300.0)
    try:
        catalog = OllamaCatalog(owned_client, endpoint, auth_state)
        responses_client = OllamaResponsesClient(owned_client, endpoint)
        adapter = OllamaAdapter(catalog, responses_client, store)
        return OllamaRuntime(
            owned_client,
            catalog,
            auth_state,
            responses_client,
            adapter,
        )
    except BaseException:
        if client is None:
            _close_owned_client(owned_client)
        raise


__all__ = ["OllamaRuntime", "build_ollama_runtime"]
