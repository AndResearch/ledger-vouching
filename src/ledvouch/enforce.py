"""Enforcement — what the ledger voucher DOES when the terminal floor refuses.

# Design rationale (light/deep):
#   Three enforcement modes, in capability order. light/deep differ NOT in whether
#   provenance is guaranteed (both guarantee it) but in how completion is treated:
#     flag  — record the ungrounded leaves, ship the answer UNCHANGED (100% completion).
#     block — refuse: replace the answer with an honest "cannot report" (completion
#             is sacrificed; fail-closed).                              [stage C]
#     retry — close-gate push-back: keep the agent alive and make it fix the
#             ungrounded value, then complete (completion preserved).   [stage C]
#
#   stage A implements `flag` ONLY, and implements it as a BYTE-TRANSPARENT spectator:
#   the observation is recorded to an out-of-band sink and the response body handed
#   back to the client is unchanged. This is the strongest possible form of the
#   transparency claim we must first prove (the "spectator is transparent" proof) — with
#   a deterministic upstream (temp 0), a transparent proxy yields IDENTICAL rewards,
#   not merely statistically-matching ones. block/retry (which DO alter the body)
#   come only after transparency is confirmed.
#
# canon: fail-fast / no implicit repair. The refusal reason is
#   actionable (which value is ungrounded, how to fix) — carried already by
#   Sufficiency.reason from grounding.py. block/retry honor the run-or-help principle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .grounding import SUFFICIENT, Sufficiency

FLAG = "flag"
BLOCK = "block"
RETRY = "retry"

# ---- fail posture (A2) -------------------------------------------
#
# What happens when the LEDGER VOUCHER ITSELF fails where enforcement needed it (a
# stream cannot be parsed — no verdict; its retry call errors — an ungrounded
# answer it cannot push back on) is a customer governance choice, not a
# technical one. (Since v3 the value-map verdict is deterministic at mint time,
# so an attribution hidden-call failure is no longer a posture site — it is
# recorded and the verdict stands.)
#   open   — ship the model's original answer unverified (degrade to spectator).
#   closed — refuse: an unverified answer never reaches the client.
# The choice is MANDATORY configuration (no silent default); it only
# diverges in enforcement modes (block/retry). flag mode never alters the body by
# contract, so verification failures there stay flagged observations. Upstream
# unreachable on the MAIN forward has no answer to ship — both postures surface
# an honest 502. Every posture-decided outcome is recorded as an audit event.

FAIL_OPEN = "open"
FAIL_CLOSED = "closed"


@dataclass
class Observation:
    """One terminal-turn observation the ledger voucher records out-of-band (the audit
    trail / gate-(c) evidence). Never rides on the response body in stage A."""

    verdict: str
    missing: tuple[str, ...]
    mode: str
    shipped: bool  # did the client receive the model's original answer?
    answer: str = ""
    reason: str = ""
    # stage B enrichment (hidden call + refs + provenance walk), None on stage A runs.
    # verdict/missing above stay the stage A token floor — kept side-by-side so the
    # ref-template precision gain (gate (c)) is measurable against the same turn.
    stage_b: dict | None = None
    # content hashes of the observed answer (content_hash.py — the evidence
    # layer's join key). None when there is no parsed answer to hash (e.g. a
    # stream parse failure) — a visible absence, never an empty-string hash.
    sha_raw: str | None = None
    sha_canon: str | None = None


@dataclass
class ObservationSink:
    """In-memory collector the runner reads after a run (crash-safe persistence is the
    runner's job — the ledger voucher just accumulates)."""

    observations: list[Observation] = field(default_factory=list)

    def record(self, obs: Observation) -> None:
        self.observations.append(obs)


def apply_terminal_flag(
    *,
    answer: str,
    suff: Sufficiency,
    response_body: dict[str, Any],
    sink: ObservationSink,
    stage_b: dict | None = None,
    sha_raw: str | None = None,
    sha_canon: str | None = None,
) -> dict[str, Any]:
    """flag mode: record the verdict, return the response body UNCHANGED (byte
    transparent). Called only on terminal turns (no tool_calls in the response)."""
    sink.record(
        Observation(
            verdict=suff.verdict,
            missing=suff.missing,
            mode=FLAG,
            shipped=True,
            answer=answer,
            reason=suff.reason,
            stage_b=stage_b,
            sha_raw=sha_raw,
            sha_canon=sha_canon,
        )
    )
    return response_body


# ---- stage C enforcement (enabled 2026-07-17, after the stage A/B transparency proof:
# unit byte-identity + live 3/3 + tau2-bench arm A≡B Δ=0.000; body alteration is now the
# POINT, not an accident). Enforcement keys on the stage C value-map verdict, never on
# the stage A token floor — the floor's capitalized-common-word false positives (tau2-bench
# measured: ~23/35 insufficient turns) would cause false blocks.


def refusal_text(missing: list[str], reasons: dict[str, str]) -> str:
    """The honest refusal shipped in place of an ungrounded answer
    (run-or-help: state plainly WHAT could not be verified — never fabricate,
    never silently drop). Deterministic; audit-quotable."""
    lines = [
        "I can't give you that answer: the following value(s) could not be "
        "verified against any tool result I actually retrieved or information "
        "you provided:"
    ]
    for tok in missing:
        why = reasons.get(tok, "no verified origin")
        lines.append(f"- {tok}: {why}")
    lines.append(
        "Rather than report unverified figures, I'm stopping here. I can retry "
        "the lookups, or you can provide the missing information."
    )
    return "\n".join(lines)


def close_gate_feedback(missing: list[str], reasons: dict[str, str]) -> str:
    """The retry push-back sent to the MODEL (never seen by the client): the
    close-gate primitive — the reason names
    WHICH values are ungrounded and HOW to fix (actionable feedback at the error
    source), phrased like sufficiency_peek's refusal.

    Two wording elements added 2026-07-21 (measured, not stylistic — the
    internal induction-rig record of that date): a weak model
    (Qwen3-8B) re-asserted the same rejected values verbatim under the generic
    text (repairs 0/16 live), while the harvest-side close-gate loop that spelled
    out (1) do-not-repeat-rejected-values and (2) the remove-or-hypothesis
    fallback repaired 57.5% of the same episodes. Tool use stays the PRIMARY fix
    here (tools are alive in deep mode, unlike the frozen-prefix harvest loop)."""
    what = "; ".join(f"{tok} ({reasons.get(tok, 'no verified origin')})" for tok in missing)
    return (
        f"complete refused (grounding ledger voucher): these answer values do not trace "
        f"to any observed tool output or user-provided text: {what}. Ground each "
        f"value in your work: obtain or compute it with an available tool so it "
        f"appears in an observed result, then give the final answer again. Do not "
        f"state any rejected value above again unless a tool result you actually "
        f"obtained shows it. If a value cannot be obtained with the available "
        f"tools, remove it, or restate that claim as an explicitly unverified "
        f"hypothesis saying honestly that you could not verify it. Never state a "
        f"value you did not observe."
    )


def apply_terminal_block(
    *,
    answer: str,
    missing: list[str],
    reasons: dict[str, str],
    response_body: dict[str, Any],
) -> dict[str, Any]:
    """block mode — replace the ungrounded answer with the honest refusal. Returns a
    NEW body (the caller records the observation); the response envelope is kept,
    only choices[0].message.content changes (fail-closed, honest Help)."""
    blocked = {**response_body}
    choices = [dict(c) for c in (response_body.get("choices") or [])]
    if choices:
        msg = dict(choices[0].get("message") or {})
        msg["content"] = refusal_text(missing, reasons)
        choices[0]["message"] = msg
    blocked["choices"] = choices
    return blocked


def posture_refusal_text(reason: str) -> str:
    """The honest refusal shipped when the ledger voucher could not verify AND the
    deployment is fail-closed (run-or-help: state plainly what happened and why —
    never a fake answer, never a silent drop)."""
    return (
        "I can't deliver this answer: the grounding ledger voucher could not verify it "
        f"(verification unavailable: {reason}), and this deployment is configured "
        "fail-closed. Rather than ship an unverified answer, I'm stopping here. "
        "Please retry; if the problem persists, contact the deployment operator."
    )


def apply_posture_block(*, reason: str, response_body: dict[str, Any]) -> dict[str, Any]:
    """fail-closed replacement — same envelope-preserving shape as
    apply_terminal_block, but the refusal names the verification failure, not
    ungrounded values (there is no verdict to cite)."""
    blocked = {**response_body}
    choices = [dict(c) for c in (response_body.get("choices") or [])]
    if choices:
        msg = dict(choices[0].get("message") or {})
        msg["content"] = posture_refusal_text(reason)
        choices[0]["message"] = msg
    else:  # no parsable envelope (e.g. stream parse failure) — a minimal one
        choices = [{
            "index": 0,
            "message": {"role": "assistant", "content": posture_refusal_text(reason)},
            "finish_reason": "stop",
        }]
    blocked["choices"] = choices
    return blocked


def is_grounded(suff: Sufficiency) -> bool:
    return suff.verdict == SUFFICIENT
