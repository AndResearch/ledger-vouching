"""The `observation` audit event (opt-in, default OFF) — evidence-layer substrate.

Under test: OFF keeps the audit stream identical to pre-observation builds; ON
emits one observation event per terminal turn carrying the full portal
substrate (answer, goal, eval, steps, hashes) plus wire-derived identity —
tier 1 auth-key hash (never the credential), tier 2 opportunistic `user` field
and ONLY the allow-listed headers.
"""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from ledvouch.content_hash import answer_hashes
from ledvouch.enforce import FLAG
from ledvouch.proxy import make_app


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
    async def forward(body, headers):
        if body.get("response_format"):
            return 200, {"choices": [{"message": {
                "content": json.dumps({"values": []})}}]}
        return 200, {"id": "t", "choices": [
            {"message": {"role": "assistant", "content": terminal_content}}]}

    return forward


def _post(client, headers=None):
    return client.post("/v1/chat/completions", json={
        "model": "m", "messages": _conversation(), "user": "alice",
        "tools": [{"type": "function", "function": {"name": "sql"}}],
    }, headers=headers or {})


def test_off_by_default_stream_is_unchanged():
    audit = _CaptureEmitter()
    app = make_app(fail_posture="open", forward=_fake_forward("revenue is 4500"),
                   audit=audit, mode=FLAG, stage="C")
    _post(TestClient(app))
    assert [e["event"] for e in audit.events] == ["verdict"]


def test_on_emits_observation_with_substrate_and_identity():
    audit = _CaptureEmitter()
    app = make_app(fail_posture="open", forward=_fake_forward("revenue is 4500"),
                   audit=audit, mode=FLAG, stage="C", emit_observation=True,
                   identity_headers=("x-litellm-user",),
                   upstream_base="https://api.example.com/v1")
    _post(TestClient(app), headers={
        "Authorization": "Bearer sk-test-123",
        "X-LiteLLM-User": "team-a/alice",
        "Cookie": "secret=1",  # NOT allow-listed — must not be copied
    })
    kinds = [e["event"] for e in audit.events]
    assert kinds == ["observation", "verdict"]
    obs = audit.events[0]
    expected = answer_hashes("revenue is 4500")
    assert obs["answer"] == "revenue is 4500"
    assert obs["goal"] == "what is Q3 revenue?"
    assert obs["sha_raw"] == expected["sha_raw"]
    assert obs["sha_canon"] == expected["sha_canon"]
    assert obs["verdict"] == "grounded" and obs["action"] == "ship"
    # routing facts for the portal's connection list (URL only, never the key)
    assert obs["model"] == "m"
    assert obs["upstream_base"] == "https://api.example.com/v1"
    assert obs["eval"]["candidates"]["4500"] == ["$.s1"]
    assert obs["steps"] == [{"n": 1, "name": "sql",
                             "arguments": '{"q": "SELECT"}',
                             "output": '{"revenue": 4500}'}]
    ident = obs["identity"]
    assert ident["auth_key_hash"] == hashlib.sha256(b"sk-test-123").hexdigest()
    assert ident["request_user"] == "alice"
    assert ident["headers"] == {"x-litellm-user": "team-a/alice"}
    assert "cookie" not in json.dumps(ident).lower()


def test_identity_absent_material_is_visible_nulls():
    audit = _CaptureEmitter()
    app = make_app(fail_posture="open", forward=_fake_forward("revenue is 4500"),
                   audit=audit, mode=FLAG, stage="C", emit_observation=True)
    client = TestClient(app)
    client.post("/v1/chat/completions", json={
        "model": "m", "messages": _conversation(),
        "tools": [{"type": "function", "function": {"name": "sql"}}],
    })
    ident = audit.events[0]["identity"]
    assert ident["auth_key_hash"] is None  # TestClient sends no Authorization
    assert ident["request_user"] is None
    assert "headers" not in ident
