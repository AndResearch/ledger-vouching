"""B1 tests — conformance suite v0 + `ledvouch doctor` CLI.

The suite's own contract under test: deterministic verdicts (same deployment →
byte-identical report), shape checks that catch real misconfiguration with
actionable detail, hermetic mechanism probes that all pass on a healthy build,
and a CLI whose exit code IS the verdict.
"""

from __future__ import annotations

import json

from ledvouch.cli import main
from ledvouch.conformance import SUITE, run_suite, shape_checks

GOOD_ENV = {
    "LEDVOUCH_FAIL_POSTURE": "open",
    "LEDVOUCH_MODE": "block",
    "LEDVOUCH_STAGE": "C",
    "LEDVOUCH_AUDIT_STREAM": "stdout",
    "LEDVOUCH_UPSTREAM_BASE": "https://api.openai.com/v1",
}


def _by_id(checks):
    return {c.id if hasattr(c, "id") else c["id"]: c for c in checks}


# ---- shape checks ----------------------------------------------------------


def test_shape_checks_pass_on_good_env():
    checks = _by_id(shape_checks(GOOD_ENV))
    assert all(c.status == "pass" for c in checks.values())


def test_shape_fail_posture_missing_is_actionable():
    checks = _by_id(shape_checks({}))
    c = checks["shape.fail_posture"]
    assert c.status == "fail" and "LEDVOUCH_FAIL_POSTURE" in c.detail


def test_shape_mode_stage_combination_checked():
    env = {**GOOD_ENV, "LEDVOUCH_MODE": "retry", "LEDVOUCH_STAGE": "A"}
    c = _by_id(shape_checks(env))["shape.mode_stage"]
    assert c.status == "fail" and "stage C" in c.detail
    c2 = _by_id(shape_checks({**GOOD_ENV, "LEDVOUCH_MODE": "audit"}))["shape.mode_stage"]
    assert c2.status == "fail"


def test_shape_audit_file_writability(tmp_path):
    ok = {**GOOD_ENV, "LEDVOUCH_AUDIT_STREAM": "file",
          "LEDVOUCH_AUDIT_FILE": str(tmp_path / "audit.jsonl")}
    assert _by_id(shape_checks(ok))["shape.audit_stream"].status == "pass"
    missing = {**GOOD_ENV, "LEDVOUCH_AUDIT_STREAM": "file"}
    assert _by_id(shape_checks(missing))["shape.audit_stream"].status == "fail"
    unwritable = {**GOOD_ENV, "LEDVOUCH_AUDIT_STREAM": "file",
                  "LEDVOUCH_AUDIT_FILE": str(tmp_path / "no" / "such" / "dir" / "a.jsonl")}
    c = _by_id(shape_checks(unwritable))["shape.audit_stream"]
    assert c.status == "fail" and "not writable" in c.detail


def test_shape_webhook_and_upstream_url_syntax():
    bad_hook = {**GOOD_ENV, "LEDVOUCH_AUDIT_STREAM": "webhook",
                "LEDVOUCH_AUDIT_WEBHOOK_URL": "not-a-url"}
    assert _by_id(shape_checks(bad_hook))["shape.audit_stream"].status == "fail"
    ok_hook = {**GOOD_ENV, "LEDVOUCH_AUDIT_STREAM": "webhook",
               "LEDVOUCH_AUDIT_WEBHOOK_URL": "https://audit.example/sink"}
    assert _by_id(shape_checks(ok_hook))["shape.audit_stream"].status == "pass"
    bad_base = {**GOOD_ENV, "LEDVOUCH_UPSTREAM_BASE": "localhost:4000"}
    assert _by_id(shape_checks(bad_base))["shape.upstream_base"].status == "fail"


# ---- suite -----------------------------------------------------------------


def test_suite_passes_hermetically_on_good_env():
    report = run_suite(live=False, env=GOOD_ENV)
    assert report["suite"] == SUITE and report["verdict"] == "pass"
    checks = _by_id(report["checks"])
    mech = [c for cid, c in checks.items() if cid.startswith("mech.")]
    assert len(mech) == 24 and all(c["status"] == "pass" for c in mech)
    assert checks["live.transparency_delta"]["status"] == "skip"  # no --live


def test_suite_verdict_fails_on_bad_shape_but_mech_still_reported():
    report = run_suite(live=False, env={})
    assert report["verdict"] == "fail"
    checks = _by_id(report["checks"])
    assert checks["shape.fail_posture"]["status"] == "fail"
    assert checks["mech.enforcement_block"]["status"] == "pass"  # probes still ran


def test_suite_report_is_deterministic():
    # machine-verifiability nail: same deployment → same report,
    # byte for byte — no timestamps, no environment noise.
    a = run_suite(live=False, env=GOOD_ENV)
    b = run_suite(live=False, env=GOOD_ENV)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---- CLI -------------------------------------------------------------------


def _set_env(monkeypatch, env):
    for key in ("LEDVOUCH_FAIL_POSTURE", "LEDVOUCH_MODE", "LEDVOUCH_STAGE",
                "LEDVOUCH_AUDIT_STREAM", "LEDVOUCH_AUDIT_FILE",
                "LEDVOUCH_AUDIT_WEBHOOK_URL", "LEDVOUCH_UPSTREAM_BASE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_doctor_json_exit_zero_on_pass(monkeypatch, capsys):
    _set_env(monkeypatch, GOOD_ENV)
    code = main(["doctor", "--json"])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 0 and report["verdict"] == "pass"
    assert report["suite"] == SUITE


def test_doctor_exit_one_on_misconfiguration(monkeypatch, capsys):
    _set_env(monkeypatch, {})  # posture unset → shape failure
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "shape.fail_posture" in out
    assert "verdict: fail" in out


def test_doctor_human_output_lists_every_check(monkeypatch, capsys):
    _set_env(monkeypatch, GOOD_ENV)
    main(["doctor"])
    out = capsys.readouterr().out
    for cid in ("shape.fail_posture", "mech.stream_passthrough",
                "mech.posture_closed", "live.transparency_delta"):
        assert cid in out
