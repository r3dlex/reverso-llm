"""LiteLLM custom provider: OpenAI via Codex CLI.

Phase 1: each request spawns one `codex exec` subprocess (stateless).
Phase 2: routes through the session daemon over UDS when available, with
stateless fallback when the daemon cannot complete the turn.

Spike findings (spike-notes.md):
- Invoke: `codex exec "prompt" --json -s workspace-write`
- Thread ID: first event {"type":"thread.started","thread_id":"..."}
- Assistant text: {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
- Requires git repo in cwd OR --skip-git-repo-check

Usage in litellm_config.yaml::

    model_list:
      - model_name: gpt-4.1
        litellm_params:
          model: custom/gpt-4.1
          custom_llm_provider: openai_cli
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from contextvars import copy_context
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx
from litellm import ModelResponse
from litellm.llms.custom_llm import CustomLLM
from litellm.types.utils import Choices, GenericStreamingChunk, Message

from reverso.proxy.utils import (
    call_daemon,
    stream_daemon,
    daemon_available,
    daemon_sock_path,
    last_user_message,
    resolve_cli_command,
    strip_think_blocks,
    StreamingThinkStripper,
)
from reverso.proxy.profile_routing import CURRENT_PROFILE_WORKSPACE

logger = logging.getLogger(__name__)

_MODEL_FLAG_MAP: dict[str, str] = {
    "gpt-4.1": "gpt-4.1",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.5": "gpt-5.5",
}
_DEFAULT_MODEL_FLAG = "gpt-4.1"


def _model_flag(litellm_model: str) -> str:
    key = litellm_model.removeprefix("custom/")
    return _MODEL_FLAG_MAP.get(key, _DEFAULT_MODEL_FLAG)


def _make_x_gateway(session_id: str | None = None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "observations": [],
        "provider": "openai",
        "warnings": [],
    }


def _request_workspace(kwargs: dict[str, Any]) -> str | None:
    x_gateway = kwargs.get("x_gateway")
    if (
        isinstance(x_gateway, dict)
        and isinstance(x_gateway.get("workspace"), str)
        and x_gateway["workspace"].strip()
    ):
        return x_gateway["workspace"]
    return CURRENT_PROFILE_WORKSPACE.get()


def _parse_codex_output(stdout: str) -> dict[str, Any]:
    """Parse newline-delimited JSON events from `codex exec --json`."""
    thread_id: str | None = None
    text_parts: list[str] = []
    usage: dict[str, Any] = {}

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = event.get("type")
        if t == "thread.started":
            thread_id = event.get("thread_id")
        elif t == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and (text := item.get("text", "")):
                text_parts.append(text)
        elif t == "turn.completed":
            usage = event.get("usage", {})

    return {
        "thread_id": thread_id,
        "assistant_text": "\n".join(text_parts),
        "usage": usage,
    }


def _invoke_codex(
    prompt: str, model_flag: str, workspace: str | None = None, timeout: int = 300
) -> dict[str, Any]:
    """Spawn `codex exec` and return its parsed output dict."""
    cwd = str(Path(workspace).expanduser().resolve()) if workspace else None
    cmd = [
        resolve_cli_command("codex", "REVERSO_CODEX_BIN"),
        "exec",
        prompt,
        "--json",
        "-s",
        "workspace-write",
        "--model",
        model_flag,
        "--skip-git-repo-check",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"codex CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("codex CLI not found on PATH") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"codex CLI exited {proc.returncode}. stderr: {proc.stderr.strip()[:500]}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("codex CLI produced no output")

    return _parse_codex_output(stdout)


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
            resp = call_daemon(
                sock, effective_ws, "openai", prompt, model_flag, float(cli_timeout)
            )
            return (
                strip_think_blocks(resp.get("assistant_text", "")),
                resp.get("session_id"),
                resp.get("observations", []),
                warnings,
            )
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            logger.warning(
                "Daemon turn failed (%s); falling back to stateless mode", exc
            )
            warnings.append(f"daemon_unavailable: {exc}")

    result = _invoke_codex(prompt, model_flag, workspace=workspace, timeout=cli_timeout)
    return (
        strip_think_blocks(result["assistant_text"]),
        result.get("thread_id"),
        [],
        warnings,
    )


def _run_turn_stream(
    prompt: str,
    model_flag: str,
    workspace: str | None,
    cli_timeout: int,
) -> Iterator[str]:
    """Yield visible assistant text deltas via daemon streaming when available."""
    sock = daemon_sock_path()
    if daemon_available(sock):
        effective_ws = workspace or str(Path.home())
        stripper = StreamingThinkStripper()
        try:
            for event in stream_daemon(
                sock, effective_ws, "openai", prompt, model_flag, float(cli_timeout)
            ):
                event_type = event.get("type")
                if event_type == "delta":
                    delta = stripper.strip_delta(str(event.get("delta", "")))
                    if delta:
                        yield delta
                elif event_type == "error":
                    raise RuntimeError(str(event.get("error", "daemon stream error")))
            tail = stripper.strip_delta("")
            if tail:
                yield tail
            return
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ):
            logger.warning(
                "Daemon stream failed; falling back to aggregate turn", exc_info=True
            )

    assistant_text, _sid, _obs, _warn = _run_turn(
        prompt, model_flag, workspace, cli_timeout
    )
    if assistant_text:
        yield assistant_text


class OpenAICLIProvider(CustomLLM):
    """LiteLLM custom provider backed by the Codex CLI."""

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

        assistant_text, session_id, observations, warnings = _run_turn(
            prompt, model_flag, workspace, cli_timeout
        )

        x_gateway = _make_x_gateway(session_id=session_id)
        x_gateway["observations"] = observations
        if warnings:
            x_gateway["warnings"] = warnings

        response = model_response or ModelResponse()
        response.choices = [
            Choices(
                index=0,
                message=Message(role="assistant", content=assistant_text),
                finish_reason="stop",
            )
        ]
        response.model = model
        response._hidden_params = {
            "x_gateway": x_gateway,
            "elapsed_seconds": time.monotonic() - t_start,
        }
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
        """Yield assistant text chunks as they arrive from the daemon stream."""
        prompt = last_user_message(messages)
        model_flag = _model_flag(model)
        cli_timeout = int(timeout) if isinstance(timeout, (int, float)) else 300
        workspace = _request_workspace(kwargs)

        for delta in _run_turn_stream(prompt, model_flag, workspace, cli_timeout):
            yield {
                "text": delta,
                "is_finished": False,
                "finish_reason": "",
                "usage": None,
                "index": 0,
            }
        yield {
            "text": "",
            "is_finished": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "index": 0,
        }

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        import asyncio

        return await asyncio.to_thread(self.completion, *args, **kwargs)

    async def astreaming(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[GenericStreamingChunk]:  # pyright: ignore[reportIncompatibleMethodOverride]
        import asyncio
        import threading

        queue: asyncio.Queue[GenericStreamingChunk | BaseException | None] = (
            asyncio.Queue()
        )
        loop = asyncio.get_running_loop()

        def produce() -> None:
            try:
                for chunk in self.streaming(*args, **kwargs):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        context = copy_context()
        threading.Thread(target=lambda: context.run(produce), daemon=True).start()
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item


openai_cli = OpenAICLIProvider()
