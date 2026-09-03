# Error catalog — ledger-vouching (support kit v0)

Every operator-visible failure, what it means, and what to do. Strings below are
the literal strings the code emits — grep for them. Severity: **config** (fix
your deployment), **expected** (the ledger voucher doing its job), **degraded**
(governance reduced, traffic unharmed), **outage**.

## Startup refusals (process exits; config)

| message contains | meaning | action |
|---|---|---|
| `LEDVOUCH_FAIL_POSTURE must be set to 'open' or 'closed'` | The mandatory fail-posture choice is missing/invalid. There is no default on purpose (A2). | Set `LEDVOUCH_FAIL_POSTURE`. This is the customer's governance decision — see README "fail posture"; do not pick silently on their behalf. |
| `LEDVOUCH_AUDIT_STREAM=file requires LEDVOUCH_AUDIT_FILE` | file audit sink chosen but no path. | Set `LEDVOUCH_AUDIT_FILE` to an append-writable path. |
| `LEDVOUCH_AUDIT_STREAM=webhook requires LEDVOUCH_AUDIT_WEBHOOK_URL` | webhook sink chosen but no URL. | Set the URL (http/https). |
| `unknown LEDVOUCH_AUDIT_STREAM` | typo in the sink kind. | One of `stdout` / `file` / `webhook`. |
| `mode ... requires stage='C'` | `block`/`retry` configured with stage A/B. | Set `LEDVOUCH_STAGE=C` (enforcement keys on the value-map verdict). |
| `fail_posture must be 'open' or 'closed'` | programmatic `make_app` misuse (rigs/scripts). | Pass `fail_posture=` explicitly. |

Fastest triage for any startup problem: **`ledvouch doctor`** — the `shape.*`
checks name the exact missing/invalid setting.

## HTTP responses the client sees

| symptom | meaning | severity | action |
|---|---|---|---|
| `502` with `"type": "ledvouch_upstream_unreachable"` | The MAIN forward to the model API failed. There is no answer to ship, so both postures surface this honestly. A `posture` audit event was recorded. | outage (upstream) | Check upstream endpoint/key/network (`LEDVOUCH_UPSTREAM_BASE`, `LEDVOUCH_UPSTREAM_KEY`); confirm with `ledvouch doctor --live`. Not a ledger voucher bug. |
| Answer replaced by *"could not be verified against any tool result…"* | **Enforcement working as configured** (`block` on an ungrounded value). The refusal lists the exact values and reasons. | expected | Review the `enforcement` audit event. If the customer disputes the refusal, collect the event + conversation for the semantic-attribution review flow (runbook §5) — do not switch mode to make the symptom disappear without the customer's decision. |
| Answer replaced by *"verification unavailable … configured fail-closed"* | The ledger voucher **itself** could not verify (hidden call failed, unparseable stream, retry call failed) and the deployment is `closed`. | degraded | Find the `posture` audit event → its `trigger` names the failing machinery. Usually upstream flakiness on the hidden call. |
| Agent seems to "loop once more" before answering | `retry` keep-alive: the model was pushed back and chose to call tools again. Capped (pushback budget 3/conversation; stagnation detection). | expected | Nothing. `pushbacks` counter in `/metrics` tracks volume. |

## Audit-trail entries (observation `stage_b.action` / `degrade_reason`)

| value | meaning | severity |
|---|---|---|
| `degrade_flag` + `no tools — no keep-alive means` | retry mode but the request offered no tools; shipped flagged. | expected |
| `degrade_flag` + `pushback budget exhausted` | 3 keep-alive rounds spent for this conversation. | expected |
| `degrade_flag` + `stagnant: …` | retry stopped because the missing set stopped shrinking (convergence detection, not a count cap). | expected |
| `degrade_flag` + `internal retry budget exhausted` | in-turn re-ask cap hit. | expected |
| `degrade_flag` + `hidden call failed upstream (status N)` / `retry upstream N` / `retry call failed: …` / `value-map check failed on the retry answer` | ledger voucher machinery failed; posture=open shipped the original. Under `closed` these become `posture_block`. | degraded |
| verdict `stream_parse_error` | A stream=true response could not be parsed as SSE; shipped verbatim (open/flag) or refused (closed+enforcement). | degraded |
| `laundering` event | A tool argument carried a value with a derived, unobserved origin. Recorded only — argument rewriting is deliberately not implemented (fail-dangerous). | expected |

## stderr lines (never affect traffic)

| line | meaning | action |
|---|---|---|
| `ledvouch audit emit failed: …` | The audit sink write failed; the response was still served. | Fix the sink (disk full? path?); events during the gap are lost — say so honestly in any audit context. |
| `ledvouch audit webhook delivery failed: …` | Webhook POST failed (fire-and-forget). | Check the webhook endpoint. Consider `file` sink if the endpoint is flaky. |

## What is NOT an error

- Flagged-but-shipped answers in `flag` mode — that is the light contract.
- Semantic-family refusals (`mismatch` / `user-false` / `omitted` /
  `policy-false`) — correct under the guarantee; see
  `docs/claims_scope.md` before promising a customer these will vanish.
