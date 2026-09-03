# ledger-vouching

[![test](https://github.com/AndResearch/ledger-vouching/actions/workflows/test.yml/badge.svg)](https://github.com/AndResearch/ledger-vouching/actions/workflows/test.yml)

*(License: Apache-2.0. Python package: `ledger-vouching`, import name
`ledvouch`.)*

The reference implementation of **ledger vouching**: deterministic provenance
enforcement for LLM outputs. The **ledger voucher** is an OpenAI-compatible proxy that
enforces output grounding on **someone else's** agent loop, without touching its
code. The customer swaps one line:

```
base_url: https://api.openai.com/v1   →   http://ledvouch:4000/v1
```

The ledger voucher holds the real endpoint's URL + key and forwards every request. It is a
**spectator**: it reads the conversation carried in each request (`messages[]`),
bookkeeps every observed `role:"tool"` result, and on the **terminal turn** (the
model's final answer, no `tool_calls`) checks that the answer's load-bearing values
trace to evidence actually observed. Ungrounded values are caught deterministically —
no LLM judge.

**The guarantee is provenance, not correctness** — every load-bearing value in the
final answer either traces to a source actually observed on the wire, or is caught
(flagged, refused, or pushed back, per configuration). The ledger voucher does not
guarantee that your tools or data are right, and it is not a general
hallucination solution. The full claims authority — what is guaranteed, what is
not, and the measured limits — is [`docs/claims_scope.md`](docs/claims_scope.md);
if any other material disagrees with it, that document wins.

**Design authority:** this README and `docs/claims_scope.md` — the mechanism
summary and the claims scope are self-contained.

## light / deep — what the ledger voucher does on an ungrounded terminal answer

| mode | what it does | completion | status |
|---|---|---|---|
| **flag** (light) | record the ungrounded leaves out-of-band, ship the answer **unchanged** | 100% | implemented |
| **block** (light) | replace the answer with an honest "cannot report" | sacrificed | implemented |
| **retry** (deep) | close-gate push-back: keep the agent alive, make it fix the value, then complete | preserved | implemented |

Both light and deep guarantee **provenance** (lineage to a source), not
**correctness** (the standing nail). `flag` shipped first as a
**byte-transparent** spectator — the response body is never altered — so the
spectator's transparency was proven before any body-altering enforcement was
added (and is re-proven on every deployment by `ledvouch doctor`).

## verdict engine (stage C v3 — candidate-path enum, 2026-07-21)

On a value-bearing terminal turn the ledger voucher deterministically extracts the
answer's value tokens and **reverse-looks-up** every verified origin lane for
each token — the user's own words, the operator's policy text, and each observed
tool-result step (`$.sN`). The enforcement key is **candidate existence**: a
value with at least one verified origin lane is grounded; a value with none is
ungrounded. The verdict is computed by deterministic code alone. The model is
consulted once — a strict-schema hidden call whose per-token enums pin the
verified candidates — only to **select** the true origin for the provenance
trail; its answer cannot move the verdict in either direction, and a failed
attribution call is recorded (`attribution_error`) without losing the verdict.
Tool-call **arguments** are deliberately not an origin lane: grounding a value
to the model's own arguments would let a fabricated value launder itself
through any tool call (they stay recorded-only — see `docs/claims_scope.md`).
Contract-mandated numbering labels (`H1`/`H2`/… report sections) are not a
tokenizer blind spot either: they are checked like every value and ground
through a wire-derived **numbering-scheme license** — the contract must anchor
the scheme, the answer's own consecutive run must contain the label, and no
tool result may use the shape as data (a collision disables the license for
the conversation, recorded, failing toward refusal). Residual limit stated in
`docs/claims_scope.md`.

## streaming (A1)

`stream: true` is governed (design: spectate-passthrough + terminal-only buffering;
selection rationale in `src/ledvouch/streaming.py`):

- **tool_calls turns** pass through with **chunk-unit byte identity** (a bounded
  classification prefix is held until the first decisive delta, then flushed verbatim).
- **content turns** (terminal candidates) are buffered, judged by the same terminal
  path as non-stream, and — when unaltered — replayed verbatim (the contract is SSE
  data-payload **concatenation identity**; chunk boundaries are not guaranteed).
- **effect-gated conversations** (the request's `tools[]` offers a designated
  side-effect tool whose effective mode is `block`/`retry`): tool_calls turns are
  **buffered too** — the effect verdict must precede the first shipped byte. An
  unaltered ship replays the buffered chunks verbatim (the contract weakens to
  payload **concatenation identity** for exactly these conversations; the
  membership test is the request's own `tools[]`, never mid-stream name
  guessing). Flag-only and undesignated conversations keep the chunk-identity
  passthrough — with effect terminals configured the passthrough is tee-observed,
  chunk bytes untouched.
- **block / retry-repair / keep-alive** ship a synthesized SSE stream; hidden calls and
  retry push-backs run non-stream. TTFT cost is confined to the terminal turn — for
  deep enforcement this is unavoidable (the verdict must precede the first shipped byte).
- An unparseable stream degrades to verbatim spectator with an error observation —
  the ledger voucher's own failure never punishes the client (in enforcement modes the
  fail posture decides, A2).

## fail posture (A2 — mandatory configuration)

What happens when the **ledger voucher's own machinery fails where enforcement needed
it** (a stream cannot be parsed — no verdict; its retry call errors — an
ungrounded answer it cannot push back on) is a customer governance choice, not a
technical one. (Since the v3 verdict engine, an attribution hidden-call failure
is *not* a posture site — the verdict is deterministic at mint time and stands;
the failure is recorded.) `LEDVOUCH_FAIL_POSTURE` is
**required — the ledger voucher refuses to start without it** (a silent default would
decide governance on the customer's behalf):

- `open` — ship the model's original answer unverified (degrade to spectator).
- `closed` — refuse: an honest "verification unavailable" answer replaces the
  unverified one. Nothing unverified reaches the client.

Scope: the posture diverges only in enforcement modes (`block`/`retry`). `flag`
mode never alters the body by contract, so verification failures there stay
flagged observations. If the **main** upstream forward is unreachable there is no
answer to ship — both postures surface an honest `502`
(`ledvouch_upstream_unreachable`). Every posture-decided outcome is recorded as
an audit event.

## audit event stream (A3 — schema v1, fixed)

Every governance fact is emitted as one structured JSON event to a destination
under the customer's control. **The ledger voucher persists nothing** (stateless; the
logs are the customer's — data sovereignty). This stream is also the substrate
for certification evidence, billing true-up and future registry needs, which
is why every event carries deployment/system identifiers.

Envelope (every event):

| field | meaning |
|---|---|
| `schema` | `ledger-vouching.audit.v1` (versioned; additive changes bump the version — v1 added `sha_raw`/`sha_canon` to verdict events) |
| `ts` | UTC ISO-8601 emission time |
| `deployment_id` | `LEDVOUCH_DEPLOYMENT_ID` — this ledger voucher deployment (null when unset) |
| `system_id` | `LEDVOUCH_SYSTEM_ID` — the governed agent system (null when unset) |
| `event` | `verdict` \| `enforcement` \| `laundering` \| `posture` \| `observation` \| `effect_verdict` \| `effect_enforcement` \| `effect_receipt` |

Event payloads:

- `verdict` — every terminal turn: `mode`, `stage`, `verdict`
  (`grounded`/`ungrounded`/`skipped`/`error`/`sufficient`/`insufficient`/`stream_parse_error`),
  `action` (`ship`/`degrade_flag`/`block`/`pushback`/`repair`/`posture_block`), `missing`,
  and the answer content hashes `sha_raw`/`sha_canon` (v1).
- `enforcement` — the shipped body was altered or a keep-alive was issued:
  `action`, `missing`, `reasons`.
- `laundering` — laundered argument values detected (recorded, not enforced —
  rewriting a model's tool arguments is fail-dangerous by design): `laundered`.
- `posture` — a fail-posture decision fired: `posture`, `trigger`.
- `observation` — **opt-in** (`LEDVOUCH_AUDIT_OBSERVATION=on`, default off): one
  event per terminal turn carrying the full evidence-layer substrate — `answer`,
  `goal`, `eval` (value-map v3: candidates / grounded / missing / reasons),
  `steps` (every observed tool call: name, arguments, output), `sha_raw`/
  `sha_canon`, routing facts (`model` from the request; `upstream_base` — the
  forward URL only, never the upstream key), and wire-derived `identity`
  (`auth_key_hash` = SHA-256 of the
  client's bearer token — never the credential itself; `request_user` = the
  request's `user` field; `headers` = only the names allow-listed via
  `LEDVOUCH_IDENTITY_HEADERS`). This is the ingest substrate for an evidence
  service; with the switch off the stream is identical to earlier builds.

- `effect_verdict` — **effect-terminal gate (present only when
  `LEDVOUCH_EFFECT_TERMINALS` is configured)**: one event per
  designated side-effect tool call, judged BEFORE its result exists — the same
  candidate-mint reverse lookup as the terminal, over the call's declared data
  fields. Carries `tool`, `call_id`, `kind`, `verdict`
  (`grounded`/`ungrounded`/`skipped`/`unparseable`/`stream_unparseable`),
  `missing`/`reasons`/`degraded`/`candidates`, `data_tokens`, `param_tokens`
  (declared derivation parameters — recorded, never judged, never a lane),
  `fields_absent`/`unjudged_paths` (visible declaration gaps) and `args_sha256`.
  The verdict is the record; what happened to the call is the
  `effect_enforcement` event.
- `effect_enforcement` — enforcement was active for a response's designated
  calls (pure-flag responses emit no enforcement event — their audit volume is
  the observation stage's): `mode` (global), `action`
  (`ship`/`block`/`repair`/`repair_answer`/`pushback`/`degrade_block`/
  `degrade_flag`/`posture_block`), per-call `calls[]` (tool, call_id, effective
  mode, verdict, missing), and `degrade_reason`/`posture`/`posture_trigger`
  when applicable. A blocked call is stopped BEFORE execution; a retry ships
  the model's own corrected call (arguments are never rewritten — a call ships
  exactly as the model wrote it, or not at all).
- `effect_receipt` — the observed result of a previously judged designated call,
  correlated by `tool_call_id` (wire-native join key): `tool`, `call_id`,
  `verdict_at_call` (the write-time verdict — the stain survives even though the
  result's echoed values enter the evidence corpus from then on), the
  result's `receipt_sha_raw`/`receipt_sha_canon`, and — when the declaration
  names `receipt_fields` — `receipt_data` (extracted domain crosswalk values,
  e.g. freee's deal id) with `receipt_fields_absent` (declared paths the
  result did not carry — visible, never guessed).

Destination is selected by `LEDVOUCH_AUDIT_STREAM` (`stdout` default / `file` /
`webhook`). Audit delivery failure never breaks traffic — it degrades visibly to
stderr; the webhook emitter posts fire-and-forget.

### answer content hashes (v1 — the evidence-layer join key)

Every terminal verdict event (and the out-of-band observation) carries two
SHA-256 hex digests of the observed final answer:

- `sha_raw` — over the raw UTF-8 bytes exactly as observed (byte identity).
- `sha_canon` — over the canonicalized text: NFC, newline unification,
  per-line trailing-whitespace strip, outer blank-line strip — **these four
  steps and nothing more**. This is the paste-match key: it survives the
  newline/whitespace mangling of copy-paste while still exposing any content
  edit. A hash match proves the pasted text **is** the observed text — never
  that the text is correct (provenance ≠ correctness, unchanged).

Optionally, `LEDVOUCH_ANSWER_HASH_HEADER=on` (default **off**) adds
`X-Ledvouch-Answer-Hash: <sha_canon>` to **unaltered** terminal ships — a
header-only delta (`ledvouch doctor` re-proves it: OFF is indistinguishable
from a pre-header build; ON differs by exactly this header). The header is
never attached to a body enforcement altered — a hash must not describe bytes
the client did not receive.

## observability (A4)

- `GET /healthz` — `{status, mode, stage, fail_posture}`.
- `GET /metrics` — JSON counters: request counts (total/stream), turn counts
  (normal/terminal), verdict distribution, enforcement-action distribution,
  hidden/retry call counts, pushbacks, main-upstream error count and latency
  (count/avg/max ms). In-memory, reset on restart; scraping is customer
  infrastructure.

## configuration reference (A4 — the full env contract)

| env | required | default | meaning |
|---|---|---|---|
| `LEDVOUCH_UPSTREAM_BASE` | no | `https://api.openai.com/v1` | real endpoint base URL |
| `LEDVOUCH_UPSTREAM_KEY` | no | (none) | real endpoint API key — transit only, never stored |
| `LEDVOUCH_FAIL_POSTURE` | **yes** | — (startup refusal) | `open` \| `closed` (see A2 above) |
| `LEDVOUCH_MODE` | no | `flag` | `flag` \| `block` \| `retry` (light/deep dial) |
| `LEDVOUCH_STAGE` | no | `C` | verdict engine stage (`A`/`B` are measurement-rig stages) |
| `LEDVOUCH_RETRY_MAX` | no | `2` | deep-mode in-turn correction rounds — a backstop behind convergence detection (the stagnation rule is the primary stop) |
| `LEDVOUCH_PUSHBACK_MAX` | no | `3` | deep-mode keep-alive push-backs per conversation (backstop; push-backs consume the agent's own tool budget) |
| `LEDVOUCH_ANSWER_HASH_HEADER` | no | `off` | `on` \| `off` — `X-Ledvouch-Answer-Hash` response header on unaltered terminal ships (malformed value refuses startup) |
| `LEDVOUCH_AUDIT_OBSERVATION` | no | `off` | `on` \| `off` — emit the per-terminal-turn `observation` audit event (evidence-layer substrate) |
| `LEDVOUCH_IDENTITY_HEADERS` | no | (none) | comma-separated allow-list of client header names copied into observation-event identity material |
| `LEDVOUCH_EFFECT_TERMINALS` | no | (none — gate inert) | JSON list designating side-effect tools for the effect gate, e.g. `[{"tool": "post_journal", "param_fields": ["$.threshold"], "mode": "retry"}]`. Per entry: `tool` (wire name, exact), `data_fields` (dot paths judged for provenance; `null` = every argument), `param_fields` (derivation parameters — recorded, never judged), `kind` (`record` default \| `document` — document fields keep contract-numbering licensing), `mode` (`null` = follow `LEDVOUCH_MODE` \| `flag` \| `block` \| `retry` — per-tool enforcement), `degrade` (retry non-convergence target: `block` default \| `flag` — an explicit observation-period override), `receipt_fields` (dot paths extracted from the observed result onto the `effect_receipt` event — domain crosswalk data, e.g. freee's `$.deal.id`; absent paths are listed visibly). Malformed declarations refuse startup — nothing is coerced or ignored. Domain packs (ready-made declarations + rationale) live in `packs/` — freee (a major Japanese cloud accounting SaaS): `packs/freee/`, demo: `demo/freee/` |
| `LEDVOUCH_TOKENIZER` | no | `v1` | `v1` \| `v2` — value-token granularity for the verdict engine (terminal floor candidates, provenance walk, effect gate alike). `v1`: the sentence splitter runs before tokenization, so a decimal literal is audited as its period-delimited fragments (`1234.567` → `1234`, `567`). `v2`: a period between digits is not a sentence break, so decimal literals are audited whole — closing the measured false-accept surface where both fragments of a fabricated decimal coincidentally ground. The tokenizer is a measuring instrument: switching versions changes what published verdict counts mean, so the version is explicit deployment configuration (shown by `ledvouch doctor`), never a silent upgrade. Malformed value refuses startup |
| `LEDVOUCH_AUDIT_STREAM` | no | `stdout` | `stdout` \| `file` \| `webhook` |
| `LEDVOUCH_AUDIT_FILE` | with `file` | — | JSONL path (append-only) |
| `LEDVOUCH_AUDIT_WEBHOOK_URL` | with `webhook` | — | POST destination for audit events |
| `LEDVOUCH_DEPLOYMENT_ID` | no | null | deployment identifier stamped on every audit event |
| `LEDVOUCH_SYSTEM_ID` | no | null | governed-system identifier stamped on every audit event |

## security (A5 — the baseline, stated plainly)

- **API keys are transit-only.** The upstream key is read from env, attached to
  outbound upstream requests, and never stored, logged, or included in audit
  events. Client `Authorization` headers are not logged. The opt-in observation
  event carries only a SHA-256 **hash** of the client's bearer token (an
  irreversible join key at key granularity), never the credential; arbitrary
  client headers are copied only when explicitly allow-listed by name.
- **TLS terminates on customer infrastructure.** The ledger voucher is a sidecar /
  internal-network process and does not terminate TLS itself.
- **The proxy has no authentication of its own.** Deploy it on an internal
  network reachable only by the governed agent. **Exposing the ledger voucher to the
  public internet is unsupported.**

## conformance suite v0 — `ledvouch doctor` (B1)

```
ledvouch doctor            # human-readable, one line per check
ledvouch doctor --json     # stable v0 report for certification pipelines
ledvouch doctor --live     # + real-upstream probes (needs LEDVOUCH_DOCTOR_MODEL; costs tokens)
```

**The exit code is the verdict**: 0 = pass, 1 = fail. Every check is a
deterministic pass / fail / skip with no human judgment anywhere — whoever runs
the suite against the same deployment gets the same verdict, byte for byte (the
report carries no timestamps). That machine-verifiability is what lets
certification be independent of the sales channel.

Check groups:

- `shape.*` — deployment shape: posture set, mode/stage combination valid, audit
  destination valid and writable, upstream base URL well-formed. Env only.
- `mech.*` — hermetic mechanism probes (in-process app + canned fake upstream —
  no network, no tokens): spectator transparency (non-stream byte identity,
  stream chunk identity, buffered concat identity), enforcement behavior
  (flag / block / retry keep-alive / retry repair), fail-posture behavior
  (open / closed), audit emission, and the v3 candidate-enum verdict
  (deterministic and model-independent).
- `live.*` (opt-in) — the canned exchange against the real configured upstream:
  reachability, then transparency Δ=0. Determinism is measured first (two direct
  calls); a nondeterministic upstream yields an honest **skip** — never a guessed
  Δ (the hermetic transparency proof stands regardless).

## claims scope & support

- **`docs/claims_scope.md`** — the customer-facing claims authority (what is
  guaranteed / not guaranteed / measured limits). If sales material and this
  document disagree, this document wins.
- **`support/`** — support kit v0: `error_catalog.md` (every operator-visible
  failure, literal strings), `runbook.md` (deploy/monitor/incidents/escalation).
- Load reference: internal measurement record 2026-07-20 (upstream-call
  inflation 1.0×/2.0×/3.0× by path; ledger voucher overhead tens of ms at ≤80 RPS).

## layout

```
src/ledvouch/
  grounding.py   — Φ: the deterministic floor (COPIED from the predecessor, unchanged)
  ledger.py      — bookkeep goal + observed tool outputs (+ producing tool_call args)
  refs.py        — $.<step>.<jsonpath> resolution over the ledger (refuse, never repair)
  provenance.py  — value tokens + lineage tree + laundering walk (recorded, not enforced)
  hidden_call.py — candidate mint (reverse lookup) + attribution hidden call + verdicts
  content_hash.py— sha_raw/sha_canon of the observed answer (evidence join key)
  enforce.py     — flag / block / retry + fail-posture refusal (A2)
  effect_gate.py — effect-terminal gate: designated side-effect calls judged BEFORE execution
  proxy.py       — OpenAI-compatible /v1/chat/completions front (spectate + terminal verdict)
  streaming.py   — SSE scanner / aggregator / synthesizer (A1 streaming governance)
  audit.py       — audit event stream: schema v1 + stdout/file/webhook emitters (A3)
  metrics.py     — /metrics counters (A4)
  conformance.py — conformance suite v0: shape / mech / live checks (B1)
  cli.py         — `ledvouch doctor` (exit code = verdict)
tests/
```

## run

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
LEDVOUCH_UPSTREAM_BASE=https://api.openai.com/v1 \
LEDVOUCH_UPSTREAM_KEY=sk-... \
LEDVOUCH_FAIL_POSTURE=open \
  .venv/bin/uvicorn --factory ledvouch.proxy:create_app --port 4000
```

Point an agent's `api_base` at `http://localhost:4000/v1`. Tests: `.venv/bin/pytest`.

## measurements

Reference measurements live in the internal measurement archive
(2026-07-17 tau2-bench transparency rig arm A ≡ arm B Δ=0.000; 2026-07-17 Northwind
9/9; 2026-07-21 induction rig 814/814 · 16/16 live; 2026-07-20 load).
Headline numbers and their limits are stated in `docs/claims_scope.md`.
Benchmark data is the **fictional Northwind trading-company dataset** — no
real customer data was used in any measurement.

## commercial

The proxy is fully open (Apache-2.0) and stays a **single codebase**:
enforcement evolution lands here, in the open — there is no commercial fork of
the proxy, so every behavior that enforces in production is re-runnable by
your auditors from this repository. A working trial-grade **evidence layer**
(verification portal over this proxy's audit stream) exists as a read-only
reference implementation in a companion repository (**Okugaki** — publication
in preparation); production authentication, admin tooling, custom integration
and support are the commercial line.
