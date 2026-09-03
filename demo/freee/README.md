# Write-gate demo (deterministic, using the freee domain pack)

*The demo shows the effect gate stopping a fabricated value before a write
executes. The domain instance is freee — one of Japan's largest cloud
accounting platforms; the demo registers a bank transaction as an expense deal
through its official MCP tool shapes. freee is a trademark of freee K.K.; this
project is not affiliated with or endorsed by freee K.K.*

```bash
.venv/bin/python demo/freee/run_demo.py
```

No network, no keys, no freee account — same output every run.

## What is real vs mocked (read this first)

| layer | status |
|---|---|
| **ledger voucher** | **REAL** — the exact shipped code path (`make_app`): the effect-gate verdict, block/retry enforcement, the synthetic push-back, audit events, receipt correlation. Nothing is demo-special-cased. |
| agent model | MOCKED — scripted turns (no LLM call). The script deliberately answers the attribution call with "unknown" for every value: the verdict is deterministic candidate existence and the model's say-so cannot move it. |
| freee side | MOCKED — freee-mcp execution simulated from an in-file fixture (no OAuth, no real books). Tool names, argument shapes and response shapes follow the real [freee-mcp client mode](https://github.com/freee/freee-mcp) (`freee_api_get`/`freee_api_post` with `service`/`path`/`body`) and the freee API's `{"deal": {"id": …}}` receipt shape. |

Designation config is the real pack: [`packs/freee/effect_terminals.json`](../../packs/freee/effect_terminals.json).

## The three scenarios

1. **Grounded copy-type write** (`mode=retry`): the agent reads the bank line
   (¥15,400), writes a deal with the observed values — the call ships
   **verbatim** and the receipt correlates freee's `deal.id` to the write-time
   verdict (`effect_receipt.verdict_at_call=grounded`): the journal-level
   audit trail.
2. **Fabricated amount, retry**: the agent writes 15,800 (appears in no
   observed source). The gate stops the call **before execution** and pushes
   back ledger voucher-internally; the model's own corrected call (15,400) is the
   only write that ever executes. The harness never sees the exchange — just a
   slightly slower turn.
3. **Fabricated amount, block**: same fabrication under `mode=block` — the
   harness receives an honest refusal naming the unverifiable value; nothing
   executes, no receipt exists.

## What this demo does NOT show (honest boundary)

- A real LLM looping against a real freee test office (that is the live-demo
  follow-up; freee provides developer test offices).
- Misdirection (a real-but-wrong row's value) passing the gate — it would, by
  design: the ledger voucher guarantees provenance, not correctness
  (`docs/claims_scope.md` is the authority on the claim boundary).
- The evidence-portal rendering of these audit events (evidence-layer work,
  separate repo).
