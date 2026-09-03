"""effect-terminal gate tests — E0 observation + E1 enforcement.

Observation (E0 semantics, still the flag mode's behavior): a designated call
ships exactly as the model wrote it while its declared data values are judged
(candidate-mint reverse lookup — the stage-C v3 engine over a new judged
object) and its observed result is correlated back by tool_call_id.
Enforcement (E1): an ungrounded designated call is stopped BEFORE execution
(block), or pushed back ledger voucher-internally over the D5 synthetic tool
exchange (retry) — non-convergence degrades to block (D1; per-tool "flag"
override is D2). Arguments are never rewritten; judging always runs against
the ORIGINAL request's ledger (the push-back echo must never self-ground).
Declaration is strict (fail-fast: invalid values refuse, never coerce).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ledvouch.content_hash import answer_hashes
from ledvouch.effect_gate import (
    EffectConfigError,
    EffectTerminal,
    PendingEffects,
    iter_designated_calls,
    judge_effect_call,
    parse_effect_terminals,
)
from ledvouch.ledger import Ledger, ToolRecord, build_ledger
from ledvouch.proxy import make_app

# ---- fixtures ----


def _ledger() -> Ledger:
    return Ledger(
        goal="book the September invoice for order W555",
        user_texts=["book the September invoice for order W555"],
        records=[
            ToolRecord(call_id="c1", name="fetch_invoice", arguments_raw='{"id": "W555"}',
                       output='{"amount": 4500, "issued": "2026-09-01"}'),
        ],
    )


def _terminal(**overrides) -> EffectTerminal:
    base = dict(tool="post_journal", data_fields=None, param_fields=(), kind="record")
    base.update(overrides)
    return EffectTerminal(**base)


# ---- declaration parsing (strict, fail-fast) ----


def test_unset_declaration_is_inert():
    assert parse_effect_terminals(None) == {}
    assert parse_effect_terminals("") == {}
    assert parse_effect_terminals("   ") == {}


def test_declaration_parses_fields_and_kind():
    terminals = parse_effect_terminals(json.dumps([
        {"tool": "post_journal", "param_fields": ["$.threshold"]},
        {"tool": "sql_write", "data_fields": ["$.values"], "param_fields": ["$.where"]},
        {"tool": "governed_render", "data_fields": ["$.source"], "kind": "document"},
    ]))
    assert set(terminals) == {"post_journal", "sql_write", "governed_render"}
    assert terminals["post_journal"].data_fields is None  # default: everything is data
    assert terminals["post_journal"].kind == "record"
    assert terminals["sql_write"].data_fields == ("$.values",)
    assert terminals["sql_write"].param_fields == ("$.where",)
    assert terminals["governed_render"].kind == "document"


@pytest.mark.parametrize("raw,needle", [
    ("not json", "not valid JSON"),
    ('{"tool": "t"}', "JSON list"),
    ('[42]', "objects"),
    ('[{}]', "non-empty 'tool'"),
    ('[{"tool": "t", "mode": "maybe"}]', "mode"),
    ('[{"tool": "t", "degrade": "retry"}]', "degrade"),
    ('[{"tool": "t", "surprise": 1}]', "unknown key"),
    ('[{"tool": "t"}, {"tool": "t"}]', "twice"),
    ('[{"tool": "t", "data_fields": []}]', "judge nothing"),
    ('[{"tool": "t", "data_fields": ["amount"]}]', "dot path"),
    ('[{"tool": "t", "data_fields": ["$."]}]', "dot path"),
    ('[{"tool": "t", "data_fields": ["$.a..b"]}]', "empty path segment"),
    ('[{"tool": "t", "kind": "table"}]', "kind"),
    ('[{"tool": "t", "data_fields": ["$.a"], "param_fields": ["$.a.b"]}]', "overlap"),
    ('[{"tool": "t", "data_fields": ["$.a.b"], "param_fields": ["$.a"]}]', "overlap"),
])
def test_malformed_declaration_refuses_with_actionable_message(raw, needle):
    with pytest.raises(EffectConfigError, match=".*"):
        try:
            parse_effect_terminals(raw)
        except EffectConfigError as e:
            assert needle in str(e), f"message lacks {needle!r}: {e}"
            raise


# ---- judgment (the deterministic existence verdict over declared data) ----


def test_observed_tool_value_grounds():
    j = judge_effect_call(_terminal(), call_id="w1",
                          arguments_raw='{"amount": 4500}', ledger=_ledger())
    assert j.verdict == "grounded" and j.data_tokens == ("4500",)


def test_user_given_value_grounds():
    j = judge_effect_call(_terminal(), call_id="w1",
                          arguments_raw='{"order": "W555"}', ledger=_ledger())
    assert j.verdict == "grounded"


def test_fabricated_value_is_ungrounded_with_reason():
    j = judge_effect_call(_terminal(), call_id="w1",
                          arguments_raw='{"amount": 9999}', ledger=_ledger())
    assert j.verdict == "ungrounded" and j.missing == ["9999"]
    assert "no observed origin" in j.reasons["9999"]


def test_param_field_is_recorded_never_judged():
    t = _terminal(param_fields=("$.threshold",))
    j = judge_effect_call(t, call_id="w1",
                          arguments_raw='{"amount": 4500, "threshold": 3}',
                          ledger=_ledger())
    assert j.verdict == "grounded"  # 3 did not refuse the call
    assert j.param_tokens == ("3",) and "3" not in j.missing


def test_explicit_data_fields_leave_visible_unjudged_paths():
    t = _terminal(data_fields=("$.entry",))
    j = judge_effect_call(
        t, call_id="w1",
        arguments_raw='{"entry": {"amount": 4500}, "memo": "September 4500 yen"}',
        ledger=_ledger())
    assert j.verdict == "grounded"
    assert j.unjudged_paths == ["$.memo"]  # visible absence, never silent


def test_declared_but_absent_field_is_visible():
    t = _terminal(data_fields=("$.entry", "$.lines"), param_fields=("$.threshold",))
    j = judge_effect_call(t, call_id="w1",
                          arguments_raw='{"entry": {"amount": 4500}}', ledger=_ledger())
    assert sorted(j.fields_absent) == ["$.lines", "$.threshold"]


def test_nested_and_list_arguments_flatten_to_leaves():
    j = judge_effect_call(
        _terminal(), call_id="w1",
        arguments_raw='{"lines": [{"amount": 4500}, {"amount": 9999}]}',
        ledger=_ledger())
    assert j.verdict == "ungrounded" and j.missing == ["9999"]
    assert set(j.data_tokens) == {"4500", "9999"}


def test_non_json_arguments_are_a_visible_absence():
    j = judge_effect_call(_terminal(), call_id="w1",
                          arguments_raw="amount=4500", ledger=_ledger())
    assert j.verdict == "unparseable" and "not JSON" in j.error


def test_value_free_call_is_skipped():
    j = judge_effect_call(_terminal(), call_id="w1",
                          arguments_raw='{"confirm": true}', ledger=_ledger())
    assert j.verdict == "skipped" and j.data_tokens == ()


def test_scheme_license_follows_kind_not_a_toggle():
    # D3': the contract-numbering license applies to document-kind fields (a
    # render source IS an answer-shaped document) and never to record-kind
    # fields (an API write carries data only).
    # The ellipsis contract ("H1..H5") anchors the scheme without spelling out
    # H2/H3 — so only the license can ground them (H1 also grounds via the
    # user lane as a literal substring; H2/H3 isolate the license's effect).
    ledger = Ledger(
        goal="write the report with sections H1..H5",
        user_texts=["write the report with sections H1..H5"],
    )
    args = json.dumps({"source": "## H1 intro\n## H2 findings\n## H3 close"})
    doc = judge_effect_call(_terminal(kind="document"), call_id="r1",
                            arguments_raw=args, ledger=ledger)
    rec = judge_effect_call(_terminal(kind="record"), call_id="r1",
                            arguments_raw=args, ledger=ledger)
    assert doc.verdict == "grounded", f"document kind refused structure: {doc.reasons}"
    assert rec.verdict == "ungrounded" and set(rec.missing) == {"H2", "H3"}, \
        "record kind must not take the structure license"


# ---- designated-call extraction ----


def test_iter_designated_calls_filters_by_declared_name():
    body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function",
         "function": {"name": "fetch_invoice", "arguments": "{}"}},
        {"id": "b", "type": "function",
         "function": {"name": "post_journal", "arguments": '{"amount": 1}'}},
    ]}}]}
    terminals = {"post_journal": _terminal()}
    assert list(iter_designated_calls(body, terminals)) == \
        [("b", "post_journal", '{"amount": 1}')]


# ---- receipt correlation (tool_call_id join) ----


def test_receipt_carries_write_time_stain_and_is_consumed():
    pending = PendingEffects()
    pending.register("w1", "post_journal", "ungrounded")
    output = '{"journal_id": 4470123}'
    ledger = build_ledger([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "post_journal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "w1", "content": output},
    ])
    events = pending.match(ledger)
    assert len(events) == 1
    e = events[0]
    assert e["verdict_at_call"] == "ungrounded"  # the stain survives execution
    assert e["receipt_sha_canon"] == answer_hashes(output)["sha_canon"]
    assert pending.match(ledger) == []  # one receipt per call, ever


def test_receipt_fields_extract_domain_crosswalk_data():
    # §3.4: the domain-specific part of the correlation is ONLY data riding
    # the receipt — e.g. the journal id freee returns. Declared paths that the
    # result does not carry are listed visibly (one declaration spans several
    # endpoint shapes; only the matching ones appear).
    pending = PendingEffects()
    pending.register("w1", "freee_api_post", "grounded",
                     ("$.deal.id", "$.manual_journal.id"))
    output = '{"deal": {"id": 4470123, "amount": 15400}}'
    ledger = build_ledger([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "freee_api_post", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "w1", "content": output},
    ])
    e = pending.match(ledger)[0]
    assert e["receipt_data"] == {"$.deal.id": "4470123"}
    assert e["receipt_fields_absent"] == ["$.manual_journal.id"]


def test_receipt_fields_on_non_json_result_are_visibly_absent():
    pending = PendingEffects()
    pending.register("w1", "freee_api_post", "grounded", ("$.deal.id",))
    ledger = build_ledger([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "freee_api_post", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "w1",
         "content": "APIリクエストエラー: 400 Bad Request"},
    ])
    e = pending.match(ledger)[0]
    assert e["receipt_data"] == {} and e["receipt_fields_absent"] == ["$.deal.id"]


def test_receipt_fields_declaration_parses_and_validates():
    t = parse_effect_terminals(json.dumps([
        {"tool": "freee_api_post", "data_fields": ["$.body"],
         "receipt_fields": ["$.deal.id"]}]))["freee_api_post"]
    assert t.receipt_fields == ("$.deal.id",)
    with pytest.raises(EffectConfigError, match="dot path"):
        parse_effect_terminals(json.dumps([
            {"tool": "t", "receipt_fields": ["deal.id"]}]))


def test_pending_is_bounded_with_visible_eviction():
    pending = PendingEffects(cap=2)
    for i in range(4):
        pending.register(f"w{i}", "post_journal", "grounded")
    assert len(pending) == 2 and pending.evicted == 2


def test_missing_call_id_is_not_registered():
    pending = PendingEffects()
    pending.register("", "post_journal", "grounded")
    assert len(pending) == 0


# ---- app wiring (observation only: shipped bytes are the model's own) ----


class _CaptureAudit:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _conversation():
    return [
        {"role": "user", "content": "book the September invoice for order W555"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "fetch_invoice", "arguments": '{"id": "W555"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"amount": 4500}'},
    ]


def _designated_response(arguments: str):
    return {"id": "t", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "w1", "type": "function",
                        "function": {"name": "post_journal",
                                     "arguments": arguments}}]}}]}


def _client(arguments: str, declaration: str | None):
    upstream = _designated_response(arguments)

    async def forward(body, headers):
        return 200, json.loads(json.dumps(upstream))

    audit = _CaptureAudit()
    app = make_app(
        fail_posture="open", forward=forward, audit=audit, stage="C",
        effect_terminals=parse_effect_terminals(declaration),
    )
    return TestClient(app), audit, upstream


def _post(client, messages=None):
    return client.post("/v1/chat/completions", json={
        "model": "test", "messages": messages or _conversation(),
        "tools": [{"type": "function", "function": {"name": "post_journal"}}],
    })


def test_designated_call_ships_unchanged_and_is_recorded():
    declaration = json.dumps([{"tool": "post_journal"}])
    client, audit, upstream = _client('{"amount": 9999}', declaration)
    resp = _post(client)
    assert resp.json() == upstream  # observation never alters the body
    verdicts = [e for e in audit.events if e.get("event") == "effect_verdict"]
    assert len(verdicts) == 1 and verdicts[0]["verdict"] == "ungrounded"
    effects = client.get("/metrics").json()["effects"]
    assert effects["verdicts"] == {"ungrounded": 1} and effects["pending"] == 1


def test_unconfigured_gate_is_inert_and_metrics_unchanged():
    client, audit, upstream = _client('{"amount": 9999}', None)
    resp = _post(client)
    assert resp.json() == upstream
    assert [e for e in audit.events if str(e.get("event", "")).startswith("effect_")] == []
    assert "effects" not in client.get("/metrics").json()


# ---- stage E1: enforcement ----


def _decl(**kw):
    return json.dumps([{"tool": "post_journal", **kw}])


def test_declaration_accepts_mode_and_degrade():
    t = parse_effect_terminals(_decl(mode="retry", degrade="flag"))["post_journal"]
    assert t.mode == "retry" and t.degrade == "flag"
    default = parse_effect_terminals(_decl())["post_journal"]
    assert default.mode is None and default.degrade == "block"  # D1


def _enforce_client(arguments, declaration, retry_responses=None, *,
                    posture="open", mode="flag"):
    upstream = _designated_response(arguments)
    retries = list(retry_responses or [])
    seen = []

    async def forward(body, headers):
        seen.append(json.loads(json.dumps(body)))
        last = (body.get("messages") or [])[-1]
        if last.get("role") == "tool" and "grounding ledger voucher" in str(last.get("content", "")):
            return 200, json.loads(json.dumps(retries.pop(0)))
        return 200, json.loads(json.dumps(upstream))

    audit = _CaptureAudit()
    app = make_app(
        fail_posture=posture, forward=forward, audit=audit, stage="C", mode=mode,
        effect_terminals=parse_effect_terminals(declaration),
    )
    return TestClient(app), audit, upstream, seen


def _actions(audit):
    return [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]


def test_block_stops_the_call_before_execution():
    client, audit, _, _ = _enforce_client('{"amount": 9999}', _decl(mode="block"))
    msg = _post(client).json()["choices"][0]["message"]
    assert not msg.get("tool_calls"), "an executable tool_call survived the block"
    assert "9999" in msg["content"] and "before execution" in msg["content"]
    assert _actions(audit) == ["block"]
    effects = client.get("/metrics").json()["effects"]
    assert effects["actions"] == {"block": 1}
    assert effects["pending"] == 0  # a blocked call can never produce a receipt


def test_grounded_call_ships_verbatim_under_enforcement():
    client, audit, upstream, _ = _enforce_client('{"amount": 4500}', _decl(mode="block"))
    assert _post(client).json() == upstream
    assert _actions(audit) == ["ship"]
    assert client.get("/metrics").json()["effects"]["pending"] == 1


def test_per_tool_mode_defaults_to_global_mode():
    client, audit, _, _ = _enforce_client('{"amount": 9999}', _decl(), mode="block")
    msg = _post(client).json()["choices"][0]["message"]
    assert not msg.get("tool_calls") and _actions(audit) == ["block"]


def test_pure_flag_response_keeps_e0_audit_volume():
    client, audit, upstream, _ = _enforce_client('{"amount": 9999}', _decl())
    assert _post(client).json() == upstream
    assert _actions(audit) == []  # verdict + receipt only — no enforcement event


def test_retry_ships_models_own_corrected_call():
    corrected = _designated_response('{"amount": 4500}')
    client, audit, _, seen = _enforce_client(
        '{"amount": 9999}', _decl(mode="retry"), [corrected])
    assert _post(client).json() == corrected
    # D5 synthetic tool exchange: the rejected call verbatim, answered in the
    # tool role by a refusal that never claims execution.
    tail = seen[1]["messages"][-2:]
    assert tail[0]["role"] == "assistant"
    assert tail[0]["tool_calls"][0]["function"]["arguments"] == '{"amount": 9999}'
    assert tail[1]["role"] == "tool" and tail[1]["tool_call_id"] == "w1"
    assert "NOT executed" in tail[1]["content"]
    assert _actions(audit) == ["repair"]
    assert client.get("/metrics").json()["effects"]["pending"] == 1


def test_feedback_echo_never_grounds_the_rejected_value():
    # §3.2 nail: the push-back text echoes 9999. If the retry exchange leaked
    # into the ledger, that echo would mint a candidate and the re-asserted
    # call would pass. It must NOT: the same call again is stagnant → D1 block.
    client, audit, _, _ = _enforce_client(
        '{"amount": 9999}', _decl(mode="retry"),
        [_designated_response('{"amount": 9999}')])
    msg = _post(client).json()["choices"][0]["message"]
    assert not msg.get("tool_calls") and "9999" in msg["content"]
    assert _actions(audit) == ["degrade_block"]


def test_degrade_flag_override_ships_original_visibly():
    client, audit, upstream, _ = _enforce_client(
        '{"amount": 9999}', _decl(mode="retry", degrade="flag"),
        [_designated_response('{"amount": 9999}')])
    assert _post(client).json() == upstream
    assert _actions(audit) == ["degrade_flag"]
    assert client.get("/metrics").json()["effects"]["pending"] == 1  # it WILL execute


def test_retry_keepalive_hands_read_calls_to_harness():
    read_calls = {"id": "k", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "r1", "type": "function",
                        "function": {"name": "fetch_invoice", "arguments": "{}"}}]}}]}
    client, audit, _, _ = _enforce_client(
        '{"amount": 9999}', _decl(mode="retry"), [read_calls])
    assert _post(client).json() == read_calls
    assert _actions(audit) == ["pushback"]


def test_retry_text_answer_is_governed_as_terminal():
    text = {"id": "a", "choices": [{"message": {
        "role": "assistant",
        "content": "I could not verify the amount, so I did not post the journal."}}]}
    client, audit, _, _ = _enforce_client(
        '{"amount": 9999}', _decl(mode="retry"), [text])
    body = _post(client).json()
    assert body["choices"][0]["message"]["content"] == \
        text["choices"][0]["message"]["content"]
    assert _actions(audit) == ["repair_answer"]


def test_unparseable_enforced_args_follow_posture():
    client, audit, _, _ = _enforce_client(
        "amount=9999", _decl(mode="block"), posture="closed")
    msg = _post(client).json()["choices"][0]["message"]
    assert not msg.get("tool_calls") and "verification unavailable" in msg["content"]
    assert _actions(audit) == ["posture_block"]

    client2, audit2, upstream2, _ = _enforce_client(
        "amount=9999", _decl(mode="block"), posture="open")
    assert _post(client2).json() == upstream2
    assert _actions(audit2) == ["degrade_flag"]


def test_mixed_response_is_gated_whole():
    # §3.2: no partial shipping — a read call parallel to an ungrounded write
    # is withheld with it (a surviving tool_call would be executed).
    mixed = {"id": "t", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": "r1", "type": "function",
             "function": {"name": "fetch_invoice", "arguments": "{}"}},
            {"id": "w1", "type": "function",
             "function": {"name": "post_journal", "arguments": '{"amount": 9999}'}},
        ]}}]}

    async def forward(body, headers):
        return 200, json.loads(json.dumps(mixed))

    audit = _CaptureAudit()
    app = make_app(fail_posture="open", forward=forward, audit=audit, stage="C",
                   effect_terminals=parse_effect_terminals(_decl(mode="block")))
    msg = _post(TestClient(app)).json()["choices"][0]["message"]
    assert not msg.get("tool_calls") and "before execution" in msg["content"]


def _sse_bytes(events):
    out = b""
    for e in events:
        payload = e if isinstance(e, str) else json.dumps(e)
        out += b"data: " + payload.encode() + b"\n\n"
    return out


def _stream_chunk(delta, finish=None):
    return {"id": "s", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def test_gated_stream_buffers_and_blocks():
    # §4.1 contract revision: the request offers an enforcement-mode designated
    # tool → the tool_calls stream is buffered; the ungrounded write ships as
    # a synthesized refusal stream, never as the model's call bytes.
    raw = _sse_bytes([
        _stream_chunk({"role": "assistant", "content": ""}),
        _stream_chunk({"tool_calls": [{"index": 0, "id": "w1", "type": "function",
                                       "function": {"name": "post_journal",
                                                    "arguments": '{"amount": 9999}'}}]}),
        _stream_chunk({}, finish="tool_calls"),
        "[DONE]",
    ])

    async def stream_forward(body, headers):
        async def chunks():
            yield raw[:40]
            yield raw[40:]
        return 200, {"content-type": "text/event-stream"}, chunks()

    app = make_app(fail_posture="open", stream_forward=stream_forward, stage="C",
                   effect_terminals=parse_effect_terminals(_decl(mode="block")))
    resp = TestClient(app).post("/v1/chat/completions", json={
        "model": "test", "messages": _conversation(), "stream": True,
        "tools": [{"type": "function", "function": {"name": "post_journal"}}],
    })
    text = resp.text
    assert "before execution" in text, "gated stream shipped without enforcement"
    assert '"tool_calls"' not in text, "an executable tool_call survived in the stream"


def test_receipt_emitted_on_followup_request():
    declaration = json.dumps([{"tool": "post_journal"}])
    client, audit, upstream = _client('{"amount": 4500}', declaration)
    _post(client)
    followup = [
        *_conversation(),
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "post_journal", "arguments": '{"amount": 4500}'}}]},
        {"role": "tool", "tool_call_id": "w1", "content": '{"journal_id": 88}'},
    ]
    _post(client, messages=followup)
    receipts = [e for e in audit.events if e.get("event") == "effect_receipt"]
    assert len(receipts) == 1
    assert receipts[0]["call_id"] == "w1"
    assert receipts[0]["verdict_at_call"] == "grounded"
    # the fake upstream answers the follow-up with the same designated call, so
    # w1 is re-judged and re-registered: receipts counted, one pending again.
    effects = client.get("/metrics").json()["effects"]
    assert effects["receipts"] == 1 and effects["pending"] == 1
