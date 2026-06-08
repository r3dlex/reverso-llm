"""Output parser for Codex CLI --json output.

Parses the newline-delimited JSON stream produced by:
    codex exec "prompt" --json

See docs/spike-notes.md for the confirmed event format.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import AsyncIterator


class CodexCLIParser:
    """Parse --json JSON-lines output from the Codex CLI.

    Usage::

        parser = CodexCLIParser()
        text, observations = await parser.parse_stream(line_iterator)
    """

    async def parse_stream(
        self,
        stdout_lines: AsyncIterator[str],
    ) -> tuple[str, list[dict]]:
        """Consume lines from the CLI stdout and return (assistant_text, observations).

        Also captures the thread_id from the first event; callers that need
        the thread_id should access ``parser.thread_id`` after awaiting.

        Each observation dict has the shape::

            {
                "type": "shell_cmd",
                "tool_name": "shell",
                "args": {"command": str},
                "is_error": bool,
                "result_summary": str,
                "timestamp": str,
            }
        """
        self.thread_id: str | None = None
        text_parts: list[str] = []
        observations: list[dict] = []

        async for raw_line in stdout_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            # ---- thread.started: capture session key ----
            if event_type == "thread.started":
                self.thread_id = event.get("thread_id")

            # ---- item.completed: tool executions and assistant text ----
            elif event_type == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type")

                if item_type == "command_execution":
                    command = item.get("command", "")
                    aggregated_output = item.get("aggregated_output", "")
                    exit_code = item.get("exit_code")
                    is_error = exit_code not in (0, None)
                    result_summary = (
                        aggregated_output[:200] if aggregated_output else ""
                    )
                    obs = {
                        "type": "shell_cmd",
                        "tool_name": "shell",
                        "args": {"command": command},
                        "is_error": is_error,
                        "result_summary": result_summary,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    observations.append(obs)

                elif item_type == "agent_message":
                    text = item.get("text", "")
                    if text:
                        text_parts.append(text)

            # ---- turn.completed: turn is done (no action needed) ----
            elif event_type == "turn.completed":
                break

        assistant_text = "\n".join(text_parts)
        return assistant_text, observations
