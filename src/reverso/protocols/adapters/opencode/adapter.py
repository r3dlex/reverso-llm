"""OpenCode Go provider adapter, Responses/chat-completions vertical (OCG-G4).

This is the FIRST usable vertical and deliberately the Codex-facing one. The
``ProviderAdapter`` contract is Responses-shaped and this transport is
chat-completions, so no Anthropic round-trip is involved: a defect observed here
is an adapter defect rather than a translation defect. The Claude/Messages
vertical lands in G5 on top of a transport already proven in isolation.

The chat-completions transport is inherited from ``DeepSeekAdapter``, following
``KimiAdapter``'s precedent. What OpenCode Go changes is credentials, base URL,
listing metadata and quota semantics, so only those are overridden.

Two upstream facts shape the code (both measured, see the G3 spec section):

* ``/chat/completions`` requires ``Authorization: Bearer``. The sibling
  ``/messages`` path accepts ``X-API-Key`` ONLY, which is why G5 cannot simply
  reuse these headers.
* The edge rejects a default HTTP client fingerprint with Cloudflare error 1010,
  so a ``User-Agent`` is a functional requirement rather than politeness.

Quota refusals fail closed. A ``429`` raises ``OpenCodeQuotaError`` carrying the
status and body so the gateway surfaces it verbatim; it is never retried against
another credential and never rerouted to another backend. Silent fallback would
spend a different subscription and make the user's quota state unobservable.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from reverso.protocols.adapter import ModelList
from reverso.protocols.adapters.deepseek import DeepSeekAdapter
from reverso.protocols.openai_chat import parse_stream_event as _parse_stream_event
from reverso.protocols.adapters.opencode.catalog import (
    FALLBACK_MODEL_IDS,
    OPENCODE_GO_API_BASE,
    USER_AGENT,
)
from reverso.protocols.adapters.opencode.credentials import (
    OpenCodeCredentialError,
    require_api_key,
)
from reverso.protocols.adapters.opencode.metadata import limits_for

__all__ = ["OpenCodeAdapter", "OpenCodeError", "OpenCodeQuotaError"]

logger = logging.getLogger(__name__)

_FORWARD_TIMEOUT_SECONDS = 300.0
_OWNED_BY = "opencode"


class OpenCodeError(RuntimeError):
    """An OpenCode Go upstream call failed. Never carries the credential."""


class OpenCodeQuotaError(OpenCodeError):
    """Upstream refused the request for quota or rate reasons (HTTP 429).

    Subclasses ``OpenCodeError`` so a caller catching the provider's base error
    still catches quota, while a caller that cares can distinguish it.

    ``status_code`` and ``payload`` are read by the gateway's provider-error
    mapping to surface the refusal verbatim instead of collapsing it into a
    generic 502. A user who has exhausted a subscription needs to see that, not
    a vague upstream failure.
    """

    status_code = 429

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__("opencode go upstream refused the request (429)")
        self.payload = payload if isinstance(payload, dict) else {}


class OpenCodeAdapter(DeepSeekAdapter):
    """ProviderAdapter for the OpenCode Go subscription over chat-completions."""

    def __init__(
        self,
        store: Any | None = None,
        *,
        api_base: str = OPENCODE_GO_API_BASE,
        client_factory: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            store,
            api_base=api_base,
            client_factory=client_factory
            or (lambda: httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SECONDS)),
        )
        # None means "read the live process environment at call time", matching
        # DeepSeek. An explicit mapping is for tests and for proving the
        # fail-closed path without mutating global state.
        self._env = env

    def _api_key(self) -> str:
        """Resolve the key at call time. Raises rather than sending an empty one."""
        try:
            return require_api_key(self._env)
        except OpenCodeCredentialError as exc:
            raise OpenCodeError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _listing_headers(self) -> dict[str, str]:
        """Headers for ``GET /models``, which is public and needs no credential.

        Sending one would make a listing fail whenever the key is absent or
        stale, for an endpoint that never required it.
        """
        return {"Content-Type": "application/json", "User-Agent": USER_AGENT}

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = self._headers()
        try:
            async with self._client_factory() as client:
                return await client.post(
                    f"{self._api_base}/chat/completions",
                    headers=headers,
                    content=json.dumps(body).encode("utf-8"),
                )
        except httpx.HTTPError as exc:
            raise OpenCodeError("opencode go upstream request failed") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Map a non-2xx upstream status onto the provider's error taxonomy."""
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 429:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            logger.warning("opencode go upstream refused with 429")
            raise OpenCodeQuotaError(payload if isinstance(payload, dict) else {})
        # Status code only: never the headers or body, which can echo the key.
        logger.warning("opencode go upstream returned %s", response.status_code)
        raise OpenCodeError(
            f"opencode go upstream returned status {response.status_code}"
        )

    async def _call_upstream(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._post(body)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenCodeError("opencode go upstream returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise OpenCodeError("opencode go upstream returned non-object JSON")
        return payload

    async def _call_upstream_stream(
        self, body: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """POST a streaming chat-completions call; yield parsed chunk dicts.

        Overridden rather than inherited for one reason: the inherited transport
        raises the DEEPSEEK error type on a non-2xx. A quota refusal on the
        streaming path (the path Codex actually uses) would surface as a generic
        502 attributed to the wrong provider, with the 429 dropped. Inheriting a
        transport must not mean inheriting a provider identity.

        The status check happens before any SSE byte is read, so a refusal is a
        clean pre-emission failure the gateway can still turn into a structured
        response.
        """
        headers = self._headers()
        try:
            async with (
                self._client_factory() as client,
                client.stream(
                    "POST",
                    f"{self._api_base}/chat/completions",
                    headers=headers,
                    content=json.dumps(body).encode("utf-8"),
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    # Body must be read before .json() on a streamed response.
                    if response.status_code == 429:
                        await response.aread()
                    self._raise_for_status(response)
                pending = b""
                async for raw in response.aiter_bytes():
                    if not raw:
                        continue
                    pending += raw
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = line.strip()
                        if not line or not line.startswith(b"data:"):
                            continue
                        payload = line[len(b"data:") :].strip()
                        if not payload:
                            continue
                        if payload == b"[DONE]":
                            yield {
                                "text": "",
                                "reasoning_text": "",
                                "tool_calls": [],
                                "usage": None,
                                "done": True,
                            }
                            return
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        parsed = _parse_stream_event(event)
                        if parsed is not None:
                            yield parsed
                            if parsed.get("done"):
                                return
        except OpenCodeError:
            raise
        except httpx.HTTPError as exc:
            logger.warning(
                "opencode go streaming transport error: %s", type(exc).__name__
            )
            raise OpenCodeError("opencode go streaming transport error") from exc

    def _model_row(self, model_id: str, created: int) -> dict[str, Any]:
        """Build one listing row, enriched with limits when they are known."""
        row: dict[str, Any] = {
            "id": model_id,
            "object": "model",
            "created": created,
            "owned_by": _OWNED_BY,
        }
        limits = limits_for(model_id)
        if limits is not None:
            # Codex sizes its context management from these fields.
            row["context_window"] = limits.context
            row["max_output_tokens"] = limits.output
        return row

    async def list_models(self) -> ModelList:
        """Return the live catalog, degrading to the bounded offline snapshot.

        Discovery is unauthenticated by design. ``discovery_source`` reports
        which path answered so a stale picker is diagnosable rather than
        indistinguishable from a live one.
        """
        created = int(time.time())
        try:
            async with self._client_factory() as client:
                response = await client.get(
                    f"{self._api_base}/models", headers=self._listing_headers()
                )
            if 200 <= response.status_code < 300:
                from reverso.protocols.adapters.opencode.catalog import parse_model_ids

                model_ids = parse_model_ids(response.json())
                if model_ids:
                    return ModelList(
                        data=[self._model_row(mid, created) for mid in model_ids],
                        discovery_source="live",
                    )
            logger.warning(
                "opencode go model listing returned %s; serving bounded fallback",
                response.status_code,
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "opencode go model listing failed (%s); serving bounded fallback",
                type(exc).__name__,
            )
        return ModelList(
            data=[self._model_row(mid, created) for mid in FALLBACK_MODEL_IDS],
            discovery_source="fallback",
        )
