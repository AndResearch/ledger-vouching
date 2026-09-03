"""Stage A operations tests — A2 fail posture / A3 audit stream / A4 healthz+metrics.

A2 semantics under test: the posture fires ONLY where the ledger voucher's own
machinery failed where enforcement needed it (retry-call failure, unparseable
stream). Since v3 (candidate-path enum) the value-map verdict is deterministic
at mint time, so an ATTRIBUTION hidden-call failure is no longer a posture
site — it is recorded and the verdict stands (covered in test_stage_c). open =
ship the original, flagged; closed = honest refusal. flag mode never alters the
body regardless (light contract). Upstream unreachable on the MAIN forward has
nothing to ship — honest 502 both ways.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ledvouch.audit import (
    AUDIT_SCHEMA,
    FileAuditEmitter,
    StdoutAuditEmitter,
    audit_emitter_from_env,
    render_event,
    terminal_events,
)
from ledvouch.enforce import BLOCK, FLAG, RETRY, ObservationSink
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
    """Captures raw audit events (the envelope is the emitter's concern and is
    tested separately via render_event)."""

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _app_with_failing_retry_call(mode, fail_posture):
    """Terminal turn 'revenue is 3200' (ungrounded by mint — candidate zero);
    the retry forward dies — the ledger voucher holds an ungrounded answer it cannot
    push back on (the remaining non-stream A2 machinery-failure site)."""
    sink = ObservationSink()
    audit = _CaptureEmitter()

    async def fake_forward(body, headers):
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            raise ConnectionError("upstream gone")
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": "revenue is 3200"}}]}

    app = make_app(fail_posture=fail_posture, forward=fake_forward, sink=sink,
                   audit=audit, mode=mode, stage="C")
    return TestClient(app), sink, audit


def _post(client, tools=True):
    body = {"model": "m", "messages": _conversation()}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "sql"}}]
    return client.post("/v1/chat/completions", json=body)


# ---- A2: make_app / create_app configuration contract ----------------------


def test_make_app_rejects_invalid_posture():
    with pytest.raises(ValueError, match="fail_posture"):
        make_app(fail_posture="sideways")


def test_create_app_refuses_to_start_without_posture(monkeypatch):
    monkeypatch.delenv("LEDVOUCH_FAIL_POSTURE", raising=False)
    with pytest.raises(RuntimeError, match="LEDVOUCH_FAIL_POSTURE"):
        create_app()
    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "maybe")
    with pytest.raises(RuntimeError, match="LEDVOUCH_FAIL_POSTURE"):
        create_app()


def test_create_app_starts_with_posture_set(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "closed")
    monkeypatch.delenv("LEDVOUCH_AUDIT_STREAM", raising=False)
    app = create_app()
    assert app.state.fail_posture == "closed"
    assert app.state.mode == FLAG and app.state.stage == "C"  # documented defaults


# ---- A2: posture at the machinery-failure sites ----------------------------


def test_closed_blocks_when_retry_call_fails():
    client, sink, audit = _app_with_failing_retry_call(RETRY, "closed")
    resp = _post(client)
    content = resp.json()["choices"][0]["message"]["content"]
    assert "verification unavailable" in content and "fail-closed" in content
    obs = sink.observations[0]
    assert obs.shipped is False
    assert obs.stage_b["action"] == "posture_block"
    assert obs.stage_b["posture"] == "closed"


def test_open_ships_when_retry_call_fails_and_records_posture():
    client, sink, audit = _app_with_failing_retry_call(RETRY, "open")
    resp = _post(client)
    assert resp.json()["choices"][0]["message"]["content"] == "revenue is 3200"
    obs = sink.observations[0]
    assert obs.shipped is True and obs.stage_b["action"] == "degrade_flag"
    postures = [e for e in audit.events if e["event"] == "posture"]
    assert postures and postures[0]["posture"] == "open"  # activation IS recorded


def test_flag_mode_never_reaches_a_posture_site():
    # flag mode has no retry machinery and never alters the body — an ungrounded
    # verdict stays a flagged observation; no posture event exists to fire.
    client, sink, audit = _app_with_failing_retry_call(FLAG, "closed")
    resp = _post(client)
    assert resp.json()["choices"][0]["message"]["content"] == "revenue is 3200"
    assert sink.observations[0].stage_b["action"] == "ship"
    assert not [e for e in audit.events if e["event"] == "posture"]


def test_upstream_unreachable_is_honest_502_both_postures():
    for posture in ("open", "closed"):
        audit = _CaptureEmitter()

        async def dead_forward(body, headers):
            raise ConnectionError("connection refused")

        app = make_app(fail_posture=posture, forward=dead_forward, audit=audit)
        resp = TestClient(app).post(
            "/v1/chat/completions", json={"model": "m", "messages": []})
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "ledvouch_upstream_unreachable"
        postures = [e for e in audit.events if e["event"] == "posture"]
        assert postures and "unreachable" in postures[0]["trigger"]


def test_stream_parse_failure_closed_ships_refusal_stream():
    # unparseable stream + closed + enforcement mode → synthesized refusal
    # (the open twin — verbatim spectator — is covered in test_streaming.py).
    plain = json.dumps({"choices": [{"message": {"content": "revenue is 3200"}}]}).encode()

    async def stream_forward(body, headers):
        async def it():
            yield plain
        return 200, {"content-type": "text/event-stream"}, it()

    async def fake_forward(body, headers):  # pragma: no cover — must not be hit
        raise AssertionError("no verification machinery may run on a parse failure")

    sink = ObservationSink()
    app = make_app(fail_posture="closed", forward=fake_forward,
                   stream_forward=stream_forward, sink=sink, mode=BLOCK, stage="C")
    with TestClient(app).stream(
        "POST", "/v1/chat/completions",
        json={"model": "m", "messages": _conversation(), "stream": True},
    ) as resp:
        raw = b"".join(resp.iter_raw())
    assert b"verification unavailable" in raw and b"data: [DONE]\n\n" in raw
    obs = sink.observations[0]
    assert obs.verdict == "stream_parse_error" and obs.shipped is False
    assert obs.stage_b["action"] == "posture_block"


# ---- A3: audit events -------------------------------------------------------


def test_terminal_events_derivation_pure():
    payload = {
        "verdict": "ungrounded", "action": "block",
        "eval": {"missing": ["3200"], "reasons": {"3200": "source unknown"}},
        "laundered": [{"value": "77"}],
        "posture": "open", "posture_trigger": "hidden call failed",
    }
    events = terminal_events(mode=BLOCK, stage="C", floor_verdict="insufficient",
                             floor_missing=("3200",), payload=payload)
    kinds = [e["event"] for e in events]
    assert kinds == ["verdict", "enforcement", "laundering", "posture"]
    assert events[0]["verdict"] == "ungrounded" and events[0]["missing"] == ["3200"]
    assert events[1]["reasons"] == {"3200": "source unknown"}
    # stage A (no payload): floor verdict only, ship action, no enforcement event
    floor_only = terminal_events(mode=FLAG, stage="A", floor_verdict="sufficient",
                                 floor_missing=(), payload=None)
    assert [e["event"] for e in floor_only] == ["verdict"]
    assert floor_only[0]["verdict"] == "sufficient" and floor_only[0]["action"] == "ship"


def test_audit_events_emitted_on_terminal_block():
    sink = ObservationSink()
    audit = _CaptureEmitter()

    async def fake_forward(body, headers):
        if body.get("response_format"):
            content = json.dumps({"values": [{"value": "3200", "source": "unknown"}]})
            return 200, {"choices": [{"message": {"content": content}}]}
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": "revenue is 3200"}}]}

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink,
                   audit=audit, mode=BLOCK, stage="C")
    _post(TestClient(app))
    kinds = [e["event"] for e in audit.events]
    assert kinds == ["verdict", "enforcement"]
    assert audit.events[0]["verdict"] == "ungrounded"
    assert audit.events[1]["action"] == "block"
    assert audit.events[1]["missing"] == ["3200"]


def test_render_event_envelope_fields(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_DEPLOYMENT_ID", "dep-1")
    monkeypatch.setenv("LEDVOUCH_SYSTEM_ID", "sys-9")
    line = render_event({"event": "verdict", "verdict": "grounded"})
    obj = json.loads(line)
    assert obj["schema"] == AUDIT_SCHEMA
    assert obj["deployment_id"] == "dep-1" and obj["system_id"] == "sys-9"
    assert obj["event"] == "verdict" and "ts" in obj
    # unset identifiers are a VISIBLE null, not an absent key
    monkeypatch.delenv("LEDVOUCH_DEPLOYMENT_ID")
    assert json.loads(render_event({"event": "x"}))["deployment_id"] is None


@pytest.mark.anyio
async def test_file_emitter_appends_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    emitter = FileAuditEmitter(str(path))
    await emitter.emit({"event": "verdict", "verdict": "grounded"})
    await emitter.emit({"event": "posture", "posture": "open", "trigger": "t"})
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["event"] == "posture"


def test_audit_emitter_from_env_contract(monkeypatch):
    monkeypatch.delenv("LEDVOUCH_AUDIT_STREAM", raising=False)
    assert isinstance(audit_emitter_from_env(), StdoutAuditEmitter)  # documented default
    monkeypatch.setenv("LEDVOUCH_AUDIT_STREAM", "file")
    monkeypatch.delenv("LEDVOUCH_AUDIT_FILE", raising=False)
    with pytest.raises(RuntimeError, match="LEDVOUCH_AUDIT_FILE"):
        audit_emitter_from_env()
    monkeypatch.setenv("LEDVOUCH_AUDIT_STREAM", "webhook")
    monkeypatch.delenv("LEDVOUCH_AUDIT_WEBHOOK_URL", raising=False)
    with pytest.raises(RuntimeError, match="LEDVOUCH_AUDIT_WEBHOOK_URL"):
        audit_emitter_from_env()
    monkeypatch.setenv("LEDVOUCH_AUDIT_STREAM", "syslog")
    with pytest.raises(RuntimeError, match="syslog"):
        audit_emitter_from_env()


# ---- A4: healthz + metrics --------------------------------------------------


def test_healthz_exposes_posture():
    app = make_app(fail_posture="closed", mode=FLAG)
    body = TestClient(app).get("/healthz").json()
    assert body["fail_posture"] == "closed" and body["status"] == "ok"


def test_metrics_counts_requests_turns_verdicts_actions():
    sink = ObservationSink()
    normal = {"choices": [{"message": {"role": "assistant", "content": None,
                                       "tool_calls": [{"id": "c9", "type": "function",
                                                       "function": {"name": "sql",
                                                                    "arguments": "{}"}}]}}]}
    responses = {"i": 0}

    async def fake_forward(body, headers):
        if body.get("response_format"):
            content = json.dumps({"values": [{"value": "4500", "source": "$.s1"}]})
            return 200, {"choices": [{"message": {"content": content}}]}
        responses["i"] += 1
        if responses["i"] == 1:
            return 200, normal
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": "revenue is 4500, forecast 9999"}}]}

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink,
                   mode=BLOCK, stage="C")
    client = TestClient(app)
    _post(client)  # normal turn (tool_calls)
    _post(client)  # terminal → 9999 candidate-zero → block (attribution call ran for 4500)
    m = client.get("/metrics").json()
    assert m["requests"]["total"] == 2 and m["requests"]["stream"] == 0
    assert m["turns"] == {"normal": 1, "terminal": 1}
    assert m["verdicts"] == {"ungrounded": 1}
    assert m["actions"] == {"block": 1}
    assert m["calls"]["hidden"] == 1
    assert m["upstream"]["latency_ms"]["count"] == 2
    assert m["upstream"]["errors"] == 0
