"""OpenCode live proof unit contract (transport injected, never networked)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from reverso.opencode_client_live_proof import (
    PROOF_MARKER,
    OpencodeLiveProofInputs,
    run_opencode_live_proof,
)


def _poster(
    status: int = 200,
    text: str = PROOF_MARKER,
    body: dict[str, Any] | None = None,
):
    def post(
        url: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any] | None]:
        if body is not None:
            return status, body
        return status, {
            "id": "msg_proof",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }

    return post


def test_passed_report_is_secret_free_and_marker_backed() -> None:
    report = run_opencode_live_proof(OpencodeLiveProofInputs(), poster=_poster())
    assert report.status == "passed"
    public = report.to_public_dict()
    assert public["endpoint"] == "http://127.0.0.1:64946/v1/messages"
    json.dumps(public)


def test_non_200_fails_without_secrets() -> None:
    report = run_opencode_live_proof(
        OpencodeLiveProofInputs(), poster=_poster(status=503)
    )
    assert report.status == "failed"
    assert report.detail == "http_503"


def test_missing_marker_fails() -> None:
    report = run_opencode_live_proof(
        OpencodeLiveProofInputs(), poster=_poster(text="no marker here")
    )
    assert report.status == "failed"
    assert report.detail == "marker_missing"


def test_transport_error_fails_with_exception_type_only() -> None:
    def boom(
        url: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any] | None]:
        raise ConnectionError("secret host detail")

    report = run_opencode_live_proof(OpencodeLiveProofInputs(), poster=boom)
    assert report.status == "failed"
    assert report.detail == "ConnectionError"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status": 200}, "passed"),
        ({"status": 401}, "failed"),
    ],
)
def test_request_shape_matches_generated_fragment(kwargs: dict, expected: str) -> None:
    seen: dict[str, Any] = {}

    def post(
        url: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any] | None]:
        seen["url"] = url
        seen["payload"] = payload
        if kwargs["status"] != 200:
            return kwargs["status"], None
        return 200, {
            "content": [{"type": "text", "text": PROOF_MARKER}],
            "type": "message",
        }

    report = run_opencode_live_proof(OpencodeLiveProofInputs(), poster=post)
    assert report.status == expected
    assert seen["url"].endswith("/v1/messages")
    assert seen["payload"]["model"] == "claude-sonnet-4-6"
    assert seen["payload"]["messages"][0]["role"] == "user"
