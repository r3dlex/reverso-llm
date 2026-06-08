"""Unit tests for Responses API think-block stripping."""

from __future__ import annotations

import asyncio
import json

from reverso.middleware.responses_think_stripper import (
    _strip_sse_payload,
    ResponsesThinkStripperMiddleware,
)


def _body(sent: list[dict]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )


def _payloads(sent: list[dict]) -> list[dict]:
    payloads = []
    for line in _body(sent).splitlines():
        if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
            continue
        payloads.append(json.loads(line[6:]))
    return payloads


def test_strips_think_blocks_from_split_sse_deltas_and_done_events() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"content-length", b"999"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1",'
                    b'"output_index":0,"content_index":0,"delta":"<think>hidden"}\n\n'
                ),
                "more_body": True,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1",'
                    b'"output_index":0,"content_index":0,"delta":" reasoning</think>Hey!"}\n\n'
                    b'data: {"type":"response.output_text.done","text":"<think>hidden reasoning</think>Hey!"}\n\n'
                    b'data: {"type":"response.completed","response":{"output":[{"type":"message","content":['
                    b'{"type":"output_text","text":"<think>hidden reasoning</think>Hey!"}]}]}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = _body(sent)
    payloads = _payloads(sent)
    assert b"<think>" not in body
    assert b"</think>" not in body
    assert [payload["type"] for payload in payloads[:3]] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
    ]
    assert payloads[2]["delta"] == "Hey!"
    assert payloads[3]["text"] == "Hey!"
    assert payloads[4]["response"]["output"][0]["content"][0]["text"] == "Hey!"
    assert sent[0]["headers"] == [(b"content-type", b"text/event-stream")]


def test_strips_think_blocks_from_non_stream_json_response() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"999"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "<think>secret</think>OK",
                                    }
                                ],
                            }
                        ],
                    }
                ).encode("utf-8"),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    payload = json.loads(_body(sent))
    assert payload["output"][0]["content"][0]["text"] == "OK"
    assert sent[0]["headers"] == [(b"content-type", b"application/json")]


def test_strips_split_think_blocks_across_non_stream_content_parts() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "<think>"},
                                    {"type": "output_text", "text": "hidden reasoning"},
                                    {"type": "output_text", "text": "</think>OK"},
                                ],
                            }
                        ],
                    }
                ).encode("utf-8"),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    content = json.loads(_body(sent))["output"][0]["content"]
    assert [part["text"] for part in content] == ["", "", "OK"]
    assert b"hidden reasoning" not in _body(sent)


def test_collapses_leading_reasoning_parts_without_visible_tags() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "The user asked for a short greeting.",
                                    },
                                    {"type": "output_text", "text": "Hey!"},
                                ],
                            }
                        ],
                    }
                ).encode("utf-8"),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    content = json.loads(_body(sent))["output"][0]["content"]
    assert [part["text"] for part in content] == ["", "Hey!"]
    assert b"The user asked" not in _body(sent)


def test_drops_reasoning_output_items_from_non_stream_json() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "output": [
                            {
                                "type": "reasoning",
                                "content": [
                                    {"type": "output_text", "text": "hidden reasoning"}
                                ],
                            },
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "Hey!"}],
                            },
                        ],
                    }
                ).encode("utf-8"),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    output = json.loads(_body(sent))["output"]
    assert [item["type"] for item in output] == ["message"]
    assert b"hidden reasoning" not in _body(sent)


def test_drops_reasoning_summary_sse_events_and_repairs_content_part_done() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"msg_1","type":"message","role":"assistant","status":"in_progress","content":[]}}\n\n'
                    b'data: {"type":"response.content_part.added","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}\n\n'
                    b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1",'
                    b'"output_index":0,"delta":"hidden"}\n\n'
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"delta":"OK"}\n\n'
                    b'data: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"text":"OK"}\n\n'
                    b'data: {"type":"response.content_part.done","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"part":{"type":"reasoning_text","reasoning":"hidden"}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = _body(sent)
    payloads = _payloads(sent)
    assert b"response.reasoning_summary_text.delta" not in body
    assert b"reasoning_text" not in body
    assert b"hidden" not in body
    assert [payload["type"] for payload in payloads] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
    ]
    assert payloads[-1]["part"] == {
        "type": "output_text",
        "text": "OK",
        "annotations": [],
    }


def test_drops_empty_sse_text_delta() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"delta":""}\n\n'
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"delta":"OK"}\n\n'
                ),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    payloads = _payloads(sent)
    assert [payload["type"] for payload in payloads] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
    ]
    assert payloads[-1]["delta"] == "OK"


def test_drops_reasoning_items_and_synthesizes_text_preamble_for_codex() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"rs_1","type":"reasoning","status":"in_progress"},"model":"MiniMax-M3"}\n\n'
                    b'data: {"type":"response.output_item.done","output_index":0,'
                    b'"item":{"id":"rs_1","type":"reasoning","summary":[{"type":"summary_text","text":"hidden"}]},'
                    b'"model":"MiniMax-M3"}\n\n'
                    b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"delta":"\\n</think>\\n\\nOK","model":"MiniMax-M3"}\n\n'
                    b'data: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,'
                    b'"content_index":0,"text":"OK","model":"MiniMax-M3"}\n\n'
                ),
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "path": "/v1/responses"}, receive, send))

    body = _body(sent)
    payloads = _payloads(sent)
    assert b"reasoning" not in body
    assert b"</think>" not in body
    assert [payload["type"] for payload in payloads] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
    ]
    assert payloads[0]["item"] == {
        "id": "msg_1",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    assert payloads[1]["part"] == {"type": "output_text", "text": "", "annotations": []}
    assert payloads[2]["delta"] == "OK"


def test_does_not_modify_non_responses_sse_streams() -> None:
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"delta":"<think>visible here</think>"}\n\n',
                "more_body": False,
            }
        )

    middleware = ResponsesThinkStripperMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware({"type": "http", "path": "/v1/chat/completions"}, receive, send)
    )

    assert b"<think>visible here</think>" in _body(sent)


def test_responses_think_stripper_splits_large_visible_delta() -> None:
    payload = {
        "type": "response.output_text.delta",
        "item_id": "item_1",
        "output_index": 0,
        "content_index": 0,
        "delta": "abcdefghijklmnopqrstuvwxyz" * 4,
    }

    rewritten = _strip_sse_payload(
        payload, {}, {}, set(), set(), split_visible_deltas=True
    )

    deltas = [
        event["delta"]
        for event in rewritten
        if event.get("type") == "response.output_text.delta"
    ]
    assert len(deltas) >= 2
    assert "".join(deltas) == payload["delta"]


def test_responses_think_stripper_does_not_split_large_delta_without_profile_flag() -> (
    None
):
    payload = {
        "type": "response.output_text.delta",
        "item_id": "item_1",
        "output_index": 0,
        "content_index": 0,
        "delta": "abcdefghijklmnopqrstuvwxyz" * 4,
    }

    rewritten = _strip_sse_payload(payload, {}, {}, set(), set())

    deltas = [
        event["delta"]
        for event in rewritten
        if event.get("type") == "response.output_text.delta"
    ]
    assert deltas == [payload["delta"]]
