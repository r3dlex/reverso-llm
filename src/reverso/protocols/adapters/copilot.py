"""GitHub Copilot provider adapter (ADR 0002 D4).

Ported from the direct-forward spine in ../copilot-openai-api/main.py rather
than the github-copilot-sdk (which is JSON-RPC-over-CLI and fails Responses SSE
parity, ADR 0002 D4). Forwards native OpenAI /responses traffic to
api.githubcopilot.com using only the local logged-in user's GitHub Copilot
credentials (~/.config/github-copilot/{apps,hosts}.json) with auto-refresh and
no repository-stored secret.

Two must-fix omissions are honored relative to the source: the access-token log
line is dropped (tokens flow through redact_secret only) and the wildcard CORS
is not carried over (the app binds loopback-only, so broad CORS is unnecessary).
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from reverso.protocols.adapter import (
    InputItemList,
    ModelList,
    ResponseEnvelope,
    ResponsesRequest,
    SSEEvent,
)
from reverso.protocols.auth import AuthResolution, redact_secret
from reverso.protocols.copilot_models import (
    canonical_copilot_responses_model,
    is_copilot_responses_model_id,
)
from reverso.protocols.feature_policy import UnsupportedFeature
from reverso.protocols.store import ResponseStore

logger = logging.getLogger(__name__)


class CopilotUpstreamError(RuntimeError):
    """Secret-free Copilot upstream error safe to surface to callers."""

    __slots__ = ("public_message",)

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_CLI_API_BASES = (
    "https://api.business.githubcopilot.com",
    "https://api.individual.githubcopilot.com",
)
GITHUB_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_REFRESH_SKEW_SECONDS = 120
_STALE_LOCK_SECONDS = 300
_FORWARD_TIMEOUT_SECONDS = 300.0


def _github_copilot_config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path("~/AppData/Local").expanduser()
    else:
        base = Path("~/.config").expanduser()
    return base / "github-copilot"


class CopilotAuth:
    """Resolve and refresh the local GitHub Copilot bearer token.

    The OAuth token is read from the logged-in user's
    ~/.config/github-copilot/{apps,hosts}.json and exchanged for a short-lived
    Copilot bearer token, cached at token.json with a best-effort file lock so
    concurrent processes do not race the refresh. No token is ever logged.
    """

    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        enable_cli_keychain: bool | None = None,
    ) -> None:
        self._config_dir = config_dir or _github_copilot_config_dir()
        self._enable_cli_keychain = (
            config_dir is None if enable_cli_keychain is None else enable_cli_keychain
        )
        self._token_file = self._config_dir / "token.json"
        self._lock_file = Path(str(self._token_file) + ".lock")
        self._oauth_token: str | None = None
        self._copilot_token: dict | None = None
        self._api_base_override: str | None = None
        self._refresh_lock = asyncio.Lock()

    def _read_oauth_token(self) -> str:
        for name in ("apps.json", "hosts.json"):
            path = self._config_dir / name
            if not path.exists():
                continue
            hosts = json.loads(path.read_text())
            for host, data in hosts.items():
                if "github.com" in host and isinstance(data, dict):
                    token = data.get("oauth_token")
                    if token:
                        return token
        raise RuntimeError("GitHub Copilot OAuth token not found")

    def _copilot_cli_config_file(self) -> Path:
        return Path("~/.copilot/config.json").expanduser()

    def _copilot_cli_keychain_accounts(self) -> list[str]:
        config_file = self._copilot_cli_config_file()
        try:
            config = json.loads(config_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        accounts: list[str] = []

        def add_account(entry: object) -> None:
            if not isinstance(entry, dict):
                return
            host = entry.get("host")
            login = entry.get("login")
            if isinstance(host, str) and isinstance(login, str) and host and login:
                account = f"{host}:{login}"
                if account not in accounts:
                    accounts.append(account)

        add_account(config.get("last_logged_in_user"))
        users = config.get("logged_in_users")
        if isinstance(users, list):
            for user in users:
                add_account(user)
        return accounts

    def _read_copilot_cli_token(self) -> str:
        if not self._enable_cli_keychain:
            raise RuntimeError("GitHub Copilot CLI Keychain lookup disabled")
        if platform.system() != "Darwin":
            raise RuntimeError("GitHub Copilot CLI Keychain token not available")
        reasons: list[str] = []
        for account in self._copilot_cli_keychain_accounts():
            try:
                result = subprocess.run(
                    [
                        "security",
                        "find-generic-password",
                        "-s",
                        "copilot-cli",
                        "-a",
                        account,
                        "-w",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                reasons.append(f"{account}: {type(exc).__name__}")
                continue
            token = result.stdout.strip()
            if token:
                return token
        reason = "; ".join(reasons) if reasons else "no Copilot CLI users configured"
        raise RuntimeError(f"GitHub Copilot CLI Keychain token not found ({reason})")

    async def _discover_cli_api_base(self, bearer: str) -> str:
        headers = _forward_headers(bearer)
        async with httpx.AsyncClient(timeout=30.0) as client:
            for api_base in COPILOT_CLI_API_BASES:
                response = await client.get(f"{api_base}/models", headers=headers)
                if response.status_code == 200:
                    return api_base
        raise RuntimeError("Copilot CLI token did not authenticate to known API bases")

    def _load_cached_token(self) -> None:
        try:
            content = self._token_file.read_text()
        except FileNotFoundError:
            return
        if content:
            self._copilot_token = json.loads(content)

    def _save_cached_token(self) -> None:
        if self._copilot_token is None:
            return
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._copilot_token))
        tmp.replace(self._token_file)

    def _token_valid(self) -> bool:
        return bool(
            self._copilot_token
            and self._copilot_token.get("expires_at", 0)
            > time.time() + _REFRESH_SKEW_SECONDS
        )

    def _acquire_lock(self) -> bool:
        try:
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            self._lock_file.touch(exist_ok=False)
            return True
        except FileExistsError:
            return False

    def _release_lock(self) -> None:
        try:
            self._lock_file.unlink()
        except FileNotFoundError:
            pass

    def _clear_stale_lock(self) -> None:
        try:
            age = time.time() - self._lock_file.stat().st_mtime
        except FileNotFoundError:
            return
        if age > _STALE_LOCK_SECONDS:
            self._release_lock()
            logger.info("Removed stale Copilot token lock")

    async def _exchange_token(self) -> None:
        if self._oauth_token is None:
            try:
                self._oauth_token = self._read_oauth_token()
            except RuntimeError:
                token = self._read_copilot_cli_token()
                self._api_base_override = await self._discover_cli_api_base(token)
                self._copilot_token = {"token": token, "expires_at": time.time() + 3600}
                logger.info("Copilot CLI Keychain token resolved")
                return
        headers = {
            "Authorization": f"token {self._oauth_token}",
            "Accept": "application/json",
            "Editor-Plugin-Version": "copilot.lua",
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(GITHUB_TOKEN_URL, headers=headers, timeout=30.0)
        if response.status_code != 200:
            raise RuntimeError(
                f"Copilot token refresh failed with status {response.status_code}"
            )
        self._copilot_token = response.json()
        self._save_cached_token()
        logger.info("Copilot bearer token refreshed")

    async def _ensure_token(self) -> None:
        if self._token_valid():
            return
        self._load_cached_token()
        if self._token_valid():
            return
        async with self._refresh_lock:
            if self._token_valid():
                return
            self._clear_stale_lock()
            if not self._acquire_lock():
                logger.info("Another process is refreshing the Copilot token; waiting")
                await asyncio.sleep(5)
                self._load_cached_token()
                if self._token_valid():
                    return
                raise RuntimeError("Copilot token refresh in progress elsewhere")
            try:
                await self._exchange_token()
            finally:
                self._release_lock()

    def resolve(self) -> AuthResolution:
        """Resolve the local Copilot credential (non-secret summary)."""
        try:
            self._oauth_token = self._read_oauth_token()
        except (RuntimeError, json.JSONDecodeError, OSError) as oauth_exc:
            try:
                self._read_copilot_cli_token()
            except (RuntimeError, json.JSONDecodeError, OSError) as cli_exc:
                return AuthResolution(
                    authenticated=False,
                    method="copilot_oauth",
                    details={
                        "oauth_reason": str(oauth_exc),
                        "cli_reason": str(cli_exc),
                    },
                )
            return AuthResolution(
                authenticated=True,
                method="copilot_cli_keychain",
                details={"config_file": str(self._copilot_cli_config_file())},
            )
        return AuthResolution(
            authenticated=True,
            method="copilot_oauth",
            details={"config_dir": str(self._config_dir)},
        )

    async def bearer_token(self) -> str:
        """Return the current Copilot bearer token. Never log the raw value."""
        await self._ensure_token()
        if not self._copilot_token or "token" not in self._copilot_token:
            raise RuntimeError("Copilot bearer token unavailable")
        token = self._copilot_token["token"]
        logger.debug("Using Copilot bearer token %s", redact_secret(token))
        return token

    async def api_base(self, default: str) -> str:
        """Return the Copilot API base that matches the resolved credential."""
        await self._ensure_token()
        return self._api_base_override or default


def _forward_headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "copilot-developer-cli",
        "Editor-Version": "copilot-cli/1.0.21",
    }


def _safe_upstream_http_error(response: httpx.Response) -> CopilotUpstreamError:
    """Build a secret-free upstream HTTP error with actionable model detail."""
    message = f"Copilot upstream HTTP {response.status_code}"
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, httpx.ResponseNotRead, httpx.StreamConsumed):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            upstream_message = error.get("message")
            upstream_code = error.get("code")
            if isinstance(upstream_message, str) and upstream_message:
                message = upstream_message
            if isinstance(upstream_code, str) and upstream_code:
                message = f"{message} ({upstream_code})"
    return CopilotUpstreamError(message)


def _raise_for_upstream_status(response: httpx.Response) -> None:
    """Raise a secret-free error that preserves upstream model diagnostics."""
    if response.status_code >= 400:
        raise _safe_upstream_http_error(response)


def _normalize_models(payload: dict) -> ModelList:
    data = payload.get("data")
    if not isinstance(data, list):
        data = []
    normalized: list[dict] = []
    created = int(time.time())
    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not is_copilot_responses_model_id(model_id):
            continue
        normalized.append(
            {
                "id": model_id,
                "object": "model",
                "created": created,
                "owned_by": model.get("vendor", "github-copilot"),
            }
        )
    return ModelList(data=normalized, models=[])


class CopilotAdapter:
    """ProviderAdapter forwarding Responses traffic to GitHub Copilot.

    Streaming and unary /responses calls forward verbatim to
    api.githubcopilot.com; model listings are normalized to the OpenAI shape and
    the Codex-compatible refresh field. previous_response_id chaining and
    /input_items are served from the injected in-memory ResponseStore.
    """

    def __init__(
        self,
        auth: CopilotAuth | None = None,
        store: ResponseStore | None = None,
        *,
        api_base: str = COPILOT_API_BASE,
        client_factory=None,
    ) -> None:
        self._auth = auth or CopilotAuth()
        self._store = store or ResponseStore()
        self._api_base = api_base.rstrip("/")
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_SECONDS)
        )

    def _request_body(self, request: ResponsesRequest, *, stream: bool) -> bytes:
        model = canonical_copilot_responses_model(request.model) or request.model
        payload: dict = {"model": model, "input": request.input}
        if request.instructions is not None:
            payload["instructions"] = request.instructions
        if request.previous_response_id is not None:
            payload["previous_response_id"] = request.previous_response_id
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        payload.update(request.extra)
        payload["stream"] = stream
        return json.dumps(payload).encode("utf-8")

    def _check_model(self, request: ResponsesRequest) -> None:
        if not is_copilot_responses_model_id(request.model):
            raise UnsupportedFeature("copilot", f"model:{request.model}")

    async def create_response(self, request: ResponsesRequest) -> ResponseEnvelope:
        self._check_model(request)
        model = canonical_copilot_responses_model(request.model) or request.model
        bearer = await self._auth.bearer_token()
        api_base = await self._api_base_for_auth()
        body = self._request_body(request, stream=False)
        async with self._client_factory() as client:
            response = await client.post(
                f"{api_base}/responses",
                headers=_forward_headers(bearer),
                content=body,
            )
        _raise_for_upstream_status(response)
        raw = response.json()
        envelope = ResponseEnvelope(
            id=raw.get("id", ""),
            model=raw.get("model", model),
            output=raw.get("output", []) if isinstance(raw.get("output"), list) else [],
            status=raw.get("status", "completed"),
            usage=raw.get("usage"),
            previous_response_id=raw.get("previous_response_id"),
            raw=raw,
        )
        input_items = request.input if isinstance(request.input, list) else None
        if envelope.id:
            self._store.put_response(envelope, input_items=input_items)
        return envelope

    async def stream_response(self, request: ResponsesRequest):
        self._check_model(request)
        bearer = await self._auth.bearer_token()
        api_base = await self._api_base_for_auth()
        body = self._request_body(request, stream=True)
        async with self._client_factory() as client:
            async with client.stream(
                "POST",
                f"{api_base}/responses",
                headers=_forward_headers(bearer),
                content=body,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                _raise_for_upstream_status(response)
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        event = self._parse_sse_block(block)
                        if event is not None:
                            yield event
                tail = buffer.strip()
                if tail:
                    event = self._parse_sse_block(tail)
                    if event is not None:
                        yield event

    @staticmethod
    def _parse_sse_block(block: bytes) -> SSEEvent | None:
        text = block.strip()
        if not text:
            return None
        event_name = "message"
        data_lines: list[str] = []
        for line in text.decode("utf-8", "replace").splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        data_text = "\n".join(data_lines)
        parsed: dict = {}
        if data_text and data_text != "[DONE]":
            try:
                loaded = json.loads(data_text)
                if isinstance(loaded, dict):
                    parsed = loaded
                    event_name = loaded.get("type", event_name)
            except json.JSONDecodeError:
                parsed = {}
        return SSEEvent(event=event_name, data=parsed, raw=block.strip() + b"\n\n")

    async def _api_base_for_auth(self) -> str:
        api_base = getattr(self._auth, "api_base", None)
        if callable(api_base):
            return await api_base(self._api_base)
        return self._api_base

    async def list_models(self) -> ModelList:
        bearer = await self._auth.bearer_token()
        api_base = await self._api_base_for_auth()
        async with self._client_factory() as client:
            response = await client.get(
                f"{api_base}/models", headers=_forward_headers(bearer)
            )
        _raise_for_upstream_status(response)
        payload = response.json()
        return _normalize_models(payload if isinstance(payload, dict) else {})

    async def get_response(self, response_id: str) -> ResponseEnvelope:
        envelope = self._store.get_response(response_id)
        if envelope is None:
            raise KeyError(response_id)
        return envelope

    async def list_input_items(self, response_id: str) -> InputItemList:
        items = self._store.get_input_items(response_id)
        if items is None:
            return InputItemList(response_id=response_id, data=[])
        return items
