#!/usr/bin/env python3
"""Loopback-only fake model and usage server for isolated convergence verification."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reverso.protocols.headroom_compression import (
    DEFAULT_HEADROOM_METRICS,
    HeadroomCompressionConfig,
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/usage/headroom":
            body = {
                "schema_version": 1,
                "provider": "headroom",
                "headroom": DEFAULT_HEADROOM_METRICS.snapshot(
                    HeadroomCompressionConfig.from_env()
                ),
            }
        elif self.path.endswith("/v1/models"):
            provider = self.path.split("/")[1]
            model = {
                "ollama": "qwen3:8b",
                "kimi": "kimi-k3",
                "claude": "claude-opus-4-8",
                "copilot": "gpt-5.5",
                "auggie": "auggie-model",
                "deepseek": "deepseek-v4-pro",
                "codex-direct": "gpt-5.5",
                "openai-pass-through": "gpt-5.5",
            }.get(provider, "model")
            row = {"id": model, "object": "model"}
            body = {"object": "list", "data": [row]}
            if provider == "ollama":
                row.update(
                    {
                        "ollama_local": True,
                        "ollama_cloud": False,
                        "ollama_stale": False,
                    }
                )
                body["model_discovery_source"] = "ollama-inventory-disabled"
            elif provider == "kimi":
                body["model_discovery_source"] = "live"
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


ThreadingHTTPServer(
    ("127.0.0.1", int(os.environ["REVERSO_FAKE_GATEWAY_PORT"])), Handler
).serve_forever()
