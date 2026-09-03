"""stage B tests — refs resolution, provenance walk, hidden-call evaluation, and the
stage-B proxy path.

Standing constraint under test throughout: the shipped response body is UNCHANGED
(stage A transparency) — stage B only enriches the out-of-band observation.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from ledvouch.enforce import ObservationSink
from ledvouch.hidden_call import (
    build_hidden_request,
    evaluate_answer_refs,
    parse_answer_refs,
)
from ledvouch.ledger import Ledger, ToolRecord, build_ledger
from ledvouch.provenance import build_provenance, value_tokens
from ledvouch.proxy import make_app
from ledvouch.refs import GroundingError, find_refs, resolve_ref

# ---- fixtures ----


def _ledger_json() -> Ledger:
    """s1: a JSON list output; s2: a JSON object output."""
    return Ledger(
        goal="report Q3 revenue for order W555",
        user_texts=["report Q3 revenue for order W555"],
        records=[
            ToolRecord(
                call_id="c1", name="sql",
                arguments_raw='{"q": "SELECT revenue, cost"}',
                output='[{"revenue": 4500, "cost": 3600, "region": "EMEA"}]',
            ),
            ToolRecord(
                call_id="c2", name="get_order",
                arguments_raw='{"order_id": "W555"}',
                output='{"order_id": "W555", "status": "delivered"}',
            ),
        ],
    )


def _ledger_laundering() -> Ledger:
    """The head-computed tax case: profit=900 computed in the model's head (not in any
    output), consumed as tax_tool's argument; the answer reports the REAL output 180."""
    return Ledger(
        goal="report the tax on Q3 profit",
        user_texts=["report the tax on Q3 profit"],
        records=[
            ToolRecord(
                call_id="c1", name="sql", arguments_raw='{"q": "SELECT *"}',
                output='{"revenue": 4500, "cost": 3600}',
            ),
            ToolRecord(
                call_id="c2", name="tax_tool", arguments_raw='{"profit": 900}',
                output='{"tax": 180}',
            ),
        ],
    )


# ---- refs.py (B-1) ----


def test_resolve_json_strict_index_and_key():
    r = resolve_ref("$.s1.[0].revenue", _ledger_json())
    assert r.value == "4500" and r.degraded is False and r.step == 1


def test_resolve_string_value_is_bare():
    r = resolve_ref("$.s2.order_id", _ledger_json())
    assert r.value == "W555"  # no JSON quotes — textual comparison needs bare strings


def test_resolve_whole_step():
    r = resolve_ref("$.s2", _ledger_json())
    assert json.loads(r.value) == {"order_id": "W555", "status": "delivered"}


def test_dangling_step_refused():
    with pytest.raises(GroundingError, match="s9 does not exist"):
        resolve_ref("$.s9.revenue", _ledger_json())


def test_dangling_key_refused_with_available_keys():
    with pytest.raises(GroundingError, match="profit"):
        resolve_ref("$.s1.[0].profit", _ledger_json())


def test_dangling_index_refused():
    with pytest.raises(GroundingError, match=r"\[5\]"):
        resolve_ref("$.s1.[5].revenue", _ledger_json())


def test_unparsable_ref_refused():
    with pytest.raises(GroundingError, match="unparsable"):
        resolve_ref("$.x1.revenue", _ledger_json())


def test_non_json_output_degrades_to_step_scope():
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="web", arguments_raw="{}",
                   output="Sunny today, high of 25 degrees."),
    ])
    r = resolve_ref("$.s1.temperature", ledger)
    assert r.degraded is True
    assert r.value == "Sunny today, high of 25 degrees."  # whole-step substring scope


def test_find_refs_in_order():
    text = "revenue $.s1.[0].revenue vs cost $.s1.[0].cost from $.s2"
    assert find_refs(text) == ["$.s1.[0].revenue", "$.s1.[0].cost", "$.s2"]


def test_quoted_key_with_space_resolves():
    # τ retail outputs carry keys like "switch type" — quoted-key segments express
    # them (a bare-word grammar made the model's underscore guess a false dangling).
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="get_item", arguments_raw="{}",
                   output='{"options": {"switch type": "linear"}}'),
    ])
    r = resolve_ref('$.s1.options.["switch type"]', ledger)
    assert r.value == "linear" and r.degraded is False
    r2 = resolve_ref("$.s1.options.['switch type']", ledger)
    assert r2.value == "linear"
    assert find_refs('x $.s1.options.["switch type"] y') == [
        '$.s1.options.["switch type"]'
    ]


# ---- provenance walk (B-3) ----


def test_value_tokens_are_digit_bearing_only():
    toks = value_tokens("The Request total is 4,500 for order W555.")
    assert "4,500" in toks and "W555" in toks
    assert "Request" not in toks  # the stage A false-positive surface, excluded here


def test_provenance_grounded_chain():
    ledger = _ledger_json()
    report = build_provenance("revenue is 4500 for W555", ledger)
    assert report.ungrounded_answer == [] and report.laundered == []
    assert report.tree[0]["origin"] == "tool" and report.tree[0]["step"] == 1


def test_provenance_detects_compute_laundering():
    report = build_provenance("the tax is 180", _ledger_laundering())
    # 180 IS a real tool output — the stage A floor passes it. The walk digs into
    # tax_tool's arguments and finds profit=900 with no origin: laundering.
    assert report.ungrounded_answer == []
    assert [e["token"] for e in report.laundered] == ["900"]
    assert report.laundered[0]["consumed_by"] == [{"step": 2, "tool": "tax_tool"}]


def test_provenance_argument_grounded_in_prior_output_recurses():
    ledger = _ledger_laundering()
    ledger.records[1] = ToolRecord(
        call_id="c2", name="tax_tool", arguments_raw='{"profit": 4500}',
        output='{"tax": 180}',
    )  # argument copies a REAL s1 value → grounded (copy-type is not walk-detectable)
    report = build_provenance("the tax is 180", ledger)
    assert report.laundered == []


def test_provenance_user_supplied_argument_is_not_laundering():
    ledger = _ledger_laundering()
    ledger.user_texts.append("my zip is 94110")  # mid-dialogue user message
    ledger.records[1] = ToolRecord(
        call_id="c2", name="tax_tool", arguments_raw='{"zip": "94110"}',
        output='{"tax": 180}',
    )
    report = build_provenance("the tax is 180", ledger)
    assert report.laundered == []


def test_provenance_fabricated_answer_value():
    report = build_provenance("revenue is 9999", _ledger_json())
    assert report.ungrounded_answer == ["9999"]


# ---- hidden-call evaluation (B-2) ----


def test_evaluate_match_is_grounded():
    res = evaluate_answer_refs(
        "revenue is 4500", "revenue is $.s1.[0].revenue", _ledger_json()
    )
    assert res.verdict == "grounded"
    assert res.rendered == "revenue is 4500"
    assert not res.dangling and not res.bare_ungrounded and not res.ungrounded


def test_evaluate_mismatch_detected():
    # The 3200-vs-4500 case: the ref resolves to 4500, the original said
    # 3200 → 3200 is not covered by what actually resolves.
    res = evaluate_answer_refs(
        "revenue is 3200", "revenue is $.s1.[0].revenue", _ledger_json()
    )
    assert res.verdict == "ungrounded" and res.ungrounded == ["3200"]


def test_evaluate_dangling_ref_marked_never_fabricated():
    res = evaluate_answer_refs("revenue is 4500", "revenue is $.s3.revenue", _ledger_json())
    assert res.verdict == "ungrounded"
    assert res.dangling[0]["ref"] == "$.s3.revenue"
    assert "<dangling:$.s3.revenue>" in res.rendered  # visibly unresolved


def test_evaluate_bare_ungrounded_number():
    res = evaluate_answer_refs("total is 9999", "total is 9999", _ledger_json())
    assert "9999" in res.bare_ungrounded and res.verdict == "ungrounded"


def test_evaluate_literal_copy_recorded_not_refused():
    # copy-type surface (enum decision data): literal 4500 IS in evidence.
    res = evaluate_answer_refs("revenue is 4500", "revenue is 4500", _ledger_json())
    assert res.literal_copies == ["4500"]
    assert res.verdict == "grounded"  # grounded but unref'd — observed, not refused


def test_evaluate_user_stated_literal_allowed():
    res = evaluate_answer_refs("cancelled W555", "cancelled W555", _ledger_json())
    assert res.verdict == "grounded" and not res.bare_ungrounded


def test_evaluate_full_evidence_is_not_a_backdoor():
    # A value present in evidence but NOT covered by any ref must still be
    # ungrounded in direction 1 — otherwise stage B collapses into the stage A floor.
    res = evaluate_answer_refs("cost is 3600", "the cost is as computed", _ledger_json())
    assert res.ungrounded == ["3600"]


def test_parse_answer_refs_shape_enforced():
    ok = {"choices": [{"message": {"content": '{"answer_refs": "x"}'}}]}
    assert parse_answer_refs(ok) == "x"
    for bad in (
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "not json"}}]},
        {"choices": [{"message": {"content": '{"other": 1}'}}]},
    ):
        with pytest.raises(GroundingError):
            parse_answer_refs(bad)


def test_build_hidden_request_shape():
    body = {"model": "m", "temperature": 0.0,
            "messages": [{"role": "user", "content": "goal"}],
            "tools": [{"type": "function"}]}
    req = build_hidden_request(body, "revenue is 4500", _ledger_json())
    assert req["model"] == "m" and req["temperature"] == 0.0
    assert "tools" not in req  # the hidden call must never offer tools
    assert req["response_format"]["json_schema"]["strict"] is True
    assert req["messages"][-2] == {"role": "assistant", "content": "revenue is 4500"}
    assert "s1 = sql" in req["messages"][-1]["content"]  # the step legend


# ---- proxy stage-B path (fake upstream; the hidden call is the SECOND forward) ----


def _tool_conversation():
    return [
        {"role": "user", "content": "what is Q3 revenue?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT revenue"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"revenue": 4500}'},
    ]


def _stage_b_client(terminal_content: str, hidden_answer_refs: str | None,
                    hidden_status: int = 200):
    """Fake upstream: agent requests get a terminal text answer; the hidden call
    (recognized by its response_format) gets the answer_refs rewrite."""
    sink = ObservationSink()
    calls: list[dict] = []
    terminal = {"id": "t", "choices": [
        {"message": {"role": "assistant", "content": terminal_content}}]}

    async def fake_forward(body, headers):
        calls.append(copy.deepcopy(body))
        if body.get("response_format"):  # the ledger voucher's own hidden call
            content = json.dumps({"answer_refs": hidden_answer_refs})
            return hidden_status, {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "total_tokens": 120},
            }
        return 200, copy.deepcopy(terminal)

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink, stage="B")
    return TestClient(app), sink, calls, terminal


def test_stage_b_grounded_ships_unchanged_with_one_hidden_call():
    client, sink, calls, terminal = _stage_b_client(
        "revenue is 4500", "revenue is $.s1.revenue")
    resp = client.post("/v1/chat/completions",
                       json={"model": "m", "messages": _tool_conversation()})
    assert resp.json() == terminal  # body UNCHANGED — stage A transparency held
    assert len(calls) == 2  # exactly +1 hidden call
    sb = sink.observations[0].stage_b
    assert sb["verdict"] == "grounded"
    assert sb["usage"]["total_tokens"] == 120  # cost accounting for gate (d)


def test_stage_b_mismatch_flagged_but_shipped():
    client, sink, calls, terminal = _stage_b_client(
        "revenue is 3200", "revenue is $.s1.revenue")
    resp = client.post("/v1/chat/completions",
                       json={"model": "m", "messages": _tool_conversation()})
    assert resp.json() == terminal  # flag ships, never blocks in stage B
    sb = sink.observations[0].stage_b
    assert sb["verdict"] == "ungrounded" and sb["eval"]["ungrounded"] == ["3200"]


def test_stage_b_conversational_turn_skips_hidden_call():
    client, sink, calls, _ = _stage_b_client("You are welcome!", None)
    client.post("/v1/chat/completions",
                json={"model": "m", "messages": _tool_conversation()})
    assert len(calls) == 1  # no hidden call on a value-free turn
    assert sink.observations[0].stage_b["verdict"] == "skipped"


def test_stage_b_hidden_failure_never_breaks_shipping():
    client, sink, calls, terminal = _stage_b_client(
        "revenue is 4500", "irrelevant", hidden_status=500)
    resp = client.post("/v1/chat/completions",
                       json={"model": "m", "messages": _tool_conversation()})
    assert resp.json() == terminal  # our own call failing must not hurt the client
    sb = sink.observations[0].stage_b
    assert sb["verdict"] == "error" and "500" in sb["hidden_error"]


def test_stage_b_laundering_reaches_the_observation():
    msgs = [
        {"role": "user", "content": "report the tax"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT *"}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": '{"revenue": 4500, "cost": 3600}'},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "tax_tool", "arguments": '{"profit": 900}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": '{"tax": 180}'},
    ]
    client, sink, calls, terminal = _stage_b_client("the tax is 180", "the tax is $.s2.tax")
    resp = client.post("/v1/chat/completions", json={"model": "m", "messages": msgs})
    assert resp.json() == terminal
    sb = sink.observations[0].stage_b
    # eval grounded (180 really is s2's output) but the WALK catches profit=900.
    assert sb["eval"]["verdict"] == "grounded"
    assert [e["token"] for e in sb["provenance"]["laundered"]] == ["900"]
    assert sb["verdict"] == "ungrounded"


def test_stage_a_default_has_no_stage_b_payload():
    sink = ObservationSink()

    async def fake_forward(body, headers):
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": "revenue is 4500"}}]}

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink)  # stage defaults to "A"
    TestClient(app).post("/v1/chat/completions",
                         json={"model": "m", "messages": _tool_conversation()})
    assert sink.observations[0].stage_b is None
