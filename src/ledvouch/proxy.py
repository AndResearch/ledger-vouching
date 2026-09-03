"""The ledger voucher proxy — an OpenAI-compatible /v1/chat/completions front.

# Design rationale (one-line adoption / spectator turns / terminal turn):
#   The customer's agent points its base_url at us instead of at the model API. We
#   hold the real endpoint's URL+key (env LEDVOUCH_UPSTREAM_BASE / _KEY) and forward
#   every request unchanged. We are a SPECTATOR:
#     - normal turns (response HAS tool_calls): forward, return as-is. Bookkeep the
#       role:"tool" results carried in the request (ledger.py).
#     - terminal turn (response has NO tool_calls): this is the final answer. stage A
#       runs the grounding floor over the ledger's evidence and records a flag
#       out-of-band, then returns the body UNCHANGED (byte transparent).
#
#   The response BODY is never altered in stage A. That is the whole point of the first
#   milestone: prove the spectator is transparent (arm B reward ≡ arm A). block/retry
#   (which alter the body) are stage C, gated on that proof (enforce.py).
#
# Boundary: stateless chat-completions assumed — every past tool
#   result flows past us in messages[]. A Responses-API `previous_response_id` agent
#   would hide history; out of scope for the tau2 rig (it uses chat-completions).
#
# Streaming (A1, 2026-07-20): stream=true is GOVERNED —
#   tool_calls turns pass through chunk-byte-identical; the terminal (content)
#   turn is buffered, judged by the same terminal path, and replayed verbatim
#   unless enforcement altered the body. Design + transparency definitions live
#   in streaming.py; the stream branch is _handle_stream below.
#
# fail-fast: upstream errors are surfaced verbatim (status + body), never
#   swallowed into a fake success.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import hashlib
from dataclasses import dataclass

from .audit import audit_emitter_from_env, posture_event, terminal_events
from .content_hash import answer_hashes
from .enforce import (
    BLOCK,
    FAIL_CLOSED,
    FAIL_OPEN,
    FLAG,
    RETRY,
    Observation,
    ObservationSink,
    apply_posture_block,
    apply_terminal_block,
    apply_terminal_flag,
    close_gate_feedback,
)
from .effect_gate import (
    ACTION_BLOCK,
    ACTION_POSTURE,
    ACTION_RETRY,
    ACTION_SHIP,
    EffectJudgment,
    EffectTerminal,
    PendingEffects,
    apply_effect_block,
    apply_effect_posture_block,
    build_effect_retry_turn,
    decide_effect_action,
    degrade_target,
    effective_mode,
    enforced_tools_in_request,
    enforcement_event,
    iter_designated_calls,
    judge_effect_call,
    parse_effect_terminals,
    union_missing,
)
from .metrics import Metrics
from .grounding import load_bearing_tokens, sufficiency_peek
from .hidden_call import (
    ValueMapResult,
    build_hidden_request,
    build_value_map_request,
    evaluate_answer_refs,
    evaluate_value_map,
    mint_candidates,
    parse_answer_refs,
    parse_value_map,
)
from .ledger import Ledger, build_ledger
from .provenance import build_provenance, numbering_schemes, tokenizer_version, value_tokens
from .refs import GroundingError
from .streaming import (
    CONTENT,
    TOOL_CALLS,
    SSEParseError,
    SSEScanner,
    aggregate_stream,
    classify_payload,
    parse_chunk_payloads,
    synthesize_sse,
)

# A forward function: (body, headers) -> (status_code, response_json). Injectable so
# tests can supply a fake upstream without a live server.
ForwardFn = Callable[[dict[str, Any], dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]]

# The streaming forward seam: (body, headers) -> (status_code, response_headers,
# byte-chunk iterator). The iterator yields the upstream's RAW bytes with its
# chunk boundaries — the passthrough path's byte-identity claim rides on them.
StreamForwardFn = Callable[
    [dict[str, Any], dict[str, str]],
    Awaitable[tuple[int, dict[str, str], Any]],
]


def _response_has_tool_calls(body: dict[str, Any]) -> bool:
    """True if the assistant is calling tools (a normal turn), False if this is a
    final text answer (a terminal turn)."""
    for choice in body.get("choices") or []:
        msg = choice.get("message") or {}
        if msg.get("tool_calls"):
            return True
    return False


def _terminal_answer(body: dict[str, Any]) -> str:
    """The final answer text of a terminal response (choices[0].message.content)."""
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # content-parts form
        return "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return content or ""


def _default_forward() -> ForwardFn:
    """Real upstream: POST {LEDVOUCH_UPSTREAM_BASE}/chat/completions with the real key."""
    base = os.environ.get("LEDVOUCH_UPSTREAM_BASE", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LEDVOUCH_UPSTREAM_KEY", "")
    url = f"{base}/chat/completions"

    async def forward(body: dict[str, Any], _headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # Timeout generous: agent turns can be long; the client sets its own retries.
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(url, json=body, headers=headers)
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"_ledvouch_nonjson_body": resp.text}

    return forward


def _default_stream_forward() -> StreamForwardFn:
    """Real upstream, streaming: same endpoint/key as _default_forward, but the
    response bytes are handed back chunk-by-chunk exactly as received."""
    base = os.environ.get("LEDVOUCH_UPSTREAM_BASE", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LEDVOUCH_UPSTREAM_KEY", "")
    url = f"{base}/chat/completions"

    async def stream_forward(
        body: dict[str, Any], _headers: dict[str, str]
    ) -> tuple[int, dict[str, str], Any]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
        req = client.build_request("POST", url, json=body, headers=headers)
        resp = await client.send(req, stream=True)

        async def chunks():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, chunks()

    return stream_forward


async def _emit_audit(app: FastAPI, event: dict[str, Any]) -> None:
    """Emit one audit event (A3). Audit failure must never break shipping — it is
    surfaced on stderr (visible degradation, never traded against traffic)."""
    emitter = app.state.audit
    if emitter is None:
        return
    try:
        await emitter.emit(event)
    except Exception as e:
        print(f"ledvouch audit emit failed: {type(e).__name__}: {e}", file=sys.stderr)


def _verification_unavailable(
    app: FastAPI, payload: dict[str, Any], *, resp_body: dict[str, Any], reason: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """OUR machinery failed where enforcement needed it (A2 posture site): an
    unparseable stream leaves no verdict; a failed retry forward leaves an
    ungrounded answer we cannot push back on. flag mode ships regardless (light
    never alters the body by contract). In enforcement modes the fail posture
    decides: open → ship the original, flagged; closed → honest refusal (an
    unverified answer never ships)."""
    if app.state.mode in (BLOCK, RETRY):
        payload["posture"] = app.state.fail_posture
        payload["posture_trigger"] = reason
        if app.state.fail_posture == FAIL_CLOSED:
            payload["action"] = "posture_block"
            return apply_posture_block(reason=reason, response_body=resp_body), payload
    payload["action"] = "degrade_flag"
    payload.setdefault("degrade_reason", reason)
    return resp_body, payload


async def _judge_designated(
    app: FastAPI, resp_body: dict[str, Any], ledger: Ledger
) -> list[tuple[EffectJudgment, EffectTerminal]]:
    """Effect-terminal gate (effect_gate.py): judge every designated tool call
    in a response and emit effect_verdict events. Judging never alters, delays,
    or fails the response path (audit failure is already stderr-contained);
    what the caller DOES with the judgments is the enforcement question.
    Receipt registration is the caller's job too — only calls that actually
    SHIP can ever produce a receipt (a blocked call must not sit in the
    pending map waiting for a result that cannot come)."""
    judgments: list[tuple[EffectJudgment, EffectTerminal]] = []
    for call_id, name, args_raw in iter_designated_calls(
        resp_body, app.state.effect_terminals
    ):
        terminal = app.state.effect_terminals[name]
        judgment = judge_effect_call(
            terminal, call_id=call_id, arguments_raw=args_raw, ledger=ledger,
        )
        app.state.metrics.count("effect_verdicts", judgment.verdict)
        await _emit_audit(app, judgment.to_event())
        judgments.append((judgment, terminal))
    return judgments


def _register_effects(
    app: FastAPI, judgments: list[tuple[EffectJudgment, EffectTerminal]]
) -> None:
    """Register SHIPPED designated calls for receipt correlation (the harness
    will execute them; their results arrive on a later request)."""
    for judgment, terminal in judgments:
        app.state.effect_pending.register(
            judgment.call_id, judgment.tool, judgment.verdict,
            terminal.receipt_fields,
        )


async def _emit_effect_enforcement(
    app: FastAPI,
    *,
    action: str,
    judgments: list[tuple[EffectJudgment, EffectTerminal]],
    degrade_reason: str | None = None,
    posture: str | None = None,
    posture_trigger: str | None = None,
) -> None:
    app.state.metrics.count("effect_actions", action)
    await _emit_audit(app, enforcement_event(
        action=action, global_mode=app.state.mode, judgments=judgments,
        degrade_reason=degrade_reason, posture=posture,
        posture_trigger=posture_trigger,
    ))


async def _effect_verification_unavailable(
    app: FastAPI,
    *,
    resp_body: dict[str, Any],
    judgments: list[tuple[EffectJudgment, EffectTerminal]],
    reason: str,
) -> dict[str, Any]:
    """A2 posture site for the effect gate: enforcement needed a verdict the
    ledger voucher cannot produce (unparseable designated arguments; a failed retry
    forward). open → ship the model's original response, flagged (the calls
    execute; the absence of a verdict is recorded, never silent); closed → an
    unverified side effect never executes."""
    await _emit_audit(app, posture_event(app.state.fail_posture, reason))
    if app.state.fail_posture == FAIL_CLOSED:
        await _emit_effect_enforcement(
            app, action="posture_block", judgments=judgments,
            posture=FAIL_CLOSED, posture_trigger=reason,
        )
        return apply_effect_posture_block(reason, resp_body)
    _register_effects(app, judgments)
    await _emit_effect_enforcement(
        app, action="degrade_flag", judgments=judgments,
        degrade_reason=reason, posture=FAIL_OPEN, posture_trigger=reason,
    )
    return resp_body


async def _effect_retry(
    app: FastAPI,
    *,
    body: dict[str, Any],
    resp_body: dict[str, Any],
    ledger: Ledger,
    judgments: list[tuple[EffectJudgment, EffectTerminal]],
    retrying: list[EffectJudgment],
    client_headers: dict[str, str] | None,
) -> tuple[dict[str, Any], str | None]:
    """Effect retry (deep): ledger voucher-internal close-gate push-back over the D5
    synthetic tool exchange. Convergence control mirrors the text terminal
    (strict shrink of the missing set; stagnation and budgets degrade) with
    ONE deliberate asymmetry — D1: the degrade target is BLOCK (per-tool
    "flag" override aside), because an executed unverified write is the harm
    itself. Every round judges against the ORIGINAL request's ledger: the
    push-back feedback echoes rejected values, and a ledger rebuilt from the
    retry exchange would let that echo mint a candidate next round (the
    self-grounding hole §3.2 nails shut)."""
    terminals: dict[str, EffectTerminal] = app.state.effect_terminals
    st = _convo_state(app, body.get("messages") or [])
    cur, cur_missing = retrying, union_missing(retrying)

    async def _degrade(reason: str) -> dict[str, Any]:
        if degrade_target(cur, terminals) == "flag":
            _register_effects(app, judgments)
            await _emit_effect_enforcement(
                app, action="degrade_flag", judgments=judgments,
                degrade_reason=reason,
            )
            return resp_body
        await _emit_effect_enforcement(
            app, action="degrade_block", judgments=judgments, degrade_reason=reason,
        )
        return apply_effect_block(resp_body, cur)

    if st.pushbacks >= app.state.pushback_max:
        return await _degrade("pushback budget exhausted"), None
    if cur_missing == st.last_effect_missing:
        return await _degrade(
            "stagnant: the missing set did not change since the last push-back"
        ), None

    retry_messages = [
        *(body.get("messages") or []),
        *build_effect_retry_turn(resp_body, cur),
    ]
    for _attempt in range(app.state.retry_max):
        # retry rides the NON-stream forward even when the client streams (A1:
        # the push-back is ledger voucher-internal; only the final ship is re-streamed).
        retry_req = {
            k: v for k, v in body.items() if k not in ("stream", "stream_options")
        }
        retry_req["messages"] = retry_messages
        try:
            status, retry_body = await app.state.forward(retry_req, {})
        except Exception as e:
            return await _effect_verification_unavailable(
                app, resp_body=resp_body, judgments=judgments,
                reason=f"effect retry call failed: {type(e).__name__}: {e}",
            ), None
        app.state.metrics.retry_calls += 1
        if status != 200 or not isinstance(retry_body, dict):
            return await _effect_verification_unavailable(
                app, resp_body=resp_body, judgments=judgments,
                reason=f"effect retry upstream {status}",
            ), None

        if not _response_has_tool_calls(retry_body):
            # The model answered in TEXT instead of retrying the write (the
            # honest way out the feedback offers). That is a terminal turn —
            # hand it to the terminal gate (one governance authority for final
            # answers; it judges, enforces per the deployment's mode, and
            # records its own observation).
            await _emit_effect_enforcement(
                app, action="repair_answer", judgments=judgments,
            )
            return await _govern_terminal(
                app, body=body, resp_body=retry_body,
                answer=_terminal_answer(retry_body), ledger=ledger,
                client_headers=client_headers,
            )

        judgments2 = await _judge_designated(app, retry_body, ledger)
        decision2 = decide_effect_action(judgments2, app.state.mode)
        if decision2.action == ACTION_BLOCK:
            await _emit_effect_enforcement(
                app, action="block", judgments=judgments2,
            )
            return apply_effect_block(retry_body, decision2.blocking), None
        if decision2.action == ACTION_POSTURE:
            return await _effect_verification_unavailable(
                app, resp_body=retry_body, judgments=judgments2,
                reason="designated call arguments unparseable — no verdict possible",
            ), None
        if decision2.action == ACTION_RETRY:
            new_missing = union_missing(decision2.retrying)
            if not set(new_missing) < set(cur_missing):
                cur = decision2.retrying
                return await _degrade(
                    "stagnant: retry did not shrink the missing set"
                ), None
            cur, cur_missing = decision2.retrying, new_missing
            retry_messages = [
                *retry_messages,
                *build_effect_retry_turn(retry_body, cur),
            ]
            continue

        # ACTION_SHIP: either the model repaired the designated call(s), or it
        # answered with read-only tool_calls — a keep-alive the harness
        # executes to gather the evidence the feedback demanded.
        _register_effects(app, judgments2)
        if judgments2:
            await _emit_effect_enforcement(
                app, action="repair", judgments=judgments2,
            )
        else:
            st.pushbacks += 1
            st.last_effect_missing = cur_missing
            app.state.metrics.pushbacks += 1
            await _emit_effect_enforcement(
                app, action="pushback", judgments=judgments,
            )
        return retry_body, None

    return await _degrade("internal retry budget exhausted"), None


async def _govern_effects(
    app: FastAPI,
    *,
    body: dict[str, Any],
    resp_body: dict[str, Any],
    ledger: Ledger,
    client_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """The effect-terminal gate over one tool_calls response (E1): judge every
    designated call, then enforce the whole-response decision (§3.2 — no
    partial shipping). Pure-flag responses reproduce the E0 audit volume
    exactly (verdict + receipt, no enforcement event). Returns
    (body_to_ship, answer_hash_header) — the header is non-None only when an
    effect retry ended in a text answer that the terminal gate shipped."""
    judgments = await _judge_designated(app, resp_body, ledger)
    if not judgments:
        return resp_body, None
    decision = decide_effect_action(judgments, app.state.mode)

    if decision.action == ACTION_SHIP:
        _register_effects(app, judgments)
        if any(effective_mode(t, app.state.mode) != FLAG for _j, t in judgments):
            await _emit_effect_enforcement(app, action="ship", judgments=judgments)
        return resp_body, None

    if decision.action == ACTION_BLOCK:
        await _emit_effect_enforcement(app, action="block", judgments=judgments)
        return apply_effect_block(resp_body, decision.blocking), None

    if decision.action == ACTION_POSTURE:
        return await _effect_verification_unavailable(
            app, resp_body=resp_body, judgments=judgments,
            reason="designated call arguments unparseable — no verdict possible",
        ), None

    return await _effect_retry(
        app, body=body, resp_body=resp_body, ledger=ledger,
        judgments=judgments, retrying=decision.retrying,
        client_headers=client_headers,
    )


async def _match_effect_receipts(app: FastAPI, ledger: Ledger) -> None:
    """Emit effect_receipt events for observed results of previously judged
    designated calls (tool_call_id join — effect_gate.py). Runs on every
    request's ledger; matched entries are consumed."""
    for event in app.state.effect_pending.match(ledger):
        app.state.metrics.effect_receipts += 1
        await _emit_audit(app, event)


async def _observe_effect_stream(
    app: FastAPI, payloads: list[bytes], tee_error: str | None, ledger: Ledger
) -> None:
    """Post-stream effect observation for a passthrough tool_calls turn (a
    conversation whose request tools[] offered no enforcement-mode designated
    tool — enforcement-capable conversations buffer instead, §4.1). The client
    already has every byte (chunk-identity untouched — the tee never buffers
    the passthrough); a tee that could not be parsed is recorded as a visible
    absence, never silently dropped. If an ENFORCEMENT-mode designated call
    nevertheless appears here (the model called a tool the request never
    offered), the bytes are already gone — that escape is recorded as a
    degrade, never silently absorbed."""
    body: dict[str, Any] | None = None
    if tee_error is None:
        try:
            body = aggregate_stream(parse_chunk_payloads(payloads))
        except SSEParseError as e:
            tee_error = str(e)
    if body is not None:
        judgments = await _judge_designated(app, body, ledger)
        _register_effects(app, judgments)  # the bytes shipped — calls will execute
        if decide_effect_action(judgments, app.state.mode).action != ACTION_SHIP:
            await _emit_effect_enforcement(
                app, action="degrade_flag", judgments=judgments,
                degrade_reason="bytes already shipped: the designated tool was "
                "not offered in the request's tools[], so the stream passed "
                "through unbuffered",
            )
        return
    app.state.metrics.count("effect_verdicts", "stream_unparseable")
    await _emit_audit(app, {
        "event": "effect_verdict", "stage": "E1",
        "verdict": "stream_unparseable", "error": tee_error,
    })


async def _upstream_unreachable(app: FastAPI, exc: Exception) -> JSONResponse:
    """The MAIN forward failed — there is no model answer to pass through, so
    both postures surface an honest 502 (fail-fast: never a fake success). The
    posture activation is still recorded (A2 audit requirement)."""
    reason = f"upstream unreachable: {type(exc).__name__}: {exc}"
    app.state.metrics.upstream_errors += 1
    await _emit_audit(app, posture_event(app.state.fail_posture, reason))
    return JSONResponse(
        status_code=502,
        content={"error": {"message": f"ledger voucher: {reason}",
                           "type": "ledvouch_upstream_unreachable"}},
    )


async def _stage_b_check(
    *,
    forward: ForwardFn,
    original_body: dict[str, Any],
    answer: str,
    ledger: Ledger,
) -> dict[str, Any]:
    """stage B terminal enrichment: ONE hidden call (ref-template rewrite), ledger-side
    resolution + comparison, then the provenance walk. Read-only with respect to the
    shipped body — the caller still returns the upstream response unchanged (flag).

    Gate: skipped when the answer carries no load-bearing tokens at all (a purely
    conversational turn has nothing to ground — this keeps the hidden-call cost on
    value-bearing turns only; the skip is recorded, not silent)."""
    if not load_bearing_tokens(answer):
        return {"verdict": "skipped", "skipped": True}

    payload: dict[str, Any] = {"skipped": False}
    hidden_req = build_hidden_request(original_body, answer, ledger)
    try:
        status, hidden_body = await forward(hidden_req, {})
    except Exception as e:  # network-level failure of OUR call — never break shipping
        payload.update(verdict="error", hidden_error=f"{type(e).__name__}: {e}")
        return payload
    payload["hidden_status"] = status
    if isinstance(hidden_body, dict) and isinstance(hidden_body.get("usage"), dict):
        usage = hidden_body["usage"]
        payload["usage"] = {
            k: usage.get(k)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    if status != 200 or not isinstance(hidden_body, dict):
        payload.update(
            verdict="error",
            hidden_error=f"hidden call failed upstream (status {status}): "
            f"{str(hidden_body)[:500]}",
        )
        return payload
    try:
        answer_refs = parse_answer_refs(hidden_body)
    except GroundingError as e:
        payload.update(verdict="error", hidden_error=str(e))
        return payload

    evaluation = evaluate_answer_refs(answer, answer_refs, ledger)
    prov = build_provenance(answer, ledger)
    payload["eval"] = evaluation.to_dict()
    payload["provenance"] = {
        "tree": prov.tree,
        "ungrounded_answer": prov.ungrounded_answer,
        "laundered": prov.laundered,
    }
    grounded_b = (
        evaluation.verdict == "grounded"
        and not prov.ungrounded_answer
        and not prov.laundered
    )
    payload["verdict"] = "grounded" if grounded_b else "ungrounded"
    return payload


# ---- stage C: value-map check + enforcement ----------------------
#
# Enforcement keys on the VALUE-MAP verdict, never on the stage A token floor — the
# floor's capitalized-word false positives (tau2-bench measured 2026-07-17) would cause
# false blocks. Since v3 (candidate-path enum, 2026-07-21) that verdict
# is CANDIDATE EXISTENCE — computed deterministically at mint time, independent
# of the model's attribution answer (hidden_call.py rationale). Consequences
# wired here:
#   - the attribution hidden call can no longer lose the verdict: its failure is
#     recorded (`attribution_error`), never a fail-posture site. Remaining
#     posture sites: unparseable stream, retry-forward failure.
#   - retry convergence keys on a NOISE-FREE missing set (induction-rig measured the noisy
#     one as the structural retry blocker: strict-shrink died at round 1), and
#     retry rounds are evaluated without hidden calls — one attribution call is
#     spent on the answer that actually ships.
# Laundered argument values are RECORDED but not enforced on in stage C (repairing
# them means rewriting tool arguments — fail-dangerous by design; deferred).
#
# retry (deep) mechanics — keep-alive INSIDE the protocol:
#   the push-back request re-offers the agent's own tools; if the model answers
#   with tool_calls we return THAT response to the harness, which executes the
#   tools and continues its own loop (the harness never learns the ledger voucher
#   intervened). Convergence control follows the productive-axis scar: continue
#   while the missing set strictly shrinks; a repeated (stagnant) missing set
#   degrades to flag; PUSHBACK_MAX / INTERNAL_RETRY_MAX are backstops, not the
#   primary stop condition. Non-convergence degrades to LIGHT (flag) — deep is
#   strictly dominant, never worse than light.

INTERNAL_RETRY_MAX = 2  # re-asks for a corrected FINAL ANSWER within one turn
PUSHBACK_MAX = 3  # keep-alive rounds (tool_calls returned) per conversation


@dataclass
class _ConvoState:
    """Per-conversation retry bookkeeping. The fingerprint is wire-derived (system
    + goal text) — adequate for serialized runs; concurrent conversations sharing
    an identical goal would collide (a real deployment keys on a client-supplied
    conversation id header instead)."""

    last_len: int = 0
    pushbacks: int = 0
    last_missing: tuple[str, ...] = ()
    # effect-gate stagnation key (E1): the union of missing tokens across the
    # gated calls at the last effect push-back. Kept separate from the text
    # terminal's last_missing (different judged objects), while the pushback
    # BUDGET above is shared — one keep-alive allowance per conversation.
    last_effect_missing: tuple[str, ...] = ()


def _convo_state(app: FastAPI, messages: list[dict[str, Any]]) -> _ConvoState:
    system = next((str(m.get("content")) for m in messages if m.get("role") == "system"), "")
    goal = next((str(m.get("content")) for m in messages if m.get("role") == "user"), "")
    fp = hashlib.sha1((system + "\x00" + goal).encode()).hexdigest()
    states: dict[str, _ConvoState] = app.state.convo
    st = states.get(fp)
    if st is None or len(messages) < st.last_len:  # shorter ⇒ a NEW conversation
        st = _ConvoState()
        states[fp] = st
    st.last_len = len(messages)
    return st


async def _run_value_map(
    *,
    forward: ForwardFn,
    original_body: dict[str, Any],
    answer: str,
    ledger: Ledger,
    payload: dict[str, Any],
) -> ValueMapResult | None:
    """Deterministic candidate mint + existence verdict (v3), plus ONE attribution
    hidden call for the provenance trail when any candidate exists. The verdict
    never depends on the call's outcome — an attribution failure is recorded
    (`attribution_error`), and there is always a verdict to enforce on. Returns
    None only on a value-free turn."""
    tokens = value_tokens(answer)
    if not tokens:
        payload["verdict"] = "skipped"  # a value-free turn has nothing to ground
        return None
    candidates = mint_candidates(tokens, ledger)
    schemes = numbering_schemes(tokens, ledger)
    if schemes:  # numbering-license state is audit material (incl. guard firings)
        payload["numbering"] = {
            p: {"anchored": s.anchored, "collision": s.collision,
                "licensed": sorted(s.licensed_numbers)}
            for p, s in schemes.items()
        }
    mapping: list[dict[str, str]] = []
    if any(candidates.values()):
        hidden_req = build_value_map_request(
            original_body, answer, ledger, tokens, candidates
        )
        try:
            status, hidden_body = await forward(hidden_req, {})
        except Exception as e:
            payload["attribution_error"] = f"{type(e).__name__}: {e}"
        else:
            payload["n_hidden_calls"] = payload.get("n_hidden_calls", 0) + 1
            if isinstance(hidden_body, dict) and isinstance(
                hidden_body.get("usage"), dict
            ):
                u = hidden_body["usage"]
                payload["hidden_tokens"] = payload.get("hidden_tokens", 0) + (
                    u.get("total_tokens") or 0
                )
            if status != 200 or not isinstance(hidden_body, dict):
                payload["attribution_error"] = (
                    f"hidden call failed upstream (status {status}): "
                    f"{str(hidden_body)[:500]}"
                )
            else:
                try:
                    mapping = parse_value_map(hidden_body)
                except GroundingError as e:
                    payload["attribution_error"] = str(e)
    vm = evaluate_value_map(tokens, candidates, mapping, schemes)
    payload["verdict"] = vm.verdict
    payload["eval"] = vm.to_dict()
    return vm


async def _stage_c_terminal(
    app: FastAPI,
    *,
    body: dict[str, Any],
    resp_body: dict[str, Any],
    answer: str,
    ledger: Ledger,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stage C terminal path. Returns (body_to_ship, stage_c payload). The
    shipped body is altered ONLY by explicit enforcement (block, or a repaired /
    keep-alive retry response) — every degrade path ships the original body."""
    forward: ForwardFn = app.state.forward
    mode: str = app.state.mode
    payload: dict[str, Any] = {"stage": "C", "mode": mode, "action": "ship"}

    vm = await _run_value_map(
        forward=forward, original_body=body, answer=answer, ledger=ledger, payload=payload
    )
    prov = build_provenance(answer, ledger)
    payload["laundered"] = prov.laundered  # recorded, not enforced (fail-dangerous to repair)

    if vm is None or vm.verdict == "grounded":
        # v3: the verdict is deterministic (mint-time existence) — there is no
        # "our machinery failed, no verdict" branch left on this path; an
        # attribution failure rides in payload["attribution_error"], recorded.
        return resp_body, payload

    # ---- ungrounded: enforce per mode -------------------------------------
    if mode == FLAG:
        return resp_body, payload

    if mode == BLOCK:
        payload["action"] = "block"
        return (
            apply_terminal_block(
                answer=answer, missing=vm.missing, reasons=vm.reasons,
                response_body=resp_body,
            ),
            payload,
        )

    # mode == RETRY (deep). Degrade to flag when keep-alive is impossible or
    # the conversation has stopped converging.
    st = _convo_state(app, body.get("messages") or [])
    if not body.get("tools"):
        payload.update(action="degrade_flag", degrade_reason="no tools — no keep-alive means")
        return resp_body, payload
    if st.pushbacks >= app.state.pushback_max:
        payload.update(action="degrade_flag", degrade_reason="pushback budget exhausted")
        return resp_body, payload
    if tuple(vm.missing) == st.last_missing:
        payload.update(
            action="degrade_flag",
            degrade_reason="stagnant: the missing set did not change since the last push-back",
        )
        return resp_body, payload

    retry_messages = [*(body.get("messages") or [])]
    cur_answer, cur_vm = answer, vm
    for attempt in range(app.state.retry_max):
        retry_messages = [
            *retry_messages,
            {"role": "assistant", "content": cur_answer},
            {"role": "user", "content": close_gate_feedback(cur_vm.missing, cur_vm.reasons)},
        ]
        # retry rides the NON-stream forward even when the client streams (A1:
        # the push-back is ledger voucher-internal; only the final ship is re-streamed).
        retry_req = {
            k: v for k, v in body.items() if k not in ("stream", "stream_options")
        }
        retry_req["messages"] = retry_messages
        try:
            status, retry_body = await forward(retry_req, {})
        except Exception as e:
            return _verification_unavailable(
                app, payload, resp_body=resp_body,
                reason=f"retry call failed: {type(e).__name__}: {e}",
            )
        payload["n_retry_calls"] = payload.get("n_retry_calls", 0) + 1
        if isinstance(retry_body, dict) and isinstance(retry_body.get("usage"), dict):
            payload["hidden_tokens"] = payload.get("hidden_tokens", 0) + (
                retry_body["usage"].get("total_tokens") or 0
            )
        if status != 200 or not isinstance(retry_body, dict):
            return _verification_unavailable(
                app, payload, resp_body=resp_body, reason=f"retry upstream {status}",
            )

        if _response_has_tool_calls(retry_body):
            # keep-alive: hand the model's tool request to the harness — its own
            # loop executes the tools and comes back with more evidence.
            st.pushbacks += 1
            st.last_missing = tuple(cur_vm.missing)
            payload.update(action="pushback", pushbacks=st.pushbacks)
            return retry_body, payload

        # v3: retry rounds are judged by the deterministic existence verdict
        # alone (noise-free convergence key — the induction-rig-measured binding
        # constraint); no hidden call is spent on an answer that may not ship.
        new_answer = _terminal_answer(retry_body)
        new_tokens = value_tokens(new_answer)
        if not new_tokens:
            # an honest value-free answer ("could not verify") is a repair —
            # ship the model's OWN corrected response; the ledger voucher never
            # writes answer content.
            payload["verdict"] = "skipped"
            payload["action"] = "repair"
            return retry_body, payload
        new_vm = evaluate_value_map(
            new_tokens, mint_candidates(new_tokens, ledger), [],
            numbering_schemes(new_tokens, ledger),
        )
        payload["verdict"] = new_vm.verdict
        payload["eval"] = new_vm.to_dict()
        if new_vm.verdict == "grounded":
            # repaired — this answer ships, so spend the ONE attribution hidden
            # call on it (trail material; the verdict above cannot change).
            await _run_value_map(
                forward=forward, original_body=body, answer=new_answer,
                ledger=ledger, payload=payload,
            )
            payload["action"] = "repair"
            return retry_body, payload
        if not set(new_vm.missing) < set(cur_vm.missing):
            payload.update(
                action="degrade_flag",
                degrade_reason="stagnant: retry did not shrink the missing set",
            )
            return resp_body, payload
        cur_answer, cur_vm = new_answer, new_vm  # productive — continue

    payload.update(action="degrade_flag", degrade_reason="internal retry budget exhausted")
    return resp_body, payload


def _identity_material(
    body: dict[str, Any], client_headers: dict[str, str],
    identity_headers: tuple[str, ...],
) -> dict[str, Any]:
    """Wire-derived identity for the observation event (evidence-layer identity
    tiers 1-2). Never the credential itself (A5): tier 1 is the SHA-256 of
    the client's bearer token — an irreversible join key at key granularity.
    Tier 2 is opportunistic: the `user` body field (gateways set it) plus any
    header the operator NAMED via LEDVOUCH_IDENTITY_HEADERS (nothing else is
    copied — headers can carry cookies/credentials, so forwarding is allow-list
    only)."""
    auth = client_headers.get("authorization", "")
    cred = auth.split(" ", 1)[1] if " " in auth else auth
    identity: dict[str, Any] = {
        "auth_key_hash": hashlib.sha256(cred.encode()).hexdigest() if cred else None,
        "request_user": body.get("user"),
    }
    named = {
        h.lower(): client_headers[h.lower()]
        for h in identity_headers if client_headers.get(h.lower())
    }
    if named:
        identity["headers"] = named
    return identity


async def _govern_terminal(
    app: FastAPI,
    *,
    body: dict[str, Any],
    resp_body: dict[str, Any],
    answer: str,
    ledger: Ledger,
    client_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Terminal governance shared by the non-stream and stream paths: records the
    observation and returns (body_to_ship, answer_hash_header_value) — the body is
    resp_body ITSELF whenever the ship is unaltered (the stream path uses that
    identity to pick the verbatim replay). The header value (sha_canon) is non-None
    only when the header option is ON and the shipped body IS the observed answer —
    a hash header must never describe bytes the client did not receive."""
    suff = sufficiency_peek(
        goal=ledger.goal,
        answer=answer,
        evidence=ledger.evidence(),
        progress_possible=app.state.progress_possible,
    )
    # The evidence-layer join key: both hashes of the observed
    # terminal answer, computed once, recorded on the observation and the
    # verdict audit event below.
    hashes = answer_hashes(answer)
    payload: dict[str, Any] | None = None

    # stage C: value-map verdict + enforcement (flag/block/retry). The stage-A
    # floor verdict (suff) is still recorded side-by-side for comparison.
    if app.state.stage == "C":
        ship_body, payload = await _stage_c_terminal(
            app, body=body, resp_body=resp_body, answer=answer, ledger=ledger,
        )
        app.state.sink.record(
            Observation(
                verdict=suff.verdict,
                missing=suff.missing,
                mode=app.state.mode,
                shipped=payload.get("action") in ("ship", "degrade_flag"),
                answer=answer,
                reason=suff.reason,
                stage_b=payload,
                sha_raw=hashes["sha_raw"],
                sha_canon=hashes["sha_canon"],
            )
        )
    elif app.state.mode == FLAG:
        # flag mode — record out-of-band, return the body UNCHANGED. stage B adds
        # the hidden call + provenance walk to the observation; the body stays
        # untouched in stages A/B (stage A's transparency is the standing
        # constraint).
        if app.state.stage == "B":
            payload = await _stage_b_check(
                forward=app.state.forward,
                original_body=body,
                answer=answer,
                ledger=ledger,
            )
        ship_body = apply_terminal_flag(
            answer=answer, suff=suff, response_body=resp_body,
            sink=app.state.sink, stage_b=payload,
            sha_raw=hashes["sha_raw"], sha_canon=hashes["sha_canon"],
        )
    else:
        raise NotImplementedError(
            f"mode {app.state.mode!r} requires stage='C' (stage A/B serve flag only)"
        )

    # A3/A4: audit events + metrics, derived from the same observation facts.
    p = payload or {}
    metrics: Metrics = app.state.metrics
    metrics.turns_terminal += 1
    metrics.count("verdicts", p.get("verdict", suff.verdict))
    metrics.count("actions", p.get("action", "ship"))
    metrics.hidden_calls += p.get("n_hidden_calls", 0)
    metrics.retry_calls += p.get("n_retry_calls", 0)
    if p.get("action") == "pushback":
        metrics.pushbacks += 1
    if app.state.emit_observation:
        # The observation event (opt-in, default OFF): the evidence-layer
        # substrate — a verification portal renders its screens from exactly
        # this payload. OFF keeps the pre-observation audit volume
        # byte-for-byte.
        await _emit_audit(app, {
            "event": "observation",
            "mode": app.state.mode,
            "stage": app.state.stage,
            "verdict": p.get("verdict", suff.verdict),
            "action": p.get("action", "ship"),
            # routing facts for the portal's connection list: which model was
            # asked for, and where this ledger voucher forwards (URL only — the
            # upstream key never leaves the forward closure, A5).
            "model": body.get("model"),
            "upstream_base": app.state.upstream_base,
            "sha_raw": hashes["sha_raw"],
            "sha_canon": hashes["sha_canon"],
            "answer": answer,
            "goal": ledger.goal,
            "eval": p.get("eval"),
            "laundered": p.get("laundered"),
            "steps": [
                {"n": i + 1, "name": r.name, "arguments": r.arguments_raw,
                 "output": r.output}
                for i, r in enumerate(ledger.records)
            ],
            "identity": _identity_material(
                body, client_headers or {}, app.state.identity_headers
            ),
        })
    for event in terminal_events(
        mode=app.state.mode, stage=app.state.stage,
        floor_verdict=suff.verdict, floor_missing=suff.missing, payload=payload,
        sha_raw=hashes["sha_raw"], sha_canon=hashes["sha_canon"],
    ):
        await _emit_audit(app, event)
    hash_header = (
        hashes["sha_canon"]
        if app.state.answer_hash_header and ship_body is resp_body
        else None
    )
    return ship_body, hash_header


async def _handle_stream(
    app: FastAPI, body: dict[str, Any], headers: dict[str, str]
) -> Response:
    """A1 streaming governance (design + transparency definitions in
    streaming.py). tool_calls turns pass through with chunk-unit byte identity;
    content (terminal-candidate) turns are buffered, judged by the SAME terminal
    path as non-stream, and replayed verbatim unless enforcement altered the
    body (block / retry repair / keep-alive → synthesized SSE).

    E1 contract revision (§4.1): a conversation whose request tools[] offers a
    designated tool with enforcement (block/retry) buffers its tool_calls
    turns too — the effect verdict must precede the first shipped byte. The
    judgment is per-request from tools[] (the first tool_call delta can never
    rule out a later parallel designated call, so mid-stream name guessing is
    not attempted); an unaltered ship replays the buffered chunks verbatim
    (payload-concat identity, the same contract as the content turn).
    Flag-only and undesignated conversations keep the chunk-identity
    passthrough unchanged."""
    ledger = build_ledger(body.get("messages") or [])
    effect_gated = bool(enforced_tools_in_request(
        body.get("tools"), app.state.effect_terminals, app.state.mode
    ))
    t0 = time.monotonic()
    try:
        status, up_headers, upstream = await app.state.stream_forward(body, headers)
    except Exception as e:
        return await _upstream_unreachable(app, e)
    app.state.metrics.observe_upstream_ms((time.monotonic() - t0) * 1000)
    media = (up_headers or {}).get("content-type") or "text/event-stream"
    if status != 200:
        # fail-fast: surface the upstream error verbatim, never a fake success.
        raw = b"".join([chunk async for chunk in upstream])
        return Response(status_code=status, content=raw, media_type=media)

    scanner = SSEScanner()
    held: list[bytes] = []  # verbatim upstream chunks, for transparent replay
    payloads: list[bytes] = []
    turn: str | None = None
    parse_error: str | None = None
    it = upstream.__aiter__()

    # -- classification: hold chunks until the first decisive delta ------------
    while turn is None and parse_error is None:
        try:
            chunk = await it.__anext__()
        except StopAsyncIteration:
            break
        held.append(chunk)
        try:
            for p in scanner.feed(chunk):
                payloads.append(p)
                if turn is None:
                    turn = classify_payload(p)
        except SSEParseError as e:
            parse_error = str(e)

    if turn == TOOL_CALLS and parse_error is None and not effect_gated:
        # spectator passthrough: the held prefix, then every remaining chunk,
        # byte-identical (passthrough path = per-chunk byte identity). With effect
        # terminals configured the passthrough is additionally TEED for the
        # flag-mode observation — the tee reads the same chunks it yields, so
        # the chunk-identity contract is untouched; observation runs only after
        # the last byte has been yielded, and a tee parse failure downgrades to
        # a recorded absence, never to an altered or delayed stream.
        # (Enforcement-capable conversations never reach here — effect_gated
        # sends them to the buffered path below, §4.1.)
        app.state.metrics.turns_normal += 1
        observe_effects = bool(app.state.effect_terminals)

        async def passthrough():
            tee_error: str | None = None
            for held_chunk in held:
                yield held_chunk
            async for live_chunk in it:
                if observe_effects and tee_error is None:
                    try:
                        payloads.extend(scanner.feed(live_chunk))
                    except SSEParseError as e:
                        tee_error = str(e)
                yield live_chunk
            if observe_effects:
                try:
                    await _observe_effect_stream(app, payloads, tee_error, ledger)
                except Exception as e:  # observation must never break the stream
                    print(
                        f"ledvouch effect observation failed: "
                        f"{type(e).__name__}: {e}", file=sys.stderr,
                    )

        return StreamingResponse(passthrough(), status_code=200, media_type=media)

    # -- buffered path: content turn (terminal candidate), or ANY turn of an
    # effect-gated conversation (§4.1) — buffer to the end ---------------------
    while parse_error is None:
        try:
            chunk = await it.__anext__()
        except StopAsyncIteration:
            break
        held.append(chunk)
        try:
            payloads.extend(scanner.feed(chunk))
        except SSEParseError as e:
            parse_error = str(e)
    if parse_error is not None:
        async for chunk in it:  # drain: the verbatim replay must be complete
            held.append(chunk)
    elif scanner.residual().strip():
        parse_error = f"truncated/non-SSE trailing bytes: {scanner.residual()[:200]!r}"

    def replay(headers: dict[str, str] | None = None) -> StreamingResponse:
        async def chunks():
            for held_chunk in held:
                yield held_chunk

        return StreamingResponse(chunks(), status_code=200, media_type=media,
                                 headers=headers)

    async def degrade(reason: str) -> StreamingResponse:
        # An unparseable stream leaves NO verdict — an A2 posture site. open (and
        # flag mode, which never alters): verbatim spectator + error observation
        # (the degrade is recorded, never silent; the bytes are never
        # coerced). closed in enforcement modes: honest refusal instead of
        # unverifiable bytes.
        app.state.metrics.count("verdicts", "stream_parse_error")
        enforcing = app.state.mode in (BLOCK, RETRY) or effect_gated
        closed = enforcing and app.state.fail_posture == FAIL_CLOSED
        stage_c = None
        if enforcing:
            stage_c = {
                "action": "posture_block" if closed else "degrade_flag",
                "posture": app.state.fail_posture,
                "posture_trigger": reason,
            }
            await _emit_audit(app, posture_event(app.state.fail_posture, reason))
        app.state.sink.record(
            Observation(
                verdict="stream_parse_error", missing=(), mode=app.state.mode,
                shipped=not closed, reason=reason, stage_b=stage_c,
            )
        )
        if closed:
            app.state.metrics.count("actions", "posture_block")
            refusal = apply_posture_block(
                reason=reason,
                response_body={"id": "ledvouch", "object": "chat.completion",
                               "choices": []},
            )
            sse_chunks = synthesize_sse(refusal)

            async def refusal_stream():
                for sse_chunk in sse_chunks:
                    yield sse_chunk

            return StreamingResponse(
                refusal_stream(), status_code=200, media_type="text/event-stream"
            )
        return replay()

    if parse_error is not None:
        return await degrade(parse_error)
    try:
        resp_body = aggregate_stream(parse_chunk_payloads(payloads))
    except SSEParseError as e:
        return await degrade(str(e))

    if _response_has_tool_calls(resp_body):
        # A normal turn after all: either mixed (content first, tool_calls
        # later — misclassification cost is latency, never correctness) or an
        # effect-gated tool_calls turn. The effect gate governs it; an
        # unaltered ship replays the buffered upstream bytes verbatim
        # (payload-concat identity), an enforced ship is synthesized SSE.
        app.state.metrics.turns_normal += 1
        if app.state.effect_terminals:
            ship_body, _ = await _govern_effects(
                app, body=body, resp_body=resp_body, ledger=ledger,
                client_headers=headers,
            )
            if ship_body is not resp_body:
                effect_chunks = synthesize_sse(
                    ship_body,
                    include_usage=bool(
                        (body.get("stream_options") or {}).get("include_usage")
                    ),
                )

                async def effect_synthesized():
                    for sse_chunk in effect_chunks:
                        yield sse_chunk

                return StreamingResponse(
                    effect_synthesized(), status_code=200,
                    media_type="text/event-stream",
                )
        return replay()

    answer = _terminal_answer(resp_body)
    ship_body, hash_header = await _govern_terminal(
        app, body=body, resp_body=resp_body, answer=answer, ledger=ledger,
        client_headers=headers,
    )
    if ship_body is resp_body:
        # unaltered ship (flag / degrade): replay the buffered upstream bytes —
        # the contract is data-payload concatenation identity (streaming.py).
        return replay(
            {"X-Ledvouch-Answer-Hash": hash_header} if hash_header else None
        )

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    sse_chunks = synthesize_sse(ship_body, include_usage=include_usage)

    async def synthesized():
        for sse_chunk in sse_chunks:
            yield sse_chunk

    return StreamingResponse(synthesized(), status_code=200, media_type="text/event-stream")


def make_app(
    *,
    fail_posture: str,
    forward: ForwardFn | None = None,
    stream_forward: StreamForwardFn | None = None,
    sink: ObservationSink | None = None,
    audit: Any | None = None,
    mode: str = FLAG,
    stage: str = "A",
    progress_possible: bool = True,
    retry_max: int = INTERNAL_RETRY_MAX,
    pushback_max: int = PUSHBACK_MAX,
    answer_hash_header: bool = False,
    emit_observation: bool = False,
    identity_headers: tuple[str, ...] = (),
    upstream_base: str | None = None,
    effect_terminals: dict[str, EffectTerminal] | None = None,
) -> FastAPI:
    """Build the ledger voucher app. `fail_posture` ("open"|"closed") is REQUIRED — what
    happens when the ledger voucher cannot verify is the customer's governance choice
    and has no silent default (A2). `forward` defaults to the real
    upstream; tests inject a fake. `stream_forward` is the same seam for
    stream=true requests (raw byte chunks). `sink` collects out-of-band
    observations (the runner reads it after a run); `audit` is the external
    audit-event emitter (A3; None = no external stream, rig use). `stage`: "A" =
    terminal token floor only; "B" = + hidden call, refs resolution and the
    provenance walk (still flag — the shipped body is unchanged in both stages).
    `retry_max`/`pushback_max` bound deep-mode correction rounds / keep-alive
    push-backs; they are BACKSTOPS behind convergence detection (the stagnation
    rule stays the primary stop — productive-axis scar), made configurable
    2026-07-21 because the defaults were measured too tight for weak-model
    plateaus (the induction rig: repairs 0/16 at 2 rounds vs 57.5% at 6 harvest-side).
    `answer_hash_header` (default OFF): when ON, an UNALTERED terminal ship
    additionally carries `X-Ledvouch-Answer-Hash: <sha_canon>` — a header-only
    delta, re-provable by `ledvouch doctor` (mech.answer_hash_header). The
    hashes themselves ride the observation + audit stream regardless.
    `emit_observation` (default OFF): when ON, every terminal turn additionally
    emits one `observation` audit event — the evidence-layer substrate (answer,
    eval, steps, identity). OFF keeps
    the audit volume identical to pre-observation builds. `identity_headers`
    is the allow-list of client header names copied into that event's identity
    material (empty default: nothing is copied). `effect_terminals` (default
    none) designates side-effect tools for the effect gate (effect_gate.py):
    designated calls are judged against the observed ledger and, per the
    per-tool effective mode (declaration `mode`, else the global mode),
    flagged (observation only — E0 semantics), blocked before execution, or
    pushed back ledger voucher-internally (retry; non-convergence degrades per the
    per-tool `degrade` target, default block — D1). Unconfigured, the gate is
    fully inert and the deployment behaves byte-identically to a build
    without it (τ2-remeasured 2026-08-17)."""
    if fail_posture not in (FAIL_OPEN, FAIL_CLOSED):
        raise ValueError(
            f"fail_posture must be {FAIL_OPEN!r} or {FAIL_CLOSED!r}, got {fail_posture!r}"
        )
    app = FastAPI(title="ledger-vouching", version="0.0.1")
    app.state.forward = forward or _default_forward()
    app.state.stream_forward = stream_forward or _default_stream_forward()
    app.state.sink = sink or ObservationSink()
    app.state.audit = audit
    app.state.metrics = Metrics()
    app.state.mode = mode
    app.state.stage = stage
    app.state.fail_posture = fail_posture
    app.state.progress_possible = progress_possible
    app.state.retry_max = retry_max
    app.state.pushback_max = pushback_max
    app.state.answer_hash_header = answer_hash_header
    app.state.emit_observation = emit_observation
    app.state.identity_headers = tuple(h.lower() for h in identity_headers)
    app.state.upstream_base = upstream_base  # observation-event routing fact
    app.state.convo = {}  # stage C retry bookkeeping (per-conversation)
    app.state.effect_terminals = effect_terminals or {}
    app.state.effect_pending = PendingEffects()  # judged calls awaiting receipts
    if mode in (BLOCK, RETRY) and stage != "C":
        raise ValueError(f"mode {mode!r} requires stage='C' (enforcement keys on the value-map verdict)")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # liveness for the runner to poll
        return {
            "status": "ok",
            "mode": app.state.mode,
            "stage": app.state.stage,
            "fail_posture": app.state.fail_posture,
        }

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:  # A4: operational counters (JSON v0)
        snap = app.state.metrics.snapshot()
        if app.state.effect_terminals:  # section present only when configured —
            # the unconfigured snapshot stays byte-identical to pre-E0 builds.
            snap["effects"] = {
                "verdicts": dict(app.state.metrics.effect_verdicts),
                "actions": dict(app.state.metrics.effect_actions),
                "receipts": app.state.metrics.effect_receipts,
                "pending": len(app.state.effect_pending),
                "evicted": app.state.effect_pending.evicted,
            }
        return snap

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body: dict[str, Any] = await request.json()
        app.state.metrics.requests_total += 1

        # ① bookkeep the conversation carried in the request (spectator, no mutation).
        ledger = build_ledger(body.get("messages") or [])

        # ①' effect receipts (E0): results of previously judged designated calls
        # arrive IN this request's messages — correlate before anything else
        # (both the stream and non-stream paths carry receipts).
        if app.state.effect_terminals:
            await _match_effect_receipts(app, ledger)

        # ② streaming (A1): tool_calls turns pass through chunk-byte-identical;
        # the terminal turn is buffered and judged by the same path as non-stream.
        if body.get("stream"):
            app.state.metrics.requests_stream += 1
            return await _handle_stream(app, body, dict(request.headers))

        # ③ forward unchanged to the real endpoint.
        t0 = time.monotonic()
        try:
            status, resp_body = await app.state.forward(body, dict(request.headers))
        except Exception as e:
            return await _upstream_unreachable(app, e)
        app.state.metrics.observe_upstream_ms((time.monotonic() - t0) * 1000)
        if status != 200 or not isinstance(resp_body, dict):
            return JSONResponse(status_code=status, content=resp_body)

        # ⑤ normal turn (tool_calls present) → return as-is, keep spectating —
        # UNLESS a designated side-effect call is present: the effect gate
        # (effect_gate.py) judges it and, in enforcement modes, may replace the
        # response (block / posture) or push back internally (retry) before
        # anything the harness would execute ships.
        if _response_has_tool_calls(resp_body):
            app.state.metrics.turns_normal += 1
            if app.state.effect_terminals:
                ship_body, hash_header = await _govern_effects(
                    app, body=body, resp_body=resp_body, ledger=ledger,
                    client_headers=dict(request.headers),
                )
                response = JSONResponse(status_code=status, content=ship_body)
                if hash_header:
                    response.headers["X-Ledvouch-Answer-Hash"] = hash_header
                return response
            return JSONResponse(status_code=status, content=resp_body)

        # ⑤' terminal turn → grounding floor + per-stage governance (shared with
        # the stream path).
        answer = _terminal_answer(resp_body)
        ship_body, hash_header = await _govern_terminal(
            app, body=body, resp_body=resp_body, answer=answer, ledger=ledger,
            client_headers=dict(request.headers),
        )
        response = JSONResponse(status_code=status, content=ship_body)
        if hash_header:
            response.headers["X-Ledvouch-Answer-Hash"] = hash_header
        return response

    return app


def create_app() -> FastAPI:
    """Env-configured production entry: `uvicorn --factory ledvouch.proxy:create_app`.

    LEDVOUCH_FAIL_POSTURE is REQUIRED — the open/closed choice is the customer's
    governance judgment; refusing to start beats a silent default (A2).
    LEDVOUCH_MODE defaults to "flag" (light, never alters the body) and
    LEDVOUCH_STAGE to "C" (the current verdict engine) — documented defaults,
    see README "configuration reference". LEDVOUCH_RETRY_MAX /
    LEDVOUCH_PUSHBACK_MAX tune the deep-mode round backstops (defaults unchanged;
    a malformed value refuses startup — no silent default)."""
    posture = os.environ.get("LEDVOUCH_FAIL_POSTURE")
    if posture not in (FAIL_OPEN, FAIL_CLOSED):
        raise RuntimeError(
            f"LEDVOUCH_FAIL_POSTURE must be set to '{FAIL_OPEN}' or '{FAIL_CLOSED}' "
            f"(got {posture!r}). The fail posture is a customer governance choice — "
            "the ledger voucher refuses to start without it (no silent default)."
        )

    def _env_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except ValueError:
            raise RuntimeError(f"{name} must be an integer >= 1, got {raw!r}")
        if value < 1:
            raise RuntimeError(f"{name} must be an integer >= 1, got {raw!r}")
        return value

    def _env_switch(name: str) -> bool:
        value = os.environ.get(name, "off")
        if value not in ("on", "off"):
            raise RuntimeError(
                f"{name} must be 'on' or 'off', got {value!r} (no silent default)"
            )
        return value == "on"

    identity_headers = tuple(
        h.strip() for h in
        os.environ.get("LEDVOUCH_IDENTITY_HEADERS", "").split(",") if h.strip()
    )

    # LEDVOUCH_TOKENIZER (default v1) is read at verdict time by value_tokens;
    # validate here so a malformed value refuses startup, not a request.
    tokenizer_version()

    return make_app(
        fail_posture=posture,
        mode=os.environ.get("LEDVOUCH_MODE", FLAG),
        stage=os.environ.get("LEDVOUCH_STAGE", "C"),
        audit=audit_emitter_from_env(),
        retry_max=_env_int("LEDVOUCH_RETRY_MAX", INTERNAL_RETRY_MAX),
        pushback_max=_env_int("LEDVOUCH_PUSHBACK_MAX", PUSHBACK_MAX),
        answer_hash_header=_env_switch("LEDVOUCH_ANSWER_HASH_HEADER"),
        emit_observation=_env_switch("LEDVOUCH_AUDIT_OBSERVATION"),
        identity_headers=identity_headers,
        upstream_base=os.environ.get(
            "LEDVOUCH_UPSTREAM_BASE", "https://api.openai.com/v1").rstrip("/"),
        # E0 effect gate: designation is explicit operator configuration, never
        # inferred; a malformed declaration refuses startup (effect_gate.py).
        effect_terminals=parse_effect_terminals(
            os.environ.get("LEDVOUCH_EFFECT_TERMINALS")
        ),
    )
