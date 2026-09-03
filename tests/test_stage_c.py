"""stage C tests — value-map hidden call, block, and retry (keep-alive + convergence).

stage C alters the shipped body ON PURPOSE (block / repaired retry) — gated on the
stage A/B transparency proof. Every degrade path must ship the ORIGINAL body, and no
path may ship an answer the ledger voucher wrote itself.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from ledvouch.enforce import (
    BLOCK,
    FLAG,
    RETRY,
    ObservationSink,
    refusal_text,
)
from ledvouch.hidden_call import (
    _CANDIDATE_CAP,
    build_value_map_request,
    evaluate_value_map,
    mint_candidates,
    parse_value_map,
)
from ledvouch.ledger import Ledger, ToolRecord
from ledvouch.proxy import make_app
from ledvouch.refs import GroundingError

# ---- fixtures ----


def _ledger() -> Ledger:
    return Ledger(
        goal="report Q3 revenue for order W555",
        user_texts=["report Q3 revenue for order W555"],
        records=[
            ToolRecord(call_id="c1", name="sql", arguments_raw='{"q": "SELECT"}',
                       output='{"revenue": 4500, "cost": 3600}'),
        ],
    )


def _conversation():
    return [
        {"role": "user", "content": "what is Q3 revenue?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"revenue": 4500}'},
    ]


# ---- value-map mint / schema / parse / evaluate (v3: candidate-path enum) ----


def test_mint_reverse_lookup_lanes_and_determinism():
    ledger = _ledger()
    ledger.system_texts.append("Refunds arrive in 5-7 business days.")
    tokens = ("4500", "W555", "5-7", "9999")
    cands = mint_candidates(tokens, ledger)
    assert [l.source for l in cands["4500"]] == ["$.s1"]
    assert [l.source for l in cands["W555"]] == ["user"]
    assert [l.source for l in cands["5-7"]] == ["policy"]
    assert cands["9999"] == ()  # exists nowhere — the only true unverified
    assert mint_candidates(tokens, ledger) == cands  # pure / deterministic


def test_mint_non_json_step_is_degraded_lane():
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="search", arguments_raw="{}",
                   output="plain text mentioning 4500 dollars"),
    ])
    cands = mint_candidates(("4500",), ledger)
    assert [(l.source, l.degraded) for l in cands["4500"]] == [("$.s1", True)]
    vm = evaluate_value_map(("4500",), cands, [])
    assert vm.grounded == ["4500"] and vm.degraded == ["4500"]  # honesty bit kept


def test_value_map_schema_pins_per_token_candidate_enum():
    ledger = _ledger()
    tokens = ("4500", "W555", "9999")
    cands = mint_candidates(tokens, ledger)
    req = build_value_map_request({"model": "m", "messages": []},
                                  "revenue is 4500 for W555 (forecast 9999)",
                                  ledger, tokens, cands)
    branches = (req["response_format"]["json_schema"]["schema"]
                ["properties"]["values"]["items"]["anyOf"])
    by_value = {b["properties"]["value"]["enum"][0]: b["properties"]["source"]["enum"]
                for b in branches}
    # candidate-bearing tokens are pinned to their verified lanes (+ unknown);
    # the candidate-zero token is not offered — it is already missing by mint.
    assert by_value == {"4500": ["$.s1", "unknown"], "W555": ["user", "unknown"]}
    assert "tools" not in req
    # prompt lists exactly what the schema pins (request/prompt/parse one shape)
    prompt = req["messages"][-1]["content"]
    assert "- 4500: $.s1" in prompt and "- W555: user" in prompt
    assert "9999" not in prompt.split("Tool results observed")[0]


def test_value_map_schema_caps_huge_candidate_sets():
    # 20 steps all containing the token: the SCHEMA lists at most the cap; the
    # verdict is computed on the uncapped mint (can never flip grounded→missing).
    ledger = Ledger(records=[
        ToolRecord(call_id=f"c{i}", name="sql", arguments_raw="{}",
                   output='{"n": 4500}')
        for i in range(20)
    ])
    cands = mint_candidates(("4500",), ledger)
    assert len(cands["4500"]) == 20
    req = build_value_map_request({"model": "m", "messages": []}, "n is 4500",
                                  ledger, ("4500",), cands)
    enum = (req["response_format"]["json_schema"]["schema"]["properties"]
            ["values"]["items"]["anyOf"][0]["properties"]["source"]["enum"])
    assert len(enum) == _CANDIDATE_CAP + 1 and enum[-1] == "unknown"
    assert evaluate_value_map(("4500",), cands, []).verdict == "grounded"


def test_parse_value_map_shape_enforced():
    ok = {"choices": [{"message": {"content":
        '{"values": [{"value": "4500", "source": "$.s1.revenue"}]}'}}]}
    assert parse_value_map(ok) == [{"value": "4500", "source": "$.s1.revenue"}]
    for bad in (
        {"choices": [{"message": {"content": '{"values": "no"}'}}]},
        {"choices": [{"message": {"content": '{"values": [{"value": 5}]}'}}]},
    ):
        with pytest.raises(GroundingError):
            parse_value_map(bad)


def test_existence_verdict_is_model_independent():
    # The enforcement key is candidate EXISTENCE (user decision 2026-07-21): the
    # model answering unknown / omitting a mapping cannot flip the verdict in
    # either direction — B3's live-attribution-failure FP family is structurally
    # dead. Only the candidate-zero token is missing.
    ledger = _ledger()
    tokens = ("4500", "3600", "9999")
    cands = mint_candidates(tokens, ledger)
    for mapping in (
        [],                                              # attribution absent
        [{"value": "4500", "source": "unknown"},          # unknown despite lanes
         {"value": "3600", "source": "unknown"}],
        [{"value": "9999", "source": "$.s1"}],            # cannot talk 9999 in
    ):
        vm = evaluate_value_map(tokens, cands, mapping)
        assert set(vm.grounded) == {"4500", "3600"}
        assert vm.missing == ["9999"]
        assert "no observed origin" in vm.reasons["9999"]
        assert vm.verdict == "ungrounded"


def test_attribution_recorded_validated_never_enforced():
    ledger = _ledger()
    tokens = ("4500", "3600")
    cands = mint_candidates(tokens, ledger)
    vm = evaluate_value_map(tokens, cands, [
        {"value": "4500", "source": "$.s1"},           # valid selection → trail
        {"value": "3600", "source": "$.s1.cost"},      # outside the enum → invalid
    ])
    assert vm.attribution == {"4500": "$.s1"}
    assert vm.attribution_invalid == [{"value": "3600", "source": "$.s1.cost"}]
    assert vm.verdict == "grounded"  # trail quality never moves the verdict


def test_tabular_column_name_resolves_positionally():
    # The common SQL-tool output shape {"columns": [...], "rows": [[...]]} is
    # naturally referenced by column name; resolution goes via the step's own
    # columns array (measured as the dominant Northwind false-dangling family).
    from ledvouch.refs import GroundingError as GE, resolve_ref
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="sql_query", arguments_raw="{}",
                   output='{"columns": ["region", "revenue"], "rows": [["EMEA", 4500]]}'),
    ])
    assert resolve_ref("$.s1.rows.[0].revenue", ledger).value == "4500"
    assert resolve_ref('$.s1.rows.[0].["COUNT(*)"]' .replace("COUNT(*)", "region"),
                       ledger).value == "EMEA"
    with pytest.raises(GE, match="profit"):
        resolve_ref("$.s1.rows.[0].profit", ledger)  # absent column still refuses


def test_tabular_rows_omitted_and_single_row_shorthand():
    from ledvouch.refs import GroundingError as GE, resolve_ref
    one_row = Ledger(records=[
        ToolRecord(call_id="c1", name="sql_query", arguments_raw="{}",
                   output='{"columns": ["delayed_pct"], "rows": [[23.09]]}'),
    ])
    # rows-omitted index spelling and single-row column shorthand both resolve
    assert resolve_ref("$.s1.[0].delayed_pct", one_row).value == "23.09"
    assert resolve_ref("$.s1.delayed_pct", one_row).value == "23.09"
    multi = Ledger(records=[
        ToolRecord(call_id="c1", name="sql_query", arguments_raw="{}",
                   output='{"columns": ["v"], "rows": [[1], [2]]}'),
    ])
    assert resolve_ref("$.s1.[1].v", multi).value == "2"
    with pytest.raises(GE):  # multi-row bare-column is ambiguous — refused
        resolve_ref("$.s1.v", multi)


def test_dot_quoted_key_without_brackets_resolves():
    from ledvouch.refs import resolve_ref
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="get_user", arguments_raw="{}",
                   output='{"payment_methods": {"credit_card_9513926": {"last_four": "2478"}}}'),
    ])
    r = resolve_ref('$.s1.payment_methods."credit_card_9513926".last_four', ledger)
    assert r.value == "2478"


def test_user_and_policy_lanes_ground_by_existence():
    ledger = _ledger()
    ledger.system_texts.append("Refunds arrive in 5-7 business days.")
    ok = evaluate_value_map(
        ("W555", "5-7"), mint_candidates(("W555", "5-7"), ledger), []
    )
    assert set(ok.grounded) == {"W555", "5-7"} and ok.verdict == "grounded"
    # a value in NO lane refuses — no model claim involved either way
    lie = evaluate_value_map(("9-10",), mint_candidates(("9-10",), ledger), [])
    assert lie.missing == ["9-10"] and "no observed origin" in lie.reasons["9-10"]


def test_provenance_policy_text_is_legitimate_origin():
    from ledvouch.provenance import build_provenance
    ledger = _ledger()
    ledger.system_texts.append("Refunds arrive in 5-7 business days.")
    report = build_provenance("refund in 5-7 days", ledger)
    assert report.ungrounded_answer == []
    assert report.tree[0]["origin"] == "policy"


# ---- stage-C proxy harness -------------------------------------------------
#
# The fake upstream distinguishes request kinds:
#   - response_format present  -> the value-map hidden call: returns `mappings`
#     (a list consumed one per call).
#   - close-gate feedback in the last message -> a RETRY request: returns the
#     next scripted `retry_responses` entry.
#   - otherwise -> the agent's own request: returns the scripted terminal.


def _stage_c_client(terminal_content, mappings, retry_responses=(), mode=FLAG):
    sink = ObservationSink()
    calls = {"agent": 0, "hidden": 0, "retry": 0}
    mappings = list(mappings)
    retry_responses = list(retry_responses)
    terminal = {"id": "t", "choices": [
        {"message": {"role": "assistant", "content": terminal_content}}]}

    async def fake_forward(body, headers):
        if body.get("response_format"):
            calls["hidden"] += 1
            if not mappings:
                return 200, {"choices": [{"message": {"content": '{"values": []}'}}]}
            content = json.dumps({"values": mappings.pop(0)})
            return 200, {"choices": [{"message": {"content": content}}],
                         "usage": {"total_tokens": 50}}
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            calls["retry"] += 1
            return 200, copy.deepcopy(retry_responses.pop(0))
        calls["agent"] += 1
        return 200, copy.deepcopy(terminal)

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink, mode=mode, stage="C")
    return TestClient(app), sink, calls, terminal


def _post(client, tools=True):
    body = {"model": "m", "messages": _conversation()}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "sql"}}]
    return client.post("/v1/chat/completions", json=body)


GOOD_MAP = [{"value": "4500", "source": "$.s1.revenue"}]
BAD_MAP = [{"value": "3200", "source": "unknown"}]


# ---- flag (flag semantics: verdict only, body untouched) ----


def test_stage_c_flag_grounded_ships_unchanged():
    client, sink, calls, terminal = _stage_c_client("revenue is 4500", [GOOD_MAP])
    resp = _post(client)
    assert resp.json() == terminal
    payload = sink.observations[0].stage_b
    assert payload["verdict"] == "grounded" and payload["action"] == "ship"


def test_stage_c_flag_ungrounded_still_ships():
    client, sink, calls, terminal = _stage_c_client("revenue is 3200", [BAD_MAP])
    resp = _post(client)
    assert resp.json() == terminal  # flag never alters the body
    payload = sink.observations[0].stage_b
    assert payload["verdict"] == "ungrounded"
    assert payload["eval"]["missing"] == ["3200"]


# ---- block ----


def test_stage_c_block_replaces_with_honest_refusal():
    client, sink, calls, terminal = _stage_c_client(
        "revenue is 3200", [BAD_MAP], mode=BLOCK)
    resp = _post(client)
    content = resp.json()["choices"][0]["message"]["content"]
    assert "could not be verified" in content and "3200" in content
    obs = sink.observations[0]
    assert obs.shipped is False and obs.stage_b["action"] == "block"


def test_stage_c_block_grounded_ships_unchanged():
    client, sink, calls, terminal = _stage_c_client(
        "revenue is 4500", [GOOD_MAP], mode=BLOCK)
    assert _post(client).json() == terminal


def test_candidate_zero_answer_blocks_without_any_hidden_call():
    # v3: a fully-fabricated answer needs no attribution call at all — the mint
    # already proves candidate-zero; the model has nothing to select.
    client, sink, calls, _ = _stage_c_client("revenue is 3200", [], mode=BLOCK)
    resp = _post(client)
    assert "could not be verified" in resp.json()["choices"][0]["message"]["content"]
    assert calls["hidden"] == 0
    assert "no observed origin" in sink.observations[0].stage_b["eval"]["reasons"]["3200"]


def test_attribution_call_failure_never_loses_the_verdict():
    # v3: the verdict is deterministic at mint time — a failing attribution call
    # is recorded, the grounded answer still ships, and no posture fires (the
    # old "hidden-call failure → no verdict" A2 site is structurally gone).
    sink = ObservationSink()

    async def fake_forward(body, headers):
        if body.get("response_format"):
            return 500, {"error": "boom"}
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": "revenue is 4500"}}]}

    app = make_app(fail_posture="closed", forward=fake_forward, sink=sink, mode=BLOCK, stage="C")
    resp = TestClient(app).post(
        "/v1/chat/completions", json={"model": "m", "messages": _conversation()})
    assert resp.json()["choices"][0]["message"]["content"] == "revenue is 4500"
    payload = sink.observations[0].stage_b
    assert payload["verdict"] == "grounded" and payload["action"] == "ship"
    assert "hidden call failed upstream" in payload["attribution_error"]


# ---- retry (deep) ----


def test_stage_c_retry_repairs_and_ships_models_own_answer():
    repaired = {"id": "r", "choices": [
        {"message": {"role": "assistant", "content": "revenue is 4500"}}]}
    client, sink, calls, _ = _stage_c_client(
        "revenue is 3200", [BAD_MAP, GOOD_MAP], retry_responses=[repaired], mode=RETRY)
    resp = _post(client)
    assert resp.json()["choices"][0]["message"]["content"] == "revenue is 4500"
    payload = sink.observations[0].stage_b
    assert payload["action"] == "repair" and calls["retry"] == 1
    assert sink.observations[0].shipped is False  # client got the NEW answer


def test_stage_c_retry_keep_alive_returns_tool_calls():
    tool_resp = {"id": "k", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c9", "type": "function",
                        "function": {"name": "sql", "arguments": "{}"}}]}}]}
    client, sink, calls, _ = _stage_c_client(
        "revenue is 3200", [BAD_MAP], retry_responses=[tool_resp], mode=RETRY)
    resp = _post(client)
    assert resp.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "c9"
    payload = sink.observations[0].stage_b
    assert payload["action"] == "pushback" and payload["pushbacks"] == 1


def test_stage_c_retry_without_tools_degrades_to_flag():
    client, sink, calls, terminal = _stage_c_client(
        "revenue is 3200", [BAD_MAP], mode=RETRY)
    resp = _post(client, tools=False)
    assert resp.json() == terminal  # original ships, flagged
    payload = sink.observations[0].stage_b
    assert payload["action"] == "degrade_flag" and "no tools" in payload["degrade_reason"]


def test_stage_c_retry_stagnant_missing_set_degrades():
    # Retry answer repeats the SAME ungrounded value → not converging → degrade
    # (productive axis = shrink detection, not a count cap).
    same_again = {"id": "r", "choices": [
        {"message": {"role": "assistant", "content": "revenue is 3200"}}]}
    client, sink, calls, terminal = _stage_c_client(
        "revenue is 3200", [BAD_MAP, BAD_MAP], retry_responses=[same_again], mode=RETRY)
    resp = _post(client)
    assert resp.json() == terminal  # original ships
    payload = sink.observations[0].stage_b
    assert payload["action"] == "degrade_flag" and "shrink" in payload["degrade_reason"]
    assert calls["retry"] == 1  # stopped on stagnation, not on the attempt cap


def test_stage_c_retry_honest_no_value_answer_counts_as_repair():
    hedge = {"id": "r", "choices": [{"message": {
        "role": "assistant",
        "content": "I could not verify the revenue figure against the records."}}]}
    client, sink, calls, _ = _stage_c_client(
        "revenue is 3200", [BAD_MAP], retry_responses=[hedge], mode=RETRY)
    resp = _post(client)
    assert "could not verify" in resp.json()["choices"][0]["message"]["content"]
    assert sink.observations[0].stage_b["action"] == "repair"


def test_stage_c_pushback_budget_degrades():
    tool_resp = {"id": "k", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c9", "type": "function",
                        "function": {"name": "sql", "arguments": "{}"}}]}}]}
    # Vary the missing value per round so stagnation never fires; the budget must
    # be what stops the loop (backstop behind convergence detection).
    sink = ObservationSink()
    mappings = [[{"value": "3200", "source": "unknown"}],
                [{"value": "3201", "source": "unknown"}],
                [{"value": "3202", "source": "unknown"}],
                [{"value": "3203", "source": "unknown"}]]
    retries = [copy.deepcopy(tool_resp) for _ in range(3)]
    answers = ["v 3200", "v 3201", "v 3202", "v 3203"]
    round_no = {"i": 0}

    async def fake_forward(body, headers):
        if body.get("response_format"):
            content = json.dumps({"values": mappings.pop(0)})
            return 200, {"choices": [{"message": {"content": content}}]}
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            return 200, retries.pop(0)
        return 200, {"choices": [{"message": {"role": "assistant",
                                              "content": answers[round_no["i"]]}}]}

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink, mode=RETRY, stage="C")
    client = TestClient(app)
    for i in range(4):
        round_no["i"] = i
        # grow the conversation so the tracker never resets (same fingerprint)
        msgs = _conversation() + [
            {"role": "assistant", "content": f"turn {j}"} for j in range(i)
        ]
        client.post("/v1/chat/completions", json={
            "model": "m", "messages": msgs,
            "tools": [{"type": "function", "function": {"name": "sql"}}]})
    actions = [o.stage_b["action"] for o in sink.observations]
    assert actions[:3] == ["pushback", "pushback", "pushback"]
    assert actions[3] == "degrade_flag"
    assert "budget" in sink.observations[3].stage_b["degrade_reason"]


# ---- live-observed grammar/noise gaps (stage C smoke 2026-07-17) ----


def test_numeric_dict_key_dot_bare_resolves():
    from ledvouch.refs import resolve_ref
    ledger = Ledger(records=[
        ToolRecord(call_id="c1", name="get_product", arguments_raw="{}",
                   output='{"variants": {"7706410293": {"price": 269.16}}}'),
    ])
    assert resolve_ref("$.s1.variants.7706410293.price", ledger).value == "269.16"
    assert resolve_ref('$.s1.variants.["7706410293"].price', ledger).value == "269.16"


def test_missing_dollar_prefix_accepted():
    from ledvouch.refs import resolve_ref
    assert resolve_ref("s1.revenue", _ledger()).value == "4500"


def test_markdown_ordinals_are_not_value_tokens():
    from ledvouch.provenance import value_tokens
    text = ("Here are your items:\n"
            "1. **Headphones** - $342.81\n"
            "**2.** Vacuum Cleaner - $561.05\n"
            "3) Keyboard - $272.33")
    toks = value_tokens(text)
    # (decimals split at the dot — frozen Φ sentence-split behavior; both halves
    # still ground by substring against the resolved value)
    assert "342" in toks and "81" in toks and "561" in toks
    assert "1" not in toks and "2" not in toks and "3" not in toks


# ---- document-numbering license (scheme-licensing, 2026-07-21 user-approved) ----
#
# Label tokens are CHECKED like every value (no tokenizer blind spot) and ground
# through a wire-derived license: contract anchor + answer run + no data
# collision (provenance.py rationale). The guard fails toward refusal only.


def _report_ledger() -> Ledger:
    return Ledger(
        goal="Report with hypotheses numbered H1, H2, ... (at least three).",
        user_texts=["Report with hypotheses numbered H1, H2, ... (at least three)."],
        records=[ToolRecord(call_id="c1", name="sql", arguments_raw="{}",
                            output='{"revenue": 4500}')],
    )


def test_labels_are_value_tokens_not_a_blind_spot():
    from ledvouch.provenance import value_tokens
    toks = value_tokens("### H1: growth\n### H3: churn (see H2)\n2026H2 plan")
    assert "H1" in toks and "H2" in toks and "H3" in toks and "2026H2" in toks


def test_numbering_license_grounds_contract_anchored_run():
    # anchor (H1, H2 in the brief) + run (answer has H1..H3) + no collision
    ledger = _report_ledger()
    tokens = ("H1", "H2", "H3", "4500")
    cands = mint_candidates(tokens, ledger)
    assert [l.source for l in cands["H3"]] == ["contract-numbering"]
    vm = evaluate_value_map(tokens, cands, [])
    assert vm.verdict == "grounded" and "H3" in vm.grounded


def test_numbering_run_gap_is_not_licensed():
    # H4 without H3 is not a continuation of the answer's own numbering
    ledger = _report_ledger()
    tokens = ("H1", "H2", "H4")
    vm = evaluate_value_map(tokens, mint_candidates(tokens, ledger), [])
    assert vm.missing == ["H4"]


def test_numbering_no_anchor_fabricated_label_shaped_id_refused():
    # warehouse domain: no scheme in the contract — a fabricated H7 is checked
    # like any value (the old tokenizer exclusion would have silently passed it)
    ledger = Ledger(goal="where is item X stored?",
                    user_texts=["where is item X stored?"], records=[])
    vm = evaluate_value_map(("H7",), mint_candidates(("H7",), ledger), [])
    assert vm.missing == ["H7"]


def test_numbering_real_id_grounds_via_step_lane():
    ledger = Ledger(goal="where is item X stored?",
                    user_texts=["where is item X stored?"],
                    records=[ToolRecord(call_id="c1", name="lookup", arguments_raw="{}",
                                        output='{"shelf": "H7"}')])
    cands = mint_candidates(("H7",), ledger)
    assert [l.source for l in cands["H7"]] == ["$.s1"]  # data lane, not license


def test_numbering_collision_guard_disables_license_with_actionable_reason():
    # the contract mentions H1/H2 (a false anchor: they are shelf ids) AND tool
    # output speaks in the same shape — the environment's voice wins: licensing
    # is disabled, fails toward refusal, and the reason says why.
    from ledvouch.provenance import numbering_schemes
    ledger = Ledger(
        goal="shelves H1 and H2 are refrigerated; audit the stock",
        user_texts=["shelves H1 and H2 are refrigerated; audit the stock"],
        records=[ToolRecord(call_id="c1", name="lookup", arguments_raw="{}",
                            output='{"shelf": "H9"}')],
    )
    tokens = ("H1", "H2", "H3")
    vm = evaluate_value_map(tokens, mint_candidates(tokens, ledger), [],
                            numbering_schemes(tokens, ledger))
    assert vm.missing == ["H3"]  # H1/H2 still ground via the user lane
    assert "disabled" in vm.reasons["H3"] and "h9" in vm.reasons["H3"]


def test_numbering_ellipsis_anchor_and_generalized_prefix():
    # "S1..S5" ellipsis anchors ({1,5} — ≥2 distinct including 1); the rule is
    # prefix-generic (nothing hard-codes H)
    ledger = Ledger(goal="Structure the answer into scenarios S1..S5.",
                    user_texts=["Structure the answer into scenarios S1..S5."],
                    records=[])
    tokens = ("S1", "S2", "S3")
    cands = mint_candidates(tokens, ledger)
    assert [l.source for l in cands["S3"]] == ["contract-numbering"]


def test_provenance_tree_records_structure_origin():
    from ledvouch.provenance import build_provenance
    report = build_provenance("### H1: a\n### H2: b\n### H3: c (revenue 4500)",
                              _report_ledger())
    origins = {n["token"]: n["origin"] for n in report.tree}
    assert origins["H3"] == "structure" and origins["4500"] == "tool"
    assert report.ungrounded_answer == []


def test_refusal_text_is_actionable():
    text = refusal_text(["3200"], {"3200": "source unknown"})
    assert "3200" in text and "source unknown" in text


def test_block_mode_requires_stage_c():
    with pytest.raises(ValueError):
        make_app(fail_posture="open", mode=BLOCK, stage="A")


# ---- deep-mode round backstops + measured feedback wording (2026-07-21, the induction rig) ----


def test_close_gate_feedback_no_repeat_and_hypothesis_fallback():
    # Measured wording elements (induction rig: generic text repaired 0/16 on a
    # weak model; the harvest-side loop spelling these out repaired 57.5%).
    from ledvouch.enforce import close_gate_feedback

    text = close_gate_feedback(["3200"], {"3200": "source unknown"})
    assert "grounding ledger voucher" in text  # retry-marker contract (rigs key on it)
    assert "3200" in text and "source unknown" in text
    assert "Do not state any rejected value" in text
    assert "hypothesis" in text and "could not verify" in text


def test_stage_c_retry_max_configurable_backstop():
    # Six ungrounded values, strictly shrinking one per round, never grounded:
    # the stagnation rule never fires (every round is productive), so the stop
    # must be the CONFIGURED budget (retry_max=4, not the default 2).
    sink = ObservationSink()
    values = ["3200", "3201", "3202", "3203", "3204", "3205"]

    def answer_text(k: int) -> str:
        return "v " + " ".join(values[:k])

    state = {"round": 0}

    async def fake_forward(body, headers):
        if body.get("response_format"):  # v3: never reached — every value is
            raise AssertionError("candidate-zero values need no attribution call")
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            state["round"] += 1
            return 200, {"choices": [{"message": {
                "role": "assistant", "content": answer_text(6 - state["round"])}}]}
        return 200, {"choices": [{"message": {
            "role": "assistant", "content": answer_text(6)}}]}

    app = make_app(fail_posture="open", forward=fake_forward, sink=sink,
                   mode=RETRY, stage="C", retry_max=4)
    resp = TestClient(app).post("/v1/chat/completions", json={
        "model": "m", "messages": _conversation(),
        "tools": [{"type": "function", "function": {"name": "sql"}}]})
    payload = sink.observations[0].stage_b
    assert payload["action"] == "degrade_flag"
    assert "internal retry budget" in payload["degrade_reason"]
    assert payload["n_retry_calls"] == 4
    # degrade ships the ORIGINAL body, never a ledger voucher-written one
    assert resp.json()["choices"][0]["message"]["content"] == answer_text(6)


def test_create_app_reads_round_backstop_env(monkeypatch):
    from ledvouch.proxy import create_app

    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "open")
    monkeypatch.setenv("LEDVOUCH_RETRY_MAX", "6")
    monkeypatch.setenv("LEDVOUCH_PUSHBACK_MAX", "5")
    app = create_app()
    assert app.state.retry_max == 6
    assert app.state.pushback_max == 5


def test_create_app_refuses_malformed_round_env(monkeypatch):
    from ledvouch.proxy import create_app

    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "open")
    monkeypatch.setenv("LEDVOUCH_RETRY_MAX", "six")
    with pytest.raises(RuntimeError, match="LEDVOUCH_RETRY_MAX"):
        create_app()
