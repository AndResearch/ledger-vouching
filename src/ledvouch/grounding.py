"""Φ — the deterministic grounding floor (COPIED verbatim from the predecessor shell).

# Provenance (do not edit the copied functions — mirror the source):
#   Source: the predecessor shell's grounding module (D0606 increment 1, 2026-07-12).
#   The three functions `load_bearing_tokens` / `grounded` / `sufficiency_peek`
#   are dependency-free pure functions (imports: re / dataclass / Iterable only),
#   so the "carve-out" is a COPY, not a redesign (E0100 §1, §10, handoff §0.4).
#   The shell's `resolve_ref` / `record_facts` / `ground_inputs` are NOT copied:
#   they depend on RequestContext and belong to the shell's request-scoped ledger.
#   The ledger voucher re-implements cross-step reference resolution over the wire-visible
#   conversation instead (see refs.py / ledger.py — handoff §2).
#
# Design rationale (unchanged from source; the internal design archive (frozen) is the authority):
#   A candidate `complete` answer is Sufficient only if its load-bearing tokens
#   (values: numbers, ids, names — not connective words) TRACE to evidence actually
#   observed (recorded tool outputs ∪ the goal text). A value the LLM fabricated or
#   computed in its head fails the floor.
#   Honest laws (SCAR-01 / 00186 / 00185):
#     1. No-fabrication: never Sufficient unless the floor passes.
#     2. Uncertainty → Insufficient (degrade; never a fabricated complete).
#     3. Unattainable → Help when the floor fails AND no means remain.
#   The floor is NECESSARY, not sufficient: it stops fabrication; semantic acceptance
#   stays with a run-level judge. The refusal reason carries WHICH tokens are
#   ungrounded and HOW to fix (actionable feedback at the error source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

SUFFICIENT = "sufficient"
INSUFFICIENT = "insufficient"
UNATTAINABLE = "unattainable"


@dataclass(frozen=True)
class Sufficiency:
    """The floor's verdict: {Sufficient | Insufficient | Unattainable} with the
    ungrounded tokens and an actionable reason on the refusing verdicts."""

    verdict: str
    missing: tuple[str, ...] = ()
    reason: str = ""


# A token: starts/ends on an alphanumeric, may carry id-ish interior punctuation
# (1,234 / ord_42 / #W2378156's W2378156 / 2026-07-12 / v1.2 / a/b).
_TOKEN_RE = re.compile(r"[0-9A-Za-z_](?:[0-9A-Za-z_.,:\-/]*[0-9A-Za-z_])?")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def load_bearing_tokens(text: str) -> tuple[str, ...]:
    """The answer tokens that must trace to evidence: digit-bearing tokens (numbers,
    ids, codes, dates) always; capitalized words of len>=2 when NOT sentence-initial
    (mid-sentence capitalization ≈ a proper name). Connective words are not
    load-bearing — the floor checks values, not prose."""
    tokens: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for i, tok in enumerate(_TOKEN_RE.findall(sentence)):
            if any(ch.isdigit() for ch in tok):
                tokens.append(tok)
            elif i > 0 and len(tok) >= 2 and tok[0].isupper():
                tokens.append(tok)
    return tuple(dict.fromkeys(tokens))  # dedupe, order-preserving


def _normalize(text: str) -> str:
    # Case-insensitive; thousands separators dropped so "1,234" matches "1234".
    return text.lower().replace(",", "")


def grounded(answer: str, evidence: Iterable[str]) -> tuple[bool, tuple[str, ...]]:
    """The deterministic floor: every load-bearing token of `answer` appears in the
    evidence corpus. Returns (ok, missing_tokens). An empty answer never grounds
    (the silent-empty scar — an empty claim establishes nothing)."""
    if not answer.strip():
        return False, ()
    corpus = _normalize("\n".join(evidence))
    missing = tuple(
        tok for tok in load_bearing_tokens(answer) if _normalize(tok) not in corpus
    )
    return not missing, missing


def sufficiency_peek(
    *, goal: str, answer: str, evidence: Iterable[str], progress_possible: bool
) -> Sufficiency:
    """Honest sufficiency verdict for a candidate answer over observed evidence.
    The goal text joins the corpus: restating an Env-given value is not fabrication."""
    ok, missing = grounded(answer, (goal, *evidence))
    if ok:
        return Sufficiency(verdict=SUFFICIENT)
    what = (
        f"these answer values do not appear in any observed tool output, recorded "
        f"fact, or the goal text: {', '.join(missing)}"
        if missing
        else "the answer is empty — an empty claim establishes nothing"
    )
    if progress_possible:
        return Sufficiency(
            verdict=INSUFFICIENT,
            missing=missing,
            reason=(
                f"complete refused (grounding floor): {what}. Ground each value in "
                f"your work: obtain or compute it with a tool so it appears in an "
                f"observed result, then complete again. Keep the final answer in "
                f"exactly the format the task requires — do not add evidence, "
                f"citations, or explanation to the answer text. Never state a value "
                f"you did not observe."
            ),
        )
    return Sufficiency(
        verdict=UNATTAINABLE,
        missing=missing,
        reason=(
            f"{what}; and no means remain to obtain them "
            f"(tool/context budget exhausted)"
        ),
    )
