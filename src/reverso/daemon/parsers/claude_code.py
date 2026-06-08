"""Output parser for Claude Code CLI stream-json output.

Parses the newline-delimited JSON stream produced by:
    claude -p PROMPT --output-format stream-json --verbose

See docs/spike-notes.md for the confirmed event format.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import AsyncIterator


def _obs_type_for_tool(name: str) -> str:
    """Map a Claude Code tool name to an observation type string."""
    _MAP = {
        "Read": "file_read",
        "Write": "file_write",
        "Edit": "file_edit",
        "MultiEdit": "file_edit",
        "Bash": "shell_cmd",
    }
    return _MAP.get(name, "tool_call")


class ClaudeCodeParser:
    """Parse --output-format stream-json --verbose JSON-lines from Claude Code.

    Usage::

        parser = ClaudeCodeParser()
        text, observations = await parser.parse_stream(line_iterator)
    """

    async def parse_stream(
        self,
        stdout_lines: AsyncIterator[str],
    ) -> tuple[str, list[dict]]:
        """Consume lines from the CLI stdout and return (assistant_text, observations).

        After this coroutine returns, ``self.session_id`` holds the CLI session
        UUID extracted from the ``result`` event (or None if not found).

        Each observation dict has the shape::

            {
                "type": str,           # obs type (file_read, shell_cmd, ...)
                "tool_name": str,      # original CLI tool name
                "args": dict,          # tool input arguments
                "is_error": bool,      # whether the tool result was an error
                "result_summary": str, # short summary of the result
                "timestamp": str,      # ISO-8601 UTC timestamp
            }
        """
        # Pending tool calls awaiting their matching tool_result event.
        # Keyed by tool_use_id.
        pending: dict[str, dict] = {}
        observations: list[dict] = []
        assistant_text = ""
        session_id: str | None = None
        self.session_id: str | None = None

        async for raw_line in stdout_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            # ---- assistant event: may contain tool_use content ----
            if event_type == "assistant":
                msg = event.get("message", {})
                content_list = msg.get("content", [])
                if not isinstance(content_list, list):
                    continue
                for content_item in content_list:
                    if not isinstance(content_item, dict):
                        continue
                    if content_item.get("type") == "tool_use":
                        tool_id = content_item.get("id", "")
                        tool_name = content_item.get("name", "")
                        tool_input = content_item.get("input", {})
                        pending[tool_id] = {
                            "tool_name": tool_name,
                            "args": tool_input,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                # capture session_id from assistant events too
                if not session_id:
                    session_id = event.get("session_id")

            # ---- user event: contains tool_result responses ----
            elif event_type == "user":
                msg = event.get("message", {})
                content_list = msg.get("content", [])
                if not isinstance(content_list, list):
                    continue
                for content_item in content_list:
                    if not isinstance(content_item, dict):
                        continue
                    if content_item.get("type") == "tool_result":
                        tool_use_id = content_item.get("tool_use_id", "")
                        is_error = bool(content_item.get("is_error", False))
                        raw_content = content_item.get("content", "")
                        if isinstance(raw_content, list):
                            # content is a list of content blocks
                            parts = []
                            for block in raw_content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    parts.append(block.get("text", ""))
                                elif isinstance(block, str):
                                    parts.append(block)
                            result_text = "".join(parts)
                        else:
                            result_text = str(raw_content) if raw_content else ""

                        result_summary = result_text[:200]

                        if tool_use_id in pending:
                            pend = pending.pop(tool_use_id)
                            obs = {
                                "type": _obs_type_for_tool(pend["tool_name"]),
                                "tool_name": pend["tool_name"],
                                "args": pend["args"],
                                "is_error": is_error,
                                "result_summary": result_summary,
                                "timestamp": pend["timestamp"],
                            }
                            observations.append(obs)
                        else:
                            # No matching pending tool call - record anyway.
                            obs = {
                                "type": "tool_call",
                                "tool_name": "",
                                "args": {},
                                "is_error": is_error,
                                "result_summary": result_summary,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                            observations.append(obs)

            # ---- result event: final assistant text ----
            elif event_type == "result":
                assistant_text = event.get("result", "")
                if not session_id:
                    session_id = event.get("session_id")

        self.session_id = session_id
        return assistant_text, observations
