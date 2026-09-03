"""tokenizer v2 tests — decimal literals audited whole (versioned option, 2026-08-27).

Contract under test: v2 differs from v1 ONLY where a period sits between digits
(digits.digits stays one token); everywhere else the two versions are
token-for-token identical. The version is deployment configuration
(LEDVOUCH_TOKENIZER, default v1) — a malformed value refuses startup, and the
Φ COPY (grounding.py) is untouched.
"""

from __future__ import annotations

import pytest

from ledvouch.conformance import shape_checks
from ledvouch.ledger import Ledger, ToolRecord
from ledvouch.provenance import build_provenance, tokenizer_version, value_tokens


def _by_id(checks):
    return {c.id: c for c in checks}


# ---- the v1 fragment surface vs v2 whole literals --------------------------


def test_v1_splits_decimals_v2_keeps_them_whole():
    # The three spellings confirmed live 2026-08-27 (decimal-gap handoff).
    assert value_tokens("total 1234.567 units", version="v1") == ("1234", "567")
    assert value_tokens("total 1234.567 units", version="v2") == ("1234.567",)
    assert value_tokens("price 88,665.55 USD", version="v1") == ("88,665", "55")
    assert value_tokens("price 88,665.55 USD", version="v2") == ("88,665.55",)
    assert value_tokens("share 28.173076923076923%", version="v1") == (
        "28", "173076923076923")
    assert value_tokens("share 28.173076923076923%", version="v2") == (
        "28.173076923076923",)


def test_v2_decimal_still_ends_the_sentence_when_not_digit_digit():
    # A period NOT between digits stays a sentence break in v2 — trailing
    # periods, ellipses, and prose boundaries behave exactly as v1.
    for text in (
        "the total is 42. Next quarter follows.",
        "wait... 42 items",
        "items: 1...2",
        "end of Q3.",
    ):
        assert value_tokens(text, version="v2") == value_tokens(text, version="v1")


def test_v1_v2_identical_without_digit_digit_periods():
    # Differential contract on representative non-decimal shapes: ids, dates,
    # thousands separators, markdown ordinals, structure labels, ranges.
    for text in (
        "order W555 shipped 2026-07-12 to EMEA",
        "revenue 4,500 vs cost 3,600",
        "1. Headphones\n2. Keyboard: ord_42",
        "### H1: growth\n### H3: churn (see H2)\n2026H2 plan",
        "range 10-20, ratio a/b, id 55:7",
        "",
    ):
        assert value_tokens(text, version="v2") == value_tokens(text, version="v1")


def test_v2_multi_period_literals_stay_whole():
    # digit.digit never splits, so versions and dotted ids survive whole; the
    # token shape itself (_TOKEN_RE) always allowed interior periods.
    assert value_tokens("release v1.2 of 10.0.3", version="v2") == ("v1.2", "10.0.3")
    assert value_tokens("release v1.2 of 10.0.3", version="v1") == (
        "v1", "2", "10", "0", "3")


# ---- deployment configuration (env) ----------------------------------------


def test_default_version_is_v1(monkeypatch):
    monkeypatch.delenv("LEDVOUCH_TOKENIZER", raising=False)
    assert tokenizer_version() == "v1"
    assert value_tokens("total 1234.567") == ("1234", "567")


def test_env_selects_v2(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v2")
    assert tokenizer_version() == "v2"
    assert value_tokens("total 1234.567") == ("1234.567",)


def test_explicit_version_overrides_env(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v2")
    assert value_tokens("total 1234.567", version="v1") == ("1234", "567")


def test_malformed_version_refuses(monkeypatch):
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v3")
    with pytest.raises(RuntimeError, match="LEDVOUCH_TOKENIZER"):
        tokenizer_version()
    with pytest.raises(RuntimeError, match="LEDVOUCH_TOKENIZER"):
        value_tokens("42")
    with pytest.raises(ValueError, match="unknown tokenizer version"):
        value_tokens("42", version="v3")


def test_create_app_refuses_malformed_tokenizer(monkeypatch):
    from ledvouch.proxy import create_app

    monkeypatch.setenv("LEDVOUCH_FAIL_POSTURE", "open")
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v3")
    with pytest.raises(RuntimeError, match="LEDVOUCH_TOKENIZER"):
        create_app()


# ---- doctor visibility ------------------------------------------------------


def test_doctor_shows_tokenizer_version():
    base = {"LEDVOUCH_FAIL_POSTURE": "open"}
    c1 = _by_id(shape_checks(base))["shape.tokenizer"]
    assert c1.status == "pass" and "v1" in c1.detail
    c2 = _by_id(shape_checks({**base, "LEDVOUCH_TOKENIZER": "v2"}))["shape.tokenizer"]
    assert c2.status == "pass" and "v2" in c2.detail
    c3 = _by_id(shape_checks({**base, "LEDVOUCH_TOKENIZER": "nope"}))["shape.tokenizer"]
    assert c3.status == "fail" and "v1 | v2" in c3.detail


# ---- the false-accept surface v2 exists to close ----------------------------


def test_fabricated_decimal_fragments_pass_v1_and_are_caught_by_v2(monkeypatch):
    # The audit asymmetry (2026-08-27 handoff): a fabricated decimal whose two
    # fragments EACH coincidentally ground somewhere in the observed record is
    # shipped clean under v1 (both fragments trace) and refused under v2 (the
    # whole literal 12.50 appears in no output).
    ledger = Ledger(
        goal="report the adjusted price",
        user_texts=["report the adjusted price"],
        records=[
            ToolRecord(call_id="c1", name="sql", arguments_raw="{}",
                       output='{"quantity": 12, "batch": 50}'),
        ],
    )
    answer = "the adjusted price is 12.50"
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v1")
    assert build_provenance(answer, ledger).ungrounded_answer == []
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v2")
    assert build_provenance(answer, ledger).ungrounded_answer == ["12.50"]


def test_effect_gate_judges_at_v2_granularity(monkeypatch):
    # The gate's second customer (freee/QBO live demo): tool_call arguments are
    # judged through the same value_tokens, so v2 is the gate's judging
    # granularity itself, not display. A fabricated decimal amount whose
    # fragments coincidentally ground is a WRITE shipped under v1 and stopped
    # under v2; an amount actually observed grounds whole either way.
    from ledvouch.effect_gate import EffectTerminal, judge_effect_call

    ledger = Ledger(
        goal="post the journal entry",
        user_texts=["post the journal entry"],
        records=[
            ToolRecord(call_id="c1", name="fetch_deal", arguments_raw="{}",
                       output='{"quantity": 12, "batch": 50, "unit": "88.25"}'),
        ],
    )
    terminal = EffectTerminal(tool="post_journal", data_fields=None,
                              param_fields=(), kind="record")
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v1")
    j1 = judge_effect_call(terminal, call_id="w1",
                           arguments_raw='{"amount": "12.50"}', ledger=ledger)
    assert j1.verdict == "grounded"  # the v1 false-accept surface, measured
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v2")
    j2 = judge_effect_call(terminal, call_id="w1",
                           arguments_raw='{"amount": "12.50"}', ledger=ledger)
    assert j2.verdict == "ungrounded" and j2.missing == ["12.50"]
    j3 = judge_effect_call(terminal, call_id="w2",
                           arguments_raw='{"amount": "88.25"}', ledger=ledger)
    assert j3.verdict == "grounded"


def test_observed_decimal_grounds_whole_under_v2(monkeypatch):
    # The refusal direction stays sound: a decimal actually present in a tool
    # output (with or without thousands separators) grounds as one literal.
    ledger = Ledger(
        goal="report the invoice total",
        user_texts=["report the invoice total"],
        records=[
            ToolRecord(call_id="c1", name="get_invoice", arguments_raw="{}",
                       output='{"total": "88,665.55"}'),
        ],
    )
    monkeypatch.setenv("LEDVOUCH_TOKENIZER", "v2")
    report = build_provenance("the invoice total is 88,665.55", ledger)
    assert report.ungrounded_answer == []
