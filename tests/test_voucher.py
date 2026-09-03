"""stage A tests — Φ floor, ledger bookkeeping, and the spectator proxy's transparency.

The proxy tests inject a FAKE upstream (no network) and assert:
  - normal turns (tool_calls) pass through byte-identical;
  - terminal turns run the floor and record an out-of-band observation;
  - the response BODY handed back is UNCHANGED (the transparency guarantee).
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from ledvouch.enforce import FLAG, ObservationSink
from ledvouch.grounding import (
    INSUFFICIENT,
    SUFFICIENT,
    grounded,
    load_bearing_tokens,
    sufficiency_peek,
)
from ledvouch.ledger import build_ledger
from ledvouch.proxy import make_app

# ---- Φ floor (copied pure functions must behave exactly as the shell's) ----


def test_load_bearing_tokens_picks_values_not_prose():
    toks = load_bearing_tokens("The revenue was 4,500 and W123 shipped.")
    assert "4,500" in toks
    assert "W123" in toks
    assert "The" not in toks  # sentence-initial capital is not load-bearing


def test_grounded_floor_matches_and_misses():
    ok, missing = grounded("revenue 4500", ["observed: [{'revenue': 4500}]"])
    assert ok and missing == ()
    ok2, missing2 = grounded("revenue 9999", ["observed: [{'revenue': 4500}]"])
    assert not ok2 and "9999" in missing2


def test_thousands_separator_normalized():
    ok, _ = grounded("total 1,234", ["value 1234 recorded"])
    assert ok


def test_sufficiency_grounded_is_sufficient():
    s = sufficiency_peek(
        goal="report Q3 revenue", answer="4500", evidence=["[{'revenue': 4500}]"],
        progress_possible=True,
    )
    assert s.verdict == SUFFICIENT


def test_sufficiency_ungrounded_is_insufficient_with_actionable_reason():
    s = sufficiency_peek(
        goal="report Q3 revenue", answer="the revenue is 3200",
        evidence=["[{'revenue': 4500}]"], progress_possible=True,
    )
    assert s.verdict == INSUFFICIENT
    assert "3200" in s.missing
    assert "grounding floor" in s.reason  # actionable feedback carried


def test_goal_text_grounds_restated_values():
    # Restating an Env-given value is not fabrication (goal joins the corpus).
    s = sufficiency_peek(
        goal="cancel order W555", answer="cancelled W555", evidence=[],
        progress_possible=True,
    )
    assert s.verdict == SUFFICIENT


# ---- ledger bookkeeping ----


def _tool_conversation():
    return [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "what is Q3 revenue?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "sql", "arguments": '{"q": "SELECT revenue"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "[{\"revenue\": 4500}]"},
    ]


def test_build_ledger_records_goal_and_tool_output_with_args():
    ledger = build_ledger(_tool_conversation())
    assert ledger.goal == "what is Q3 revenue?"
    assert ledger.evidence() == ['[{"revenue": 4500}]']
    rec = ledger.by_call_id("call_1")
    assert rec is not None
    assert rec.name == "sql"
    assert rec.arguments_raw == '{"q": "SELECT revenue"}'  # verbatim, for stage B tree


def test_build_ledger_does_not_mutate_messages():
    msgs = _tool_conversation()
    snapshot = copy.deepcopy(msgs)
    build_ledger(msgs)
    assert msgs == snapshot


# ---- proxy spectator (fake upstream, no network) ----


def _client_with_upstream(upstream_response, mode=FLAG):
    sink = ObservationSink()

    async def fake_forward(body, headers):
        return 200, copy.deepcopy(upstream_response)

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink, mode=mode)
    return TestClient(app), sink


def test_normal_turn_passes_through_unchanged():
    upstream = {
        "id": "x",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "sql", "arguments": "{}"}}
                    ],
                }
            }
        ],
    }
    client, sink = _client_with_upstream(upstream)
    req = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat/completions", json=req)
    assert resp.status_code == 200
    assert resp.json() == upstream  # byte-identical
    assert sink.observations == []  # no terminal observation on a normal turn


def test_terminal_grounded_answer_ships_unchanged_and_records_sufficient():
    upstream = {
        "id": "y",
        "choices": [{"message": {"role": "assistant", "content": "revenue is 4500"}}],
    }
    client, sink = _client_with_upstream(upstream)
    req = {"model": "m", "messages": _tool_conversation()}
    resp = client.post("/v1/chat/completions", json=req)
    assert resp.status_code == 200
    assert resp.json() == upstream  # body UNCHANGED (transparency)
    assert len(sink.observations) == 1
    assert sink.observations[0].verdict == SUFFICIENT
    assert sink.observations[0].shipped is True


def test_terminal_ungrounded_answer_still_ships_but_is_flagged():
    # stage A flag: even a fabricated value is SHIPPED (flag, not block) — the response
    # body is unchanged; the ungrounded token is recorded out-of-band.
    upstream = {
        "id": "z",
        "choices": [{"message": {"role": "assistant", "content": "revenue is 3200"}}],
    }
    client, sink = _client_with_upstream(upstream)
    req = {"model": "m", "messages": _tool_conversation()}
    resp = client.post("/v1/chat/completions", json=req)
    assert resp.status_code == 200
    assert resp.json() == upstream  # UNCHANGED — flag ships, never blocks in stage A
    obs = sink.observations[0]
    assert obs.verdict == INSUFFICIENT
    assert "3200" in obs.missing
    assert obs.shipped is True


def test_upstream_error_is_surfaced_verbatim():
    sink = ObservationSink()

    async def failing_forward(body, headers):
        return 429, {"error": {"message": "rate limited"}}

    app = make_app(fail_posture="open", forward=failing_forward, sink=sink)
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "rate limited"
    assert sink.observations == []  # no floor on a non-200
