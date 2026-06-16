"""Provider profile model routing for Reverso.

Codex profiles should keep GPT-level model names. Reverso rewrites those
profile-local aliases to concrete provider model ids before LiteLLM routing.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]

FRONTIER_GPT_MODELS = frozenset({"gpt-5.5", "gpt-5.4"})
KNOWN_GPT_MODELS = frozenset(
    {
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "gpt-4.1",
    }
)
MODEL_ALIASES = {
    "claude-opus-4.8": "claude-opus-4-8",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-haiku-4.6": "claude-haiku-4-6",
}

PROVIDER_PREFIXES = frozenset({"deepseek", "claude"})
CURRENT_PROFILE_WORKSPACE: ContextVar[str | None] = ContextVar(
    "CURRENT_PROFILE_WORKSPACE", default=None
)


def _normalise_model(model: str) -> str:
    normalized = model.removeprefix("custom/").strip()
    return MODEL_ALIASES.get(normalized, normalized)


def resolve_profile_model(profile: str, model: str) -> str:
    """Resolve a Codex GPT-level model name for a Reverso provider profile."""
    normalized = _normalise_model(model)
    if profile == "deepseek":
        if normalized in FRONTIER_GPT_MODELS:
            return "deepseek-v4-pro"
        if normalized in KNOWN_GPT_MODELS:
            return "deepseek-v4-flash"
        return normalized
    if profile == "claude":
        if normalized in FRONTIER_GPT_MODELS:
            return "claude-opus-4-8"
        if normalized in KNOWN_GPT_MODELS:
            return "claude-sonnet-4-6"
        return normalized
    return normalized


@dataclass(frozen=True)
class ProfilePath:
    profile: str
    rewritten_path: str


def split_profile_path(path: str) -> ProfilePath | None:
    """Return provider profile and LiteLLM path for /<profile>/v1/... paths."""
    parts = path.split("/", 3)
    if len(parts) < 4:
        return None
    _, profile, version, rest = parts
    if profile not in PROVIDER_PREFIXES or version != "v1":
        return None
    return ProfilePath(profile=profile, rewritten_path=f"/v1/{rest}")


class ProfileRoutingMiddleware:
    """Rewrite profile-prefixed requests to normal LiteLLM requests.

    Example: POST /deepseek/v1/responses with {"model":"gpt-5.5"}
    becomes POST /v1/responses with {"model":"deepseek-v4-pro"}.
    MiniMax is not a Reverso profile; Codex should call MiniMax directly.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path_info = split_profile_path(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        metadata_workspace = _workspace_from_codex_turn_metadata(scope.get("headers", []))
        if path_info is None:
            direct_workspace = metadata_workspace
            if method in {"POST", "PUT", "PATCH"}:
                body = await _read_body(receive)
                if body is None:
                    await self.app(scope, _receive_disconnect(), send)
                    return
                direct_workspace = _workspace_from_body(body) or metadata_workspace
                token = CURRENT_PROFILE_WORKSPACE.set(direct_workspace)
                try:
                    await self.app(scope, _receive_replay(body, receive), send)
                finally:
                    CURRENT_PROFILE_WORKSPACE.reset(token)
                return

            token = CURRENT_PROFILE_WORKSPACE.set(direct_workspace)
            try:
                await self.app(scope, receive, send)
            finally:
                CURRENT_PROFILE_WORKSPACE.reset(token)
            return

        new_scope = dict(scope)
        new_scope["path"] = path_info.rewritten_path
        new_scope["raw_path"] = path_info.rewritten_path.encode("utf-8")
        new_scope["reverso_profile"] = path_info.profile
        new_scope["reverso_split_visible_deltas"] = True

        body = await _read_body(receive)
        if body is None:
            await self.app(new_scope, _receive_disconnect(), send)
            return

        profile_workspace: str | None = None
        if method in {"POST", "PUT", "PATCH"} and body:
            profile_workspace = _workspace_from_body(body) or metadata_workspace
            body = _rewrite_body_model(
                body,
                path_info.profile,
                profile_workspace,
            )
            new_scope["headers"] = _headers_with_content_length(
                scope.get("headers", []),
                len(body),
            )

        token = CURRENT_PROFILE_WORKSPACE.set(profile_workspace)
        try:
            await self.app(new_scope, _receive_replay(body, receive), send)
        finally:
            CURRENT_PROFILE_WORKSPACE.reset(token)


async def _read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            return None
        if message_type != "http.request":
            return b"".join(chunks)
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _workspace_from_codex_turn_metadata(
    headers: list[tuple[bytes, bytes]],
) -> str | None:
    metadata_raw: str | None = None
    for key, value in headers:
        if key.lower() == b"x-codex-turn-metadata":
            metadata_raw = value.decode("utf-8", "replace")
            break
    if not metadata_raw:
        return None
    return _workspace_from_codex_metadata_json(metadata_raw)


def _workspace_from_codex_metadata_json(metadata_raw: str) -> str | None:
    try:
        metadata = json.loads(metadata_raw)
    except json.JSONDecodeError:
        return None
    return _workspace_from_codex_metadata(metadata)


def _workspace_from_codex_metadata(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    workspaces = metadata.get("workspaces")
    if not isinstance(workspaces, dict):
        return None
    candidates = [
        (path, info)
        for path, info in workspaces.items()
        if isinstance(path, str) and path.strip()
    ]
    if not candidates:
        return None
    for path, info in candidates:
        if isinstance(info, dict) and (
            info.get("associated_remote_urls")
            or info.get("latest_git_commit_hash")
            or info.get("has_changes") is not None
        ):
            return path
    return candidates[0][0]


def _with_workspace(payload: dict[str, Any], workspace: str | None) -> dict[str, Any]:
    if not workspace:
        return payload
    x_gateway = payload.get("x_gateway")
    if (
        isinstance(x_gateway, dict)
        and isinstance(x_gateway.get("workspace"), str)
        and x_gateway["workspace"].strip()
    ):
        return payload
    payload = dict(payload)
    merged = dict(x_gateway) if isinstance(x_gateway, dict) else {}
    merged["workspace"] = workspace
    payload["x_gateway"] = merged
    return payload


def _workspace_from_body(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    x_gateway = payload.get("x_gateway")
    if isinstance(x_gateway, dict):
        workspace = x_gateway.get("workspace")
        if isinstance(workspace, str) and workspace.strip():
            return workspace
    client_metadata = payload.get("client_metadata")
    if isinstance(client_metadata, dict):
        metadata_raw = client_metadata.get("x-codex-turn-metadata")
        if isinstance(metadata_raw, str) and metadata_raw.strip():
            return _workspace_from_codex_metadata_json(metadata_raw)
        workspace = _workspace_from_codex_metadata(client_metadata)
        if workspace:
            return workspace
    metadata = payload.get("metadata")
    return _workspace_from_codex_metadata(metadata)


def _rewrite_body_model(
    body: bytes, profile: str, workspace: str | None = None
) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict):
        return body
    model = payload.get("model")
    if isinstance(model, str):
        payload = dict(payload)
        if profile == "claude":
            payload = _with_workspace(payload, workspace)
        else:
            payload.pop("x_gateway", None)
        payload["model"] = resolve_profile_model(profile, model)
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return body


def _headers_with_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    filtered = [
        (key, value) for key, value in headers if key.lower() != b"content-length"
    ]
    filtered.append((b"content-length", str(length).encode("ascii")))
    return filtered


def _receive_disconnect() -> Receive:
    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    return receive


def _receive_replay(body: bytes, source_receive: Receive) -> Receive:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return await source_receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive
