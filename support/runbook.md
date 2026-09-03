# Operations runbook — ledger-vouching (support kit v0)

## 1. Deploy / verify

1. Set env per README "configuration reference". `LEDVOUCH_FAIL_POSTURE` is
   mandatory and is the **customer's** decision (open/closed) — record who chose.
2. Start: `uvicorn --factory ledvouch.proxy:create_app --port <p>`
   (sidecar / internal network only; TLS terminates outside — README security).
3. Verify: `ledvouch doctor` must print `verdict: pass` (exit 0). For the
   upstream link: `ledvouch doctor --live` (needs `LEDVOUCH_DOCTOR_MODEL`;
   costs a few tokens).
4. Point the agent's `base_url` at the ledger voucher. Nothing else changes on the
   agent side.

Any config change = env change = **process restart** (config is read at
startup; there is no hot reload — by design, the running posture is always the
audited one).

## 2. Monitor

- `GET /healthz` — `{status, mode, stage, fail_posture}`: liveness + the active
  governance posture in one read.
- `GET /metrics` — watch:
  - `verdicts` distribution drifting toward `error` → ledger voucher machinery
    failing (usually upstream flakiness on hidden calls).
  - `actions.posture_block` climbing → fail-closed firing; find `posture`
    audit events' `trigger`.
  - `calls.pushbacks` climbing → retry keep-alive volume (cost driver).
  - `upstream.latency_ms` — the model API's latency as the ledger voucher sees it.
- Audit events are the ground truth; `/metrics` is the summary.

## 3. Latency questions

Reference numbers (canned-upstream load rig, internal measurement record
2026-07-20): spectator turns add no
upstream calls (pass-through, stream included); a governed terminal turn is
2.0× upstream calls (one hidden verification call), a retry round 3.0×.
Ledger voucher-added latency was tens of ms at ≤80 RPS, no saturation observed in
that range. In production, model latency dominates. Streaming: the final turn
is buffered by design (verdict before first byte) — intermediate tool-call
turns stream through untouched; this TTFT cost is documented, not a defect.

If latency regresses: check `/metrics` `upstream.latency_ms` first (is it the
model API?), then `verdicts`/`calls` (did hidden/retry volume grow?), then the
host (CPU steal, audit sink blocking on a full disk).

## 4. Incidents

- **Ledger voucher process down**: traffic stops (the agent's base_url points at us).
  Restart it. Whether to re-point agents directly at the model API while down
  is the *deployment-level* fail-posture decision — it belongs to the customer's
  routing layer, must mirror their `open`/`closed` choice, and any bypass window
  is a governance gap that must be disclosed in audit contexts.
- **Upstream down**: clients receive honest 502 `ledvouch_upstream_unreachable`;
  `posture` audit events record the window. No ledger voucher action beyond fixing
  reachability.
- **Suspected wrong verdict** (customer disputes a flag/block): collect the
  `verdict`/`enforcement` audit events + the conversation (tool results
  included) → check against `docs/claims_scope.md` (semantic-attribution family
  is expected behavior) → if it still looks like a ledger voucher defect, escalate
  with the collected bundle (§5). **Never** hot-patch grounding logic in the
  field.

## 5. Escalation bundle (L2 → engineering)

1. `ledvouch doctor --json` output.
2. The audit events for the disputed turn(s) (from the customer's sink).
3. The request body as seen by the ledger voucher if reproducible (messages incl.
   `role:"tool"` results — that is all the ledger voucher ever sees).
4. `GET /metrics` snapshot.
5. What the customer expected vs what shipped, in one sentence.

With (2)+(3) every verdict is **replayable deterministically** — that is the
point of the design; a bundle without them is not diagnosable.

## 6. Boundaries (do not cross in the field)

- Do not edit `grounding.py` (Φ is a frozen copy), `enforce.py`, or verdict
  logic on a customer site.
- Do not add json-repair / coercion / silent defaults anywhere (the
  no-implicit-repair rule).
- Do not flip `LEDVOUCH_FAIL_POSTURE` or `LEDVOUCH_MODE` to make a symptom
  disappear without the customer's explicit, recorded decision.
- Do not promise semantic-family refusals away — claims live in
  `docs/claims_scope.md`.
