# What the ledger voucher guarantees — and what it does not

*Claims-scope document, v0 (2026-07-20; terminology finalized 2026-09-02:
the **ledger-vouching** engine, "the ledger voucher" in prose,
`ledvouch doctor`, `LEDVOUCH_*`). This document is written before any
sales material on purpose: certification value depends on claims that survive
scrutiny. If a sentence here and a sentence in a pitch ever disagree, this
document wins.*

## The guarantee, in one sentence

**The ledger voucher guarantees provenance, not correctness**: every load-bearing
value (number, id, date, price, quantity) in your agent's final answer either
traces to a source that was actually observed on the wire — a tool result, the
user's own words, or your stated policy — or is caught: flagged, refused, or
sent back to the agent to fix, per your configuration.

## What is guaranteed

- **Deterministic verification, no judge.** The ledger voucher never asks a model
  whether an answer is grounded. The ledger voucher itself enumerates, by reverse
  lookup against the observed record, every verified source each value could
  have come from; a value with no verified source cannot pass the terminal
  gate. The model is consulted only to *select* among the pre-verified sources
  for the audit trail — its answer cannot move the verdict in either direction
  (refuse-not-trust, completed: the model's say-so can neither ground a value
  nor cause a false refusal).
- **Fabricated values are caught.** A value that appears in no observed tool
  result, no user message, and no policy text cannot pass the terminal gate.
- **Head-computed values are caught.** A value the model derived in its head
  (a sum, a difference, a percentage it never obtained through a tool) has no
  observable origin and is treated as ungrounded — in `retry` mode the agent is
  pushed to compute it with a real tool so the result becomes observable.
- **Transparency when we do not intervene.** On turns where the ledger voucher does
  not enforce, the byte stream is your model's own: measured Δ=0.000 outcome
  difference with the proxy in place (tau2-bench rig, 2026-07-17), byte-identical
  pass-through re-proven on every deployment by `ledvouch doctor`.
- **Designated side-effect calls execute only with observed values.** For tools
  the operator designates as effect terminals, every load-bearing value in the
  call's **declared data fields** either traces to a source actually observed
  on the wire before the call, or the call is stopped before execution
  (block/retry) or recorded (flag), per your configuration. Fields the operator
  declares as **derivation parameters** (thresholds, filter constants — the
  model's or the user's judgment, which by nature has no observable origin) are
  recorded in the trail, never enforced on — and never used as grounding
  evidence. The data/parameter boundary is your declared configuration,
  deterministic and itself part of the auditable surface: a data value
  misdeclared as a parameter is outside the guarantee, visibly so. The ledger voucher
  judges arguments; it never uses them as grounding evidence (the reverse
  direction — self-grounding — stays closed), and it never rewrites them: a
  call ships exactly as the model wrote it, or not at all.
- **An audit trail you own.** Every verdict and every intervention is emitted
  as a structured event to a destination you control. The ledger voucher stores
  nothing.

Measured recall on the reference rigs: in the Northwind measurement the
ledger voucher, seeing **only the wire**, reached the same verdicts as a privileged
oracle with database access — **9/9 ungrounded reports caught**
(internal measurement record, 2026-07-17); on the induction rig
(weak model, fabrication-prone conditions) it caught **814/814 fabricated-value
tokens across 159 episodes** and block mode shipped **zero unverified values in
16/16 live runs** (internal measurement record, 2026-07-21).

## What is NOT guaranteed

- **Correctness of your tools and data.** If the database is wrong, a grounded
  answer is faithfully wrong. Out of scope by design.
- **Correctness of source *choice*.** A value can be grounded to a real
  observation that a careful human would call the wrong one (e.g. the right
  number from the wrong row). The provenance tree exposes this for review; the
  ledger voucher does not auto-adjudicate intent.
- **Unreported reasoning.** Judgments the model makes but never states as
  values, and content outside the wire, are outside the ledger voucher's sight.
- **Non-JSON tool outputs verify at reduced precision.** Values inside prose
  tool outputs are matched by substring scope, not exact path resolution
  (recorded as `degraded` in the audit trail — visible, not silent).
- **Derivation parameters inside tool arguments** (an analyst constant, a SQL
  bucketing expression the model wrote) are **recorded** in the provenance tree
  but not enforced on — except on designated effect terminals, where
  enforcement gates the **whole call** (declared data fields must ground;
  declared parameter fields stay recorded-only) and never rewrites arguments:
  rewriting a model's tool arguments is fail-dangerous and is deliberately not
  implemented.
- **Whether the call should have been made.** The gate verifies where argument
  values came from — not tool choice, not authorization (access control is your
  gateway's plane), not whether a value grounded to a real observation is the
  *right* observation. A value copied from the wrong-but-real row executes.
- **Side effects that do not ride the LLM wire.** A call made by agent code
  directly, bypassing the model, is outside the ledger voucher's sight: absence of
  evidence, recorded as such — never silently presented as verified.
- **Decimal literals under tokenizer v1 match at fragment granularity.** The
  v1 sentence splitter runs before tokenization, so a decimal literal such as
  `1234.567` is audited as the fragments `1234` and `567`, not as one number.
  Detection of head-computed decimals still fires on their fragments (the
  refusal direction is unaffected), but a fabricated decimal can pass when
  **both** of its fragments independently ground somewhere in the observed
  record — a coincidence surface analogous to, and weaker-guarded than, the
  numbering-license conjunction. `LEDVOUCH_TOKENIZER=v2` (versioned option,
  default v1) audits decimal literals whole and closes this surface; the
  version in force is deployment configuration, shown by `ledvouch doctor`,
  and published measurement numbers are stated at the granularity of the
  version that measured them. **v2's own refuse-side residual:** dotted document
  numbering ("Section 2.3", hierarchical heading runs) becomes a whole value
  token that by nature appears in no tool output and carries no alphabetic
  prefix for the numbering-scheme license to anchor on — an over-refusal
  surface (under v1 it passed silently by fragment coincidence). It is also
  the one shape `retry` cannot repair: the missing set cannot shrink, so the
  rounds are spent and the run degrades to flag (bounded, never silent).
  **Operational guidance:** report formats that mandate dotted structure
  should run `flag` for those flows, or steer the report contract toward
  alpha-prefixed labels the license does cover (`H1`, `S2` — no dot). A
  position-scoped heading license is a designed extension, not a shipped
  capability.
- **Document-numbering labels** (`H1`/`H2`/… section labels that a report
  contract mandates) are **not exempt from checking** — there is no tokenizer
  blind spot. They ground through a *numbering-scheme license* computed
  deterministically from the wire, all three conditions at once: (1) the
  contract text instantiates the scheme (two or more same-prefix numbers
  including 1); (2) the answer's own numbering forms the consecutive run
  containing the label; (3) no observed tool result uses the same shape as
  data — if the environment speaks in that shape, the license is disabled for
  the conversation (recorded, with the counter-evidence named) and the label is
  checked like any value. Every grant is visible in the provenance trail as
  `contract-numbering`; the guard only ever errs toward refusal, never toward
  acceptance.
  **Residual limit, stated plainly:** a fabricated label-shaped value can pass
  as structure only when all three hold together — the contract anchors the
  scheme, the model fabricates the full consecutive run, and no tool result in
  the conversation ever shows that shape as data. In a domain whose real
  identifiers share the shape, any actual tool query surfaces the shape and
  disarms the license; what remains is trail-visible, never silent.

## The residual-refusal limit (measured, stated plainly)

Spelling-level false refusals were driven to near zero on the reference rig
(dangling refs 251 → 3 across three tightening rounds). The
**semantic-attribution family** — the model naming a wrong or absent source
for a value it demonstrably observed — was then removed structurally
(2026-07-21, candidate-path enum): the ledger voucher enumerates the verified
sources itself, so no refusal depends on the model's attribution diligence.
Measured: verdicts identical under a worst-case attributor across 191 replayed
episodes (0 divergence); on the induction rig's clean controls, false blocks
fell 23/32 → 14/32 on deterministic replay, and in live block-mode runs 13/16
blocks were of genuinely unverifiable answers, 2/16 clean answers shipped
untouched, 1/16 was a false block of the residual families below.

The refusals that remain — *correct under the guarantee* (the value as written
genuinely lacks a verified origin), but ones a human reviewer may judge benign:

- **Representation mismatch and fused spellings** (`609k` for an observed
  609,283; `23.09%` vs `0.2309`; `0-10` range spellings): the stated form
  appears in no observed source. In `retry` mode the agent is pushed to obtain
  the stated representation through a tool.
- **Derivation parameters** the model itself wrote into tool arguments
  (analyst constants, bucketing bounds) refuse when they surface in the
  answer — deliberately not an origin, since a model could otherwise launder a
  fabricated value through its own tool call. Recorded in the provenance tree.
- **Non-JSON tool outputs** still verify at reduced precision (`degraded`,
  unchanged).

In `retry` mode the convergence check keys on this deterministic refusal set,
so non-convergence reflects the model's inability to ground a value with its
tools — never attribution noise (non-convergence degrades to `flag`, never
worse). We publish per-domain measurements rather than a single precision
number because the residual families are domain-dependent. Current reference
numbers come from the internal measurement records of 2026-07-17 (tau2-bench
transparency; Northwind), 2026-07-21 (induction rig) and 2026-07-20 (load and
overhead); the records are retained internally and the headline numbers are
restated in this document.

## What enforcement costs (measured)

- Completion: enforcement did not degrade task completion on the reference rig
  (flag/retry arms ≥ the no-proxy arm across both rounds; differences within
  noise).
- Calls: terminal verification adds one hidden call per value-bearing terminal
  turn (2.0× upstream calls on that turn); a retry push-back round adds one
  more (3.0×). Spectator turns add zero.
- Latency: ledger voucher-added overhead is tens of milliseconds per governed
  terminal turn on the reference load rig — in production, model latency
  dominates. Streaming deep enforcement necessarily buffers the final turn
  (the verdict must precede the first shipped byte); intermediate tool-call
  turns stream through untouched.

## Configuration honesty

- `flag` never alters responses; `block` sacrifices completion for refusal;
  `retry` preserves completion by pushing the agent to ground its own answer.
  All three guarantee the same provenance; they differ in what happens to
  completion.
- `LEDVOUCH_FAIL_POSTURE` is your choice and is mandatory: `open` ships
  unverified answers when the ledger voucher itself cannot verify; `closed` refuses
  them. Every posture activation is audit-logged.
