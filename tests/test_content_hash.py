"""Content hashes (evidence-layer join key) + X-Ledvouch-Answer-Hash header.

Under test: the two SHA-256 digests of the observed terminal answer ride the
observation and the verdict audit event on every terminal turn; the optional
response header (default OFF) is a header-only delta on UNALTERED ships and
never describes bytes the client did not receive (absent on altered bodies).
canonicalize is the frozen 4-step spec COPIED from the demo reference
implementation — the digests must match it byte for byte.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from ledvouch.content_hash import answer_hashes, canonicalize
from ledvouch.enforce import BLOCK, FLAG, ObservationSink
from ledvouch.proxy import create_app, make_app

# ---- fixtures / harness ----------------------------------------------------


def _conversation():
    return [
        {"role": "user", "content": "what is Q3 revenue?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"revenue": 4500}'},
    ]


class _CaptureEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _fake_forward(terminal_content: str):
    """Terminal answer + value-map hidden call (response_format discriminates,
    same as the other test rigs); the mapping answer is irrelevant to the
    verdict (v3 existence key)."""

    async def forward(body, headers):
        if body.get("response_format"):
            return 200, {"choices": [{"message": {
                "content": json.dumps({"values": []})}}]}
        return 200, {"id": "t", "choices": [
            {"message": {"role": "assistant", "content": terminal_content}}]}

    return forward


def _post(client):
    return client.post("/v1/chat/completions", json={
        "model": "m", "messages": _conversation(),
        "tools": [{"type": "function", "function": {"name": "sql"}}],
    })


# ---- canonicalize: the frozen 4-step spec ----------------------------------


def test_canonicalize_matches_reference_spec():
    # newline unification + per-line trailing whitespace + outer blank lines
    assert canonicalize("a \r\nb\t\r\n\r\n") == "a\nb"
    assert canonicalize("\n\n  \nx\ny\n\n") == "x\ny"
    # NFC: decomposed e + combining acute → precomposed é
    assert canonicalize("café") == "café"
    # inner blank lines and leading spaces are PRESERVED (no fifth step)
    assert canonicalize("a\n\nb") == "a\n\nb"
    assert canonicalize("  indented") == "  indented"


def test_answer_hashes_are_sha256_of_raw_and_canon():
    answer = "revenue is 4500 \r\n"
    h = answer_hashes(answer)
    assert h["sha_raw"] == hashlib.sha256(answer.encode()).hexdigest()
    assert h["sha_canon"] == hashlib.sha256(b"revenue is 4500").hexdigest()
    # a paste-mangled copy (CRLF, trailing space) matches on sha_canon only
    assert answer_hashes("revenue is 4500")["sha_canon"] == h["sha_canon"]
    assert answer_hashes("revenue is 4500")["sha_raw"] != h["sha_raw"]


# ---- observation + audit carry the hashes ----------------------------------


def test_terminal_observation_and_verdict_event_carry_hashes():
    sink = ObservationSink()
    audit = _CaptureEmitter()
    app = make_app(fail_posture="open", forward=_fake_forward("revenue is 4500"),
                   sink=sink, audit=audit, mode=FLAG, stage="C")
    resp = _post(TestClient(app))
    assert resp.status_code == 200
    expected = answer_hashes("revenue is 4500")
    obs = sink.observations[0]
    assert obs.sha_raw == expected["sha_raw"]
    assert obs.sha_canon == expected["sha_canon"]
    verdict = next(e for e in audit.events if e["event"] == "verdict")
    assert verdict["sha_raw"] == expected["sha_raw"]
    assert verdict["sha_canon"] == expected["sha_canon"]


# ---- X-Ledvouch-Answer-Hash header (default OFF) ---------------------------


def test_header_off_by_default_and_on_is_header_only_delta():
    def app_of(on: bool):
        return make_app(fail_posture="open", forward=_fake_forward("revenue is 4500"),
                        mode=FLAG, stage="C", answer_hash_header=on)

    resp_off = _post(TestClient(app_of(False)))
    resp_on = _post(TestClient(app_of(True)))
    assert "x-ledvouch-answer-hash" not in resp_off.headers
    assert resp_off.content == resp_on.content  # body untouched either way
    assert resp_on.headers["x-ledvouch-answer-hash"] == \
        answer_hashes("revenue is 4500")["sha_canon"]


def test_header_absent_when_enforcement_altered_the_body():
    # block replaces the answer — a hash of the OBSERVED answer must not ride a
    # body the client did not receive (the audit event still carries it).
    audit = _CaptureEmitter()
    app = make_app(fail_posture="open", forward=_fake_forward("revenue is 9999"),
                   audit=audit, mode=BLOCK, stage="C", answer_hash_header=True)
    resp = _post(TestClient(app))
    assert "could not be verified" in resp.json()["choices"][0]["message"]["content"]
    assert "x-ledvouch-answer-hash" not in resp.headers
    verdict = next(e for e in audit.events if e["event"] == "verdict")
    assert verdict["sha_canon"] == answer_hashes("revenue is 9999")["sha_canon"]


def test_header_on_stream_unaltered_replay():
    raw = b""
    for delta, finish in [({"role": "assistant", "content": ""}, None),
                          ({"content": "revenue is "}, None),
                          ({"content": "4500"}, None),
                          ({}, "stop")]:
        chunk = {"id": "s", "object": "chat.completion.chunk", "created": 1,
                 "model": "m",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        raw += b"data: " + json.dumps(chunk).encode() + b"\n\n"
    raw += b"data: [DONE]\n\n"

    async def stream_forward(body, headers):
        async def it():
            yield raw
        return 200, {"content-type": "text/event-stream"}, it()

    app = make_app(fail_posture="open", forward=_fake_forward("unused"),
                   stream_forward=stream_forward, mode=FLAG, stage="C",
                   answer_hash_header=True)
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={
        "model": "m", "messages": _conversation(), "stream": True,
        "tools": [{"type": "function", "function": {"name": "sql"}}],
    })
    assert resp.content == raw  # verbatim replay, body untouched
    assert resp.headers["x-ledvouch-answer-hash"] == \
        answer_hashes("revenue is 4500")["sha_canon"]


# ---- env contract ----------------------------------------------------------


def test_create_app_refuses_malformed_header_env(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "open")
    monkeypatch.setenv("LEDVOUCH_ANSWER_HASH_HEADER", "yes")
    with pytest.raises(RuntimeError, match="LEDVOUCH_ANSWER_HASH_HEADER"):
        create_app()


def test_create_app_accepts_on(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "open")
    monkeypatch.setenv("LEDVOUCH_ANSWER_HASH_HEADER", "on")
    assert create_app().state.answer_hash_header is True
