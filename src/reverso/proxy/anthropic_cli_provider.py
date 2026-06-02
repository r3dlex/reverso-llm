"""LiteLLM custom provider: Anthropic via Claude Code CLI.

Phase 1: each request spawns one `claude -p` subprocess (stateless).
Phase 2: routes through the session daemon over UDS when available, with
stateless fallback when the daemon cannot complete the turn.

Usage in litellm_config.yaml::

    model_list:
      - model_name: claude-sonnet-4-6
        litellm_params:
          model: custom/claude-sonnet-4-6
          custom_llm_provider: anthropic_cli
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx
import litellm
from litellm import ModelResponse
from litellm.llms.custom_llm import CustomLLM
from litellm.types.utils import GenericStreamingChunk

from reverso.proxy.utils import (
    call_daemon,
    daemon_available,
    daemon_sock_path,
    last_user_message,
    resolve_cli_command,
    strip_think_blocks,
)
from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

logger = logging.getLogger(__name__)

_MODEL_FLAG_MAP: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-haiku-4-6": "claude-haiku-4-6",
}
_DEFAULT_MODEL_FLAG = "claude-sonnet-4-6"


def _model_flag(litellm_model: str) -> str:
    key = litellm_model.removeprefix("custom/")
    return _MODEL_FLAG_MAP.get(key, _DEFAULT_MODEL_FLAG)


def _make_x_gateway(session_id: str | None = None) -> dict[str, Any]:
    return {"session_id": session_id, "observations": [], "provider": "anthropic", "warnings": []}


def _request_workspace(kwargs: dict[str, Any]) -> str | None:
    x_gateway = kwargs.get("x_gateway")
    if isinstance(x_gateway, dict) and isinstance(x_gateway.get("workspace"), str) and x_gateway["workspace"].strip():
        return x_gateway["workspace"]
    return CURRENT_PROFILE_WORKSPACE.get()


def _invoke_claude(prompt: str, model_flag: str, workspace: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """Spawn `claude -p` and return its parsed JSON result."""
    cwd = str(Path(workspace).expanduser().resolve()) if workspace else None
    cmd = [
        resolve_cli_command("claude", "REVERSO_CLAUDE_BIN"), "-p", prompt,
        "--output-format", "json",
        "--model", model_flag,
        "--dangerously-skip-permissions",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("claude CLI not found on PATH") from exc

    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}. stderr: {proc.stderr.strip()[:500]}")

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("claude CLI produced no output")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude CLI output is not valid JSON: {exc}\nraw: {stdout[:300]}") from exc


def _run_turn(
    prompt: str,
    model_flag: str,
    workspace: str | None,
    cli_timeout: int,
) -> tuple[str, str | None, list[dict], list[str]]:
    """Run one turn via daemon (preferred) or stateless subprocess.

    Returns (assistant_text, session_id, observations, warnings).
    """
    warnings: list[str] = []
    sock = daemon_sock_path()
    if daemon_available(sock):
        effective_ws = workspace or str(Path.home())
        try:
            resp = call_daemon(sock, effective_ws, "anthropic", prompt, model_flag, float(cli_timeout))
            return (
                strip_think_blocks(resp.get("assistant_text", "")),
                resp.get("session_id"),
                resp.get("observations", []),
                warnings,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            logger.warning("Daemon turn failed (%s); falling back to stateless mode", exc)
            warnings.append(f"daemon_unavailable: {exc}")

    result = _invoke_claude(prompt, model_flag, workspace=workspace, timeout=cli_timeout)
    return strip_think_blocks(result.get("result", "")), result.get("session_id"), [], warnings


class AnthropicCLIProvider(CustomLLM):
    """LiteLLM custom provider backed by the Claude Code CLI."""

    def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        api_base: str | None = None,
        custom_prompt_dict: dict | None = None,
        model_response: ModelResponse | None = None,
        print_verbose: Any = None,
        encoding: Any = None,
        api_key: str | None = None,
        logging_obj: Any = None,
        optional_params: dict | None = None,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        t_start = time.monotonic()
        prompt = last_user_message(messages)
        model_flag = _model_flag(model)
        cli_timeout = int(timeout) if isinstance(timeout, (int, float)) else 300
        workspace = _request_workspace(kwargs)

        assistant_text, session_id, observations, warnings = _run_turn(prompt, model_flag, workspace, cli_timeout)

        x_gateway = _make_x_gateway(session_id=session_id)
        x_gateway["observations"] = observations
        if warnings:
            x_gateway["warnings"] = warnings

        response = model_response or ModelResponse()
        setattr(response, "choices", [{"index": 0, "message": {"role": "assistant", "content": assistant_text}, "finish_reason": "stop"}])
        response.model = model
        response._hidden_params = {"x_gateway": x_gateway, "elapsed_seconds": time.monotonic() - t_start}
        return response

    def streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        api_base: str | None = None,
        custom_prompt_dict: dict | None = None,
        model_response: ModelResponse | None = None,
        print_verbose: Any = None,
        encoding: Any = None,
        api_key: str | None = None,
        logging_obj: Any = None,
        optional_params: dict | None = None,
        acompletion: Any = None,
        litellm_params: Any = None,
        logger_fn: Any = None,
        headers: dict | None = None,
        timeout: Any = None,
        client: Any = None,
        **kwargs: Any,
    ) -> Iterator[GenericStreamingChunk]:
        """Yield response as a single chunk. True streaming deferred to Phase 3."""
        prompt = last_user_message(messages)
        model_flag = _model_flag(model)
        cli_timeout = int(timeout) if isinstance(timeout, (int, float)) else 300
        workspace = _request_workspace(kwargs)

        assistant_text, _sid, _obs, _warn = _run_turn(prompt, model_flag, workspace, cli_timeout)

        yield {"text": assistant_text, "is_finished": False, "finish_reason": "", "usage": None, "index": 0}
        yield {"text": "", "is_finished": True, "finish_reason": "stop", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "index": 0}

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        import asyncio
        return await asyncio.to_thread(self.completion, *args, **kwargs)

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:  # pyright: ignore[reportIncompatibleMethodOverride]
        import asyncio

        chunks = await asyncio.to_thread(lambda: list(self.streaming(*args, **kwargs)))
        for chunk in chunks:
            yield chunk


anthropic_cli = AnthropicCLIProvider()
