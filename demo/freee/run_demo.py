#!/usr/bin/env python3
"""Write-gate demo — deterministic mechanism demo (effect gate E1, freee domain pack).

# What is REAL vs MOCKED (the boundary is explicit by design — sales/publication
# discipline: current capability and mock must never blur):
#   REAL   — the ledger voucher: the exact shipped code path (make_app). Judgment,
#            block/retry enforcement, the D5 synthetic push-back, audit events,
#            receipt correlation — everything the demo shows the ledger voucher doing
#            is the production code doing it.
#   MOCKED — the agent model (scripted turns; no LLM call) and the freee side
#            (freee-mcp execution simulated from an in-file fixture; no freee
#            connection, no OAuth, no real books).
#
# Three scenarios over the same conversation (register a bank line as a deal):
#   1. copy-type write, grounded      -> ships verbatim; receipt correlates the
#                                        freee deal id to the write-time verdict
#   2. fabricated amount, mode=retry  -> gate pushes back BEFORE execution (D5);
#                                        the model's own corrected call ships
#   3. fabricated amount, mode=block  -> honest refusal reaches the harness;
#                                        nothing executes
#
# Run:  .venv/bin/python demo/freee/run_demo.py
# Deterministic: no network, no keys; same output every run (timestamps aside).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from ledvouch.conformance import _asgi_request  # in-process ASGI driver
from ledvouch.effect_gate import parse_effect_terminals
from ledvouch.proxy import make_app

PACK_PATH = pathlib.Path(__file__).resolve().parents[2] / "packs/freee/effect_terminals.json"

# ---- the mocked freee side (fixture; no freee connection) -------------------

WALLET_TXNS = {"wallet_txns": [
    {"id": 501, "date": "2026-08-05", "amount": 15400,
     "description": "AWSクラウド利用料 8月分"},
]}
DEAL_ID = 4470123

TOOLS = [
    {"type": "function", "function": {"name": "freee_api_get"}},
    {"type": "function", "function": {"name": "freee_api_post"}},
]


def execute_freee_mock(call: dict) -> str:
    """The freee-mcp execution, simulated. Reads answer from the fixture;
    writes answer with a deal id (the receipt the real API would return)."""
    name = call["function"]["name"]
    args = json.loads(call["function"]["arguments"])
    if name == "freee_api_get":
        return json.dumps(WALLET_TXNS, ensure_ascii=False)
    if name == "freee_api_post":
        amount = (args.get("body") or {}).get("amount")
        return json.dumps({"deal": {"id": DEAL_ID, "amount": amount,
                                    "status": "settled"}}, ensure_ascii=False)
    raise AssertionError(f"unexpected tool in demo: {name}")


# ---- the mocked agent model (scripted turns; no LLM) ------------------------

def tool_call_response(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": "m", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments,
                                                             ensure_ascii=False)}}]}}]}


def text_response(content: str) -> dict:
    return {"id": "m", "choices": [{"message": {"role": "assistant",
                                                "content": content}}]}


def read_turn() -> dict:
    return tool_call_response("r1", "freee_api_get", {
        "service": "accounting", "path": "/api/1/wallet_txns",
        "query": {"start_date": "2026-08-05"}})


def write_turn(amount: int, call_id: str = "w1") -> dict:
    return tool_call_response(call_id, "freee_api_post", {
        "service": "accounting", "path": "/api/1/deals",
        "body": {"issue_date": "2026-08-05", "type": "expense",
                 "amount": amount,
                 "details": [{"amount": amount,
                              "description": "AWSクラウド利用料 8月分"}]}})


def scripted_model(turns: list[dict], retry_turns: list[dict]):
    """The agent model, scripted. Discriminates the three request kinds the
    ledger voucher can send upstream: an attribution hidden call (response_format),
    an effect-retry push-back (last message = the gate's synthetic tool
    feedback), and the agent's own next turn."""
    turns, retry_turns = list(turns), list(retry_turns)

    async def forward(body, headers):
        rf = body.get("response_format")
        if rf:  # value-map attribution call — answer "unknown" for every token
            # (the verdict is candidate EXISTENCE, deterministic; the model's
            # attribution cannot move it — this scripted laziness proves it)
            branches = rf["json_schema"]["schema"]["properties"]["values"]["items"]["anyOf"]
            mapping = [{"value": b["properties"]["value"]["enum"][0],
                        "source": "unknown"} for b in branches]
            return 200, {"choices": [{"message": {
                "content": json.dumps({"values": mapping})}}]}
        last = (body.get("messages") or [])[-1]
        if last.get("role") == "tool" and "grounding ledger voucher" in str(last.get("content", "")):
            return 200, json.loads(json.dumps(retry_turns.pop(0)))
        return 200, json.loads(json.dumps(turns.pop(0)))

    return forward


# ---- the mocked harness loop ------------------------------------------------

GOAL = ("Register the 2026-08-05 AWS cloud usage bank line as an expense deal "
        "in freee.")


class CaptureAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)


async def run_conversation(app) -> str:
    """A minimal agent harness: send the conversation, execute tool calls
    against the freee mock, stop at a text answer. The harness never knows the
    ledger voucher exists — it just points at a base_url (here: the app in-process)."""
    messages = [
        {"role": "system", "content": "You are an accounting agent operating freee via freee-mcp."},
        {"role": "user", "content": GOAL},
    ]
    for _ in range(8):
        _status, chunks = await _asgi_request(app, {
            "model": "demo-model", "messages": messages, "tools": TOOLS})
        body = json.loads(b"".join(chunks))
        msg = body["choices"][0]["message"]
        if msg.get("tool_calls"):
            messages.append(msg)
            for call in msg["tool_calls"]:
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": execute_freee_mock(call)})
            continue
        return msg.get("content") or ""
    raise AssertionError("demo conversation did not terminate")


# ---- scenarios --------------------------------------------------------------

def print_trail(audit: CaptureAudit) -> None:
    for e in audit.events:
        kind = e.get("event")
        if kind == "effect_verdict":
            print(f"  [audit] effect_verdict  tool={e['tool']} call={e['call_id']} "
                  f"verdict={e['verdict']}"
                  + (f" missing={e['missing']}" if e.get("missing") else "")
                  + (f" param_tokens={e['param_tokens']}" if e.get("param_tokens") else ""))
        elif kind == "effect_enforcement":
            line = (f"  [audit] effect_enforcement  action={e['action']} "
                    f"calls={[(c['tool'], c['verdict']) for c in e['calls']]}")
            if e.get("degrade_reason"):
                line += f" degrade_reason={e['degrade_reason']!r}"
            print(line)
        elif kind == "effect_receipt":
            print(f"  [audit] effect_receipt  call={e['call_id']} "
                  f"verdict_at_call={e['verdict_at_call']} "
                  f"receipt_data={e.get('receipt_data')}")
        elif kind == "verdict":
            print(f"  [audit] terminal verdict={e.get('verdict')} action={e.get('action')}")


def build_app(mode: str, audit: CaptureAudit):
    return make_app(
        fail_posture="closed", mode=mode, stage="C", audit=audit,
        effect_terminals=parse_effect_terminals(PACK_PATH.read_text()),
    )


def scenario_1() -> None:
    print("\n━━ Scenario 1: grounded copy-type write (mode=retry — grounded, ships as-is)")
    audit = CaptureAudit()
    app = build_app("retry", audit)
    answer = asyncio.run(run_conversation_with(app, turns=[
        read_turn(), write_turn(15400),
        text_response(f"Registered as deal ID {DEAL_ID}. The amount is 15400 JPY."),
    ], retry_turns=[]))
    print(f"  [agent→user] {answer}")
    print_trail(audit)
    verdicts = [e for e in audit.events if e.get("event") == "effect_verdict"]
    receipts = [e for e in audit.events if e.get("event") == "effect_receipt"]
    assert verdicts and verdicts[0]["verdict"] == "grounded"
    assert receipts and receipts[0]["receipt_data"] == {"$.deal.id": str(DEAL_ID)}
    assert receipts[0]["verdict_at_call"] == "grounded"
    print("  ✓ the write executed unmodified; the freee deal id is correlated to the"
          " write-time verdict as an audit trail")


def scenario_2() -> None:
    print("\n━━ Scenario 2: fabricated amount 15800 (the bank line says 15400) —"
          " mode=retry pushes back BEFORE execution")
    audit = CaptureAudit()
    app = build_app("retry", audit)
    answer = asyncio.run(run_conversation_with(app, turns=[
        read_turn(), write_turn(15800),
        text_response(f"Registered as deal ID {DEAL_ID}. The amount is 15400 JPY."),
    ], retry_turns=[write_turn(15400, call_id="w2")]))
    print(f"  [agent→user] {answer}")
    print_trail(audit)
    actions = [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]
    assert actions == ["repair"], actions
    receipts = [e for e in audit.events if e.get("event") == "effect_receipt"]
    assert receipts[0]["call_id"] == "w2"  # the CORRECTED call executed; 15800 never did
    print("  ✓ the fabricated call never reached freee; only the model's own"
          " corrected call executed")


def scenario_3() -> None:
    print("\n━━ Scenario 3: fabricated amount 99999 — mode=block stops with an honest"
          " refusal (zero execution)")
    audit = CaptureAudit()
    app = build_app("block", audit)
    answer = asyncio.run(run_conversation_with(app, turns=[
        read_turn(), write_turn(99999),
    ], retry_turns=[]))
    print(f"  [agent→user] {answer}")
    print_trail(audit)
    actions = [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]
    assert actions == ["block"], actions
    assert not [e for e in audit.events if e.get("event") == "effect_receipt"]
    print("  ✓ the ungrounded write never executed; the refusal (naming the"
          " unverified values) reached the harness")


async def run_conversation_with(app, turns, retry_turns) -> str:
    app.state.forward = scripted_model(turns, retry_turns)
    return await run_conversation(app)


def main() -> None:
    print("write-gate demo (freee domain pack) — the ledger voucher is the real"
          " code path; the model and the freee execution are mocked")
    print(f"bank-line fixture: {json.dumps(WALLET_TXNS, ensure_ascii=False)}")
    scenario_1()
    scenario_2()
    scenario_3()
    print("\nAll scenarios completed as expected.")


if __name__ == "__main__":
    main()
