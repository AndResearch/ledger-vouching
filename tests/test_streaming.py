"""A1 streaming tests (spectate-passthrough + terminal-only buffering).

Transparency contract under test (fixed in streaming.py BEFORE these tests):
  ① passthrough path (tool_calls turn): CHUNK-UNIT BYTE IDENTITY — the client
     receives the exact byte chunks the fake upstream emitted, in order, even
     when SSE events straddle chunk boundaries.
  ② buffered path (content turn, unaltered ship): SSE data-payload
     CONCATENATION identity (chunk-boundary reproduction is not the contract).
  ③ enforcement under stream: flag ships verbatim; block ships a synthesized
     refusal stream; retry ships the model's own repaired answer / keep-alive
     tool_calls as a synthesized stream — hidden/retry calls are NON-stream.
  ④ first-delta edges: role-only, empty-content, and empty deltas stay
     undecided; late tool_calls after content (mixed turn) ships verbatim with
     no enforcement; unparseable streams degrade to verbatim spectator with an
     error observation (never block on OUR failure).
"""

from __future__ import annotations

import asyncio
import copy
import json

from ledvouch.enforce import BLOCK, FLAG, RETRY, ObservationSink
from ledvouch.grounding import INSUFFICIENT
from ledvouch.proxy import make_app
from ledvouch.streaming import (
    SSEScanner,
    aggregate_stream,
    parse_chunk_payloads,
    synthesize_sse,
)

# ---- SSE fixtures ----------------------------------------------------------


def _chunk(delta, finish=None, index=0, **top):
    return {
        "id": "s1", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
        **top,
    }


def _sse(events) -> bytes:
    out = b""
    for e in events:
        payload = e if isinstance(e, str) else json.dumps(e)
        out += b"data: " + payload.encode() + b"\n\n"
    return out


def _split(raw: bytes, cuts: list[int]) -> list[bytes]:
    """Split raw bytes at the given offsets — used to make SSE events straddle
    chunk boundaries on purpose."""
    parts, prev = [], 0
    for c in [*cuts, len(raw)]:
        parts.append(raw[prev:c])
        prev = c
    return [p for p in parts if p]


TOOL_TURN_EVENTS = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({}),  # fully empty delta — must stay undecided
    _chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "sql", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 0,
                            "function": {"arguments": '{"q": "SELECT"}'}}]}),
    _chunk({}, finish="tool_calls"),
    "[DONE]",
]


def _content_turn_events(*deltas: str, finish: str = "stop"):
    return [
        _chunk({"role": "assistant", "content": ""}),
        *[_chunk({"content": d}) for d in deltas],
        _chunk({}, finish=finish),
        "[DONE]",
    ]


def _conversation():
    return [
        {"role": "user", "content": "what is Q3 revenue?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"revenue": 4500}'},
    ]


# ---- harness ---------------------------------------------------------------


def _stream_forward(chunks: list[bytes], status: int = 200):
    async def sf(body, headers):
        async def it():
            for c in chunks:
                yield c
        return status, {"content-type": "text/event-stream"}, it()
    return sf


def _stream_post(app, body: dict):
    """Drive the ASGI app directly and collect each http.response.body message as
    one received chunk — TestClient's iter_raw() merges message boundaries, which
    would make the chunk-unit byte-identity claim untestable."""
    payload = json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions", "root_path": "", "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"host", b"testserver")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    requested = {"sent": False}

    async def receive():
        if requested["sent"]:
            return {"type": "http.disconnect"}
        requested["sent"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    received = [m["body"] for m in messages
                if m["type"] == "http.response.body" and m.get("body")]
    return status, received


def _data_concat(raw: bytes) -> bytes:
    scanner = SSEScanner()
    return b"".join(scanner.feed(raw))


def _aggregate_received(received: list[bytes]) -> dict:
    scanner = SSEScanner()
    payloads = []
    for c in received:
        payloads.extend(scanner.feed(c))
    assert not scanner.residual().strip()
    return aggregate_stream(parse_chunk_payloads(payloads))


def _stage_c_stream_client(sse_chunks, mappings, retry_responses=(), mode=FLAG):
    """Stream seam serves the agent turn; the non-stream forward serves the
    hidden value-map calls and the retry push-backs (same discrimination as
    test_stage_c's fake)."""
    sink = ObservationSink()
    seen = {"hidden": [], "retry": []}
    mappings = list(mappings)
    retry_responses = list(retry_responses)

    async def fake_forward(body, headers):
        if body.get("response_format"):
            seen["hidden"].append(body)
            if not mappings:
                return 200, {"choices": [{"message": {"content": '{"values": []}'}}]}
            content = json.dumps({"values": mappings.pop(0)})
            return 200, {"choices": [{"message": {"content": content}}]}
        seen["retry"].append(body)
        return 200, copy.deepcopy(retry_responses.pop(0))

    app = make_app(fail_posture="open", forward=fake_forward, stream_forward=_stream_forward(sse_chunks),
                   sink=sink, mode=mode, stage="C")
    return app, sink, seen


def _stream_body(tools=True):
    body = {"model": "m", "messages": _conversation(), "stream": True}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "sql"}}]
    return body


GOOD_MAP = [{"value": "4500", "source": "$.s1.revenue"}]
BAD_MAP = [{"value": "3200", "source": "unknown"}]


# ---- ① passthrough: chunk-unit byte identity -------------------------------


def test_tool_calls_turn_passthrough_chunk_byte_identical():
    raw = _sse(TOOL_TURN_EVENTS)
    # cuts chosen to straddle event boundaries mid-JSON
    sent = _split(raw, [7, 55, 56, 200])
    sink = ObservationSink()
    app = make_app(fail_posture="open", stream_forward=_stream_forward(sent), sink=sink)
    status, received = _stream_post(app, _stream_body())
    assert status == 200
    assert received == sent  # per-chunk byte identity
    assert sink.observations == []  # spectator: no terminal observation


def test_role_only_and_empty_deltas_stay_undecided_until_tool_calls():
    # The classification prefix (role-only, empty delta) is held back, then
    # flushed verbatim — identity must hold even when the decisive delta
    # arrives many chunks in.
    raw = _sse(TOOL_TURN_EVENTS)
    sent = [raw[i:i + 13] for i in range(0, len(raw), 13)]  # tiny uniform chunks
    app = make_app(fail_posture="open", stream_forward=_stream_forward(sent))
    _, received = _stream_post(app, _stream_body())
    assert received == sent


# ---- ② buffered path: data-payload concatenation identity ------------------


def test_terminal_flag_stage_a_ships_verbatim_and_records_floor():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    sent = _split(raw, [20, 21, 100])
    sink = ObservationSink()
    app = make_app(fail_posture="open", stream_forward=_stream_forward(sent), sink=sink)  # stage A flag
    status, received = _stream_post(app, _stream_body())
    assert status == 200
    # the contract: SSE data-payload concatenation identity
    assert _data_concat(b"".join(received)) == _data_concat(raw)
    # (implementation ships the buffered chunks verbatim — strictly stronger)
    assert received == sent
    obs = sink.observations[0]
    assert obs.verdict == INSUFFICIENT and "3200" in obs.missing
    assert obs.shipped is True


def test_stage_c_flag_grounded_ships_verbatim():
    raw = _sse(_content_turn_events("revenue is ", "4500"))
    sent = _split(raw, [33])
    client, sink, seen = _stage_c_stream_client(sent, [GOOD_MAP])
    _, received = _stream_post(client, _stream_body())
    assert b"".join(received) == raw
    payload = sink.observations[0].stage_b
    assert payload["verdict"] == "grounded" and payload["action"] == "ship"


def test_stage_c_flag_ungrounded_still_ships_verbatim():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    client, sink, seen = _stage_c_stream_client([raw], [BAD_MAP])
    _, received = _stream_post(client, _stream_body())
    assert b"".join(received) == raw  # flag never alters the bytes
    assert sink.observations[0].stage_b["verdict"] == "ungrounded"


# ---- ③ enforcement under stream --------------------------------------------


def test_stage_c_block_ships_synthesized_refusal_stream():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    client, sink, seen = _stage_c_stream_client([raw], [BAD_MAP], mode=BLOCK)
    _, received = _stream_post(client, _stream_body())
    assert received[-1] == b"data: [DONE]\n\n"
    body = _aggregate_received(received)
    content = body["choices"][0]["message"]["content"]
    assert "could not be verified" in content and "3200" in content
    assert body["choices"][0]["finish_reason"] == "stop"
    obs = sink.observations[0]
    assert obs.shipped is False and obs.stage_b["action"] == "block"


def test_stage_c_retry_repair_ships_models_own_answer_as_stream():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    repaired = {"id": "r", "choices": [
        {"message": {"role": "assistant", "content": "revenue is 4500"}}]}
    client, sink, seen = _stage_c_stream_client(
        [raw], [BAD_MAP, GOOD_MAP], retry_responses=[repaired], mode=RETRY)
    _, received = _stream_post(client, _stream_body())
    body = _aggregate_received(received)
    assert body["choices"][0]["message"]["content"] == "revenue is 4500"
    assert sink.observations[0].stage_b["action"] == "repair"
    # hidden + retry calls ride the NON-stream forward, with stream stripped
    for req in [*seen["hidden"], *seen["retry"]]:
        assert "stream" not in req and "stream_options" not in req


def test_stage_c_retry_keep_alive_ships_synthesized_tool_calls_stream():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    tool_resp = {"id": "k", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c9", "type": "function",
                        "function": {"name": "sql", "arguments": "{}"}}]}}]}
    client, sink, seen = _stage_c_stream_client(
        [raw], [BAD_MAP], retry_responses=[tool_resp], mode=RETRY)
    _, received = _stream_post(client, _stream_body())
    body = _aggregate_received(received)
    msg = body["choices"][0]["message"]
    assert msg["tool_calls"][0]["id"] == "c9"
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert sink.observations[0].stage_b["action"] == "pushback"


# ---- ④ edges ---------------------------------------------------------------


def test_mixed_turn_content_then_tool_calls_ships_verbatim_no_enforcement():
    # content classified first, tool_calls arrive later: a normal turn after
    # all — verbatim ship, no hidden call, no observation, even in BLOCK mode.
    events = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "let me check 3200"}),
        _chunk({"tool_calls": [{"index": 0, "id": "c2", "type": "function",
                                "function": {"name": "sql", "arguments": "{}"}}]}),
        _chunk({}, finish="tool_calls"),
        "[DONE]",
    ]
    raw = _sse(events)
    sent = _split(raw, [40])
    client, sink, seen = _stage_c_stream_client(sent, [BAD_MAP], mode=BLOCK)
    _, received = _stream_post(client, _stream_body())
    assert received == sent  # verbatim
    assert seen["hidden"] == [] and sink.observations == []


def test_upstream_stream_error_surfaced_verbatim():
    err = json.dumps({"error": {"message": "rate limited"}}).encode()
    app = make_app(fail_posture="open", stream_forward=_stream_forward([err], status=429))
    status, received = _stream_post(app, _stream_body())
    assert status == 429
    assert b"".join(received) == err


def test_non_sse_200_degrades_to_verbatim_spectator_never_blocks():
    # A 200 that is not SSE at all (upstream ignored stream=true): OUR parse
    # failure must never punish the client — verbatim ship + error observation,
    # even in BLOCK mode.
    plain = json.dumps({"choices": [{"message": {"content": "revenue is 3200"}}]}).encode()
    client, sink, seen = _stage_c_stream_client([plain], [BAD_MAP], mode=BLOCK)
    _, received = _stream_post(client, _stream_body())
    assert b"".join(received) == plain
    obs = sink.observations[0]
    assert obs.verdict == "stream_parse_error" and obs.shipped is True
    assert seen["hidden"] == []  # no enforcement machinery ran


def test_truncated_sse_stream_degrades_to_verbatim_with_observation():
    raw = _sse(_content_turn_events("revenue is ", "3200"))
    truncated = raw[:-9]  # cut inside the final event terminator
    client, sink, seen = _stage_c_stream_client([truncated], [BAD_MAP], mode=BLOCK)
    _, received = _stream_post(client, _stream_body())
    assert b"".join(received) == truncated
    assert sink.observations[0].verdict == "stream_parse_error"


def test_garbage_data_payload_degrades_and_drains_full_stream():
    chunks = [b"data: {not json}\n\n", b"data: more bytes\n\n"]
    client, sink, seen = _stage_c_stream_client(chunks, [BAD_MAP], mode=BLOCK)
    _, received = _stream_post(client, _stream_body())
    assert received == chunks  # the replay is COMPLETE (drained past the error)
    assert sink.observations[0].verdict == "stream_parse_error"


# ---- streaming unit: aggregate / synthesize --------------------------------


def test_aggregate_stream_reassembles_content_tool_calls_and_usage():
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "reve"}),
        _chunk({"content": "nue"}),
        _chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                "function": {"name": "sq", "arguments": '{"q"'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}),
        _chunk({"tool_calls": [{"index": 1, "id": "c2", "type": "function",
                                "function": {"name": "get", "arguments": "{}"}}]}),
        _chunk({}, finish="tool_calls"),
        {"id": "s1", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [], "usage": {"total_tokens": 7}},
    ]
    body = aggregate_stream(chunks)
    msg = body["choices"][0]["message"]
    assert msg["content"] == "revenue"
    assert msg["tool_calls"] == [
        {"id": "c1", "type": "function",
         "function": {"name": "sq", "arguments": '{"q": 1}'}},
        {"id": "c2", "type": "function", "function": {"name": "get", "arguments": "{}"}},
    ]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["usage"] == {"total_tokens": 7} and body["id"] == "s1"


def test_synthesize_sse_round_trips_through_aggregate():
    original = {"id": "x", "created": 5, "model": "m", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "revenue is 4500"},
         "finish_reason": "stop"}],
        "usage": {"total_tokens": 9}}
    body = _aggregate_received(synthesize_sse(original, include_usage=True))
    assert body["choices"][0]["message"]["content"] == "revenue is 4500"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"total_tokens": 9}
    # opt-in respected: no usage chunk unless the client asked for one
    no_usage = _aggregate_received(synthesize_sse(original, include_usage=False))
    assert "usage" not in no_usage
