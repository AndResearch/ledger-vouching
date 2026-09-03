# freee domain pack — effect-terminal declaration for freee-mcp write tools

*Domain packs are DATA, not plugins: a designation list, a data/parameter
boundary, and receipt crosswalk paths. The mechanism they feed (effect_gate.py)
is domain-independent and identical for every pack. freee is the first domain
pack; a QuickBooks Online pack is planned next.*

*freee is a trademark of freee K.K. This project is an independent work and is
not affiliated with, sponsored, or endorsed by freee K.K.*

## What this declares

[freee](https://www.freee.co.jp/) is one of Japan's largest cloud accounting
platforms. Its official [freee-mcp](https://github.com/freee/freee-mcp) server
(client mode) exposes freee's ~270 public APIs through **generic HTTP-method tools**,
not per-endpoint tools (verified against `src/openapi/client-mode.ts`,
2026-08-17):

```
freee_api_get    { service, path, query? }             — reads (NOT designated)
freee_api_post   { service, path, body, query? }       — creates
freee_api_put    { service, path, body, query? }       — full updates
freee_api_patch  { service, path, body, query? }       — partial updates
freee_api_delete { service, path, query? }             — removals
```

This pack designates the four write verbs as effect terminals. Every write —
journal entries (`/api/1/deals`, `/api/1/manual_journals`), transfers,
invoices, anything — rides these four tool names, so the designation covers
the whole write surface with four entries and zero name inference.

## The data/parameter boundary, and why

| field | declared as | reason |
|---|---|---|
| `$.body` | **data** | The write payload — what actually lands in the ledger. Every load-bearing value (amounts, dates, ids) must trace to an observed source, or the call is gated. |
| `$.path` | parameter | Endpoint choice is **tool choice** — outside the guarantee by design (claims_scope: "whether the call should have been made" is not judged). It also tokenizes as one path-shaped token (`api/1/deals`) that legitimate calls could rarely ground — judging it would buy false blocks, not safety. Recorded in the trail, always visible. |
| `$.service` | parameter | Routing choice (`accounting` / `hr` / …) — same standing as `$.path`. |
| `$.query` | parameter | Options riding a write (pagination etc. on the follow-up read) — the model's or the user's request shaping, not written data. |

**Stated limits of this boundary (v0, honest):**

- **The resource id inside a PUT/PATCH/DELETE path** (`/api/1/deals/123`) is
  recorded, not judged. A fabricated id fails at the freee API (404 — the
  fail-safe direction); a wrong-but-real id is the misdirection family, which
  provenance cannot catch by design (provenance ≠ correctness). If a
  deployment wants ids judged, split them into the body-carrying endpoints or
  wait for a path-aware declaration form — do not silently widen this pack.
- **`freee_api_delete` carries no body**, so its verdict is `skipped` with
  `fields_absent: ["$.body"]` — visible in every event. Designating it still
  buys the receipt trail: every delete correlates to its observed result by
  `tool_call_id`.
- **Compute-type values** (consumption tax 消費税, pro-rata allocation 按分 —
  values the model derives in its head) have no observable origin: `retry` mode pushes the agent to compute
  them with a real tool; if its suite has none, the gate degrades per `degrade`
  (default `block`).

## Receipt crosswalk

`receipt_fields` are the freee-side ids extracted from the observed write
result and carried on the `effect_receipt` event — the journal-level audit
trail join (`argument provenance ↔ tool_call_id ↔ freee id`):

- one declaration spans several endpoint shapes (`$.deal.id`,
  `$.manual_journal.id`, …); only the paths the actual result carries appear
  in `receipt_data`, the rest are listed in `receipt_fields_absent` — a
  visible absence, never a guess.
- freee-mcp returns API errors as non-JSON text (`APIリクエストエラー: …`):
  every declared path lands in `receipt_fields_absent`, and the receipt still
  carries the result's content hashes.

## Usage

```bash
export LEDVOUCH_EFFECT_TERMINALS="$(cat packs/freee/effect_terminals.json)"
export LEDVOUCH_MODE=retry          # or block; flag = observation rollout
export LEDVOUCH_FAIL_POSTURE=closed # unverifiable writes never execute
```

`mode` is deliberately NOT pinned per-tool in this pack — the enforcement dial
is the deployment's governance choice (`LEDVOUCH_MODE`, or edit per-tool).
`degrade` IS pinned to `block` explicitly (an executed unverified write is
the harm itself; use per-tool `"degrade": "flag"` only for an observation
rollout, knowingly).

Demo: `demo/freee/run_demo.py` (deterministic mechanism demo — scripted model,
mocked freee execution, REAL ledger voucher code path).
