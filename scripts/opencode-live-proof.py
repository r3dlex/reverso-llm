#!/usr/bin/env python3
"""Run the bounded OpenCode Go end-to-end proof (OCG-G7).

Drives Reverso's REAL ASGI apps in-process against the LIVE upstream, so both
surfaces are exercised through the actual translation stack without requiring the
deployed gateway daemon to be running.

Two surfaces, two transports, deliberately:

* ``/opencode/v1/responses`` exercises the Responses gateway over
  chat-completions. This is the Codex-facing path and involves no Anthropic
  round trip.
* ``/v1/messages`` exercises the Anthropic surface. For an id that accepts the
  native format this dispatches ``/messages`` directly; for ``grok-4.5`` it falls
  back through the translators.

Tool fidelity is compared across both so the double-translation cost is measured
rather than asserted. The key is read from the environment only and never
printed. Every request is bounded by ``max_tokens``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from reverso.protocols.adapters.opencode.credentials import (  # noqa: E402
    resolve_api_key,
)

TOOL = {
    "name": "get_weather",
    "description": "Look up the weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}
PROMPT = "Use the get_weather tool to check the weather in Berlin. Call the tool."


def _summarize_anthropic(message: dict[str, Any]) -> dict[str, Any]:
    blocks = message.get("content") or []
    kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
    tool_uses = [
        b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    texts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return {
        "block_kinds": kinds,
        "tool_use_count": len(tool_uses),
        "tool_names": [t.get("name") for t in tool_uses],
        "tool_input_parsed": [isinstance(t.get("input"), dict) for t in tool_uses],
        "stop_reason": message.get("stop_reason"),
        "has_thinking_block": "thinking" in kinds,
        # A model that leaks raw reasoning markers into text is a fidelity defect
        # distinct from one that returns a proper thinking block.
        "leaks_think_markers": any("</think>" in t or "<think>" in t for t in texts),
        "usage": message.get("usage"),
    }


def _summarize_responses(envelope: dict[str, Any]) -> dict[str, Any]:
    output = envelope.get("output") or []
    kinds = [item.get("type") for item in output if isinstance(item, dict)]
    calls = [
        i for i in output if isinstance(i, dict) and i.get("type") == "function_call"
    ]
    texts = json.dumps(
        [i for i in output if isinstance(i, dict) and i.get("type") == "message"]
    )
    return {
        "item_kinds": kinds,
        "function_call_count": len(calls),
        "tool_names": [c.get("name") for c in calls],
        "arguments_parse_ok": [_json_ok(c.get("arguments")) for c in calls],
        "status": envelope.get("status"),
        "leaks_think_markers": "</think>" in texts or "<think>" in texts,
        "usage": envelope.get("usage"),
    }


def _json_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return isinstance(value, dict)
    try:
        json.loads(value)
    except (TypeError, ValueError):
        return False
    return True


async def _probe_responses(model: str, max_tokens: int) -> dict[str, Any]:
    from reverso.protocols.responses_app import ResponsesGatewayApp
    from reverso.proxy.compose import build_adapters

    app = ResponsesGatewayApp({"opencode": build_adapters(env={})["opencode"]})
    transport = httpx.ASGITransport(app=app)
    body = {
        "model": model,
        "input": PROMPT,
        "max_output_tokens": max_tokens,
        "tools": [
            {
                "type": "function",
                "name": TOOL["name"],
                "description": TOOL["description"],
                "parameters": TOOL["input_schema"],
            }
        ],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/opencode/v1/responses", json=body, timeout=180.0)
    result: dict[str, Any] = {
        "surface": "responses",
        "model": model,
        "status_code": response.status_code,
    }
    if response.status_code == 200:
        result["fidelity"] = _summarize_responses(response.json())
    else:
        result["error_body"] = response.text[:300]
    return result


async def _probe_messages(model: str, max_tokens: int) -> dict[str, Any]:
    from reverso.protocols.anthropic_app import build_anthropic_app

    app = build_anthropic_app()
    transport = httpx.ASGITransport(app=app)
    body = {
        "model": f"opencode/{model}",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}],
        "tools": [TOOL],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/v1/messages", json=body, timeout=180.0)
    result: dict[str, Any] = {
        "surface": "anthropic_messages",
        "model": model,
        "status_code": response.status_code,
    }
    if response.status_code == 200:
        result["fidelity"] = _summarize_anthropic(response.json())
    else:
        result["error_body"] = response.text[:300]
    return result


async def _run(models: list[str], max_tokens: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        rows.append(await _probe_responses(model, max_tokens))
        rows.append(await _probe_messages(model, max_tokens))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opencode-live-proof")
    parser.add_argument(
        "--models",
        default="glm-5,kimi-k3,grok-4.5",
        help="comma separated ids: a native one, a contested one, and the deny-listed one",
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args(argv)

    if resolve_api_key() is None:
        print(
            "opencode-live-proof: no credential; set OPENCODE_API_KEY "
            "(or the read-only OCGO_API_KEY alias)",
            file=sys.stderr,
        )
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    rows = asyncio.run(_run(models, args.max_tokens))

    for row in rows:
        head = f"{row['surface']:<19} {row['model']:<10} {row['status_code']}"
        if "fidelity" in row:
            f = row["fidelity"]
            calls = f.get("tool_use_count", f.get("function_call_count"))
            print(
                f"{head}  tools={calls} names={f.get('tool_names')} "
                f"thinking={f.get('has_thinking_block', '-')} "
                f"leak={f.get('leaks_think_markers')}"
            )
        else:
            print(f"{head}  {row.get('error_body', '')[:120]}")

    if args.evidence is not None:
        args.evidence.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"opencode-live-proof: wrote {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
