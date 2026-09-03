"""Hidden call — the terminal ref-template rewrite and its evaluation (stage B B-2).

# Design rationale (the terminal hidden call):
#   On a terminal turn (no tool_calls in the response) the proxy — before handing
#   the answer back — makes ONE extra request of its own to the same endpoint:
#   the whole conversation + the model's answer + an instruction to rewrite that
#   answer with `$.sN.<jsonpath>` references into the observed tool results
#   (strict json_schema {answer_refs: string}, tools disabled). The refs are then
#   resolved FROM THE LEDGER (refs.py — the model is never asked for values) and
#   compared against the original answer. The model names the sources; the ledger
#   supplies the values; the comparison is deterministic.
#
#   stage B keeps the verdict a FLAG: the response body shipped to the client is
#   never altered (stage A's proven transparency is the standing constraint).
#   block/retry on this verdict are stage C.
#
# What the evaluation separates (derived vs copied, reflected in observation):
#   - dangling      : a ref that does not resolve (nonexistent step/path) — the
#                     model claimed a source that is not in the ledger.
#   - ungrounded    : original-answer values covered by neither the resolved refs
#                     nor user-given text — fabrication / mismatch (the 3200-vs-4500 case).
#   - bare_ungrounded: literal values in the rewrite that exist nowhere observed.
#   - literal_copies: literal values in the rewrite that DO exist in tool outputs —
#                     copy-type laundering surface, recorded (not refused) so the
#                     copy-type enum decision can be made on data, not speculation.
#
# fail-fast / no implicit repair: an unparsable hidden-call response is
#   surfaced as an error observation, never coerced; dangling refs are never
#   substituted with a guessed value (a marker keeps them visibly unresolved).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .grounding import _normalize
from .ledger import Ledger
from .provenance import (
    NumberingScheme,
    _SCHEME_TOKEN_RE,
    numbering_schemes,
    structure_licensed,
    value_tokens,
)
from .refs import _REF_RE, GroundingError, resolve_ref

# strict json_schema per the OpenAI structured-output contract. Name obeys the
# endpoint name regex (measured scar: an invalid name is a 400 mislabeled upstream).
_ANSWER_REFS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer_refs",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer_refs": {"type": "string"}},
            "required": ["answer_refs"],
            "additionalProperties": False,
        },
    },
}

_LEGEND_ARGS_CHARS = 200
_LEGEND_OUTPUT_CHARS = 200


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def build_hidden_request(
    original_body: dict[str, Any], answer: str, ledger: Ledger
) -> dict[str, Any]:
    """The proxy's own request: original conversation + the answer + the rewrite
    instruction. Sampling follows the original request (temperature copied when
    present); tools are never offered (the hidden call must never let the model
    invoke new tools)."""
    legend_lines = [
        f"s{i + 1} = {r.name}({_clip(r.arguments_raw, _LEGEND_ARGS_CHARS)})"
        f" -> {_clip(r.output, _LEGEND_OUTPUT_CHARS)}"
        for i, r in enumerate(ledger.records)
    ]
    legend = "\n".join(legend_lines) if legend_lines else "(no tool results observed)"
    instruction = (
        "The assistant message above is the final answer. Rewrite it EXACTLY, but "
        "replace every concrete value (number, id, date, price, quantity) that came "
        "from a tool result with a reference of the form $.sN.<jsonpath> into the "
        "tool results listed below (e.g. $.s1.[0].revenue, $.s2.order_id, "
        '$.s3.items.[1].price, $.s3.items.[2].options.["switch type"]).\n'
        "Rules:\n"
        "- Use the step numbers exactly as listed below.\n"
        '- Use JSON key names EXACTLY as they appear in the output; quote keys that '
        'contain spaces: ["like this"].\n'
        "- Values the user themselves stated in the conversation may stay literal.\n"
        "- Never write a bare number that came from a tool result; use its reference.\n"
        "- Do not add, remove, or reword any information; only substitute references.\n"
        f"Tool results observed in this conversation:\n{legend}"
    )
    hidden: dict[str, Any] = {
        "model": original_body.get("model"),
        "messages": [
            *(original_body.get("messages") or []),
            {"role": "assistant", "content": answer},
            {"role": "user", "content": instruction},
        ],
        "response_format": _ANSWER_REFS_SCHEMA,
    }
    if "temperature" in original_body:
        hidden["temperature"] = original_body["temperature"]
    return hidden


def parse_answer_refs(response_body: dict[str, Any]) -> str:
    """Extract answer_refs from the hidden call's response — parsed with exactly the
    shape that was requested (request/parse same shape; no repair on mismatch)."""
    choices = response_body.get("choices") or []
    if not choices:
        raise GroundingError("hidden call returned no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise GroundingError("hidden call returned empty content")
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        raise GroundingError(f"hidden call content is not the requested JSON: {e}")
    refs = obj.get("answer_refs") if isinstance(obj, dict) else None
    if not isinstance(refs, str):
        raise GroundingError("hidden call JSON lacks a string 'answer_refs' field")
    return refs


@dataclass
class StageBResult:
    """The deterministic evaluation of one hidden-call rewrite."""

    answer_refs: str
    rendered: str
    refs: list[str] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)  # ref -> value (clipped)
    degraded_refs: list[str] = field(default_factory=list)  # non-JSON substring scope
    dangling: list[dict[str, str]] = field(default_factory=list)  # ref + reason
    ungrounded: list[str] = field(default_factory=list)  # original values uncovered
    bare_ungrounded: list[str] = field(default_factory=list)
    literal_copies: list[str] = field(default_factory=list)  # copy-type surface
    verdict: str = "grounded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "answer_refs": _clip(self.answer_refs, 2000),
            "rendered": _clip(self.rendered, 2000),
            "refs": self.refs,
            "resolved": {k: _clip(v, 200) for k, v in self.resolved.items()},
            "degraded_refs": self.degraded_refs,
            "dangling": self.dangling,
            "ungrounded": self.ungrounded,
            "bare_ungrounded": self.bare_ungrounded,
            "literal_copies": self.literal_copies,
        }


# ---- stage C value-map hidden call (v3: candidate-path enum — candidate-path enum) -----------
#
# Why a third shape (measured, not speculative — the internal induction-rig
# record, 2026-07-21):
#   v2 pinned the VALUES but let the model DESCRIBE each source free-form. Live
#   attribution failure — a weak model answering unknown/wrong-source for values
#   it demonstrably observed — was the dominant false-block driver (Session 2:
#   clean answers blocked 4/4) and made retry convergence key on attribution
#   noise (repairs 0/16, "stagnant" at round 1). The ledger voucher holds both the
#   ledger and the tokens, so v3 REVERSE-LOOKS-UP the verified origin lanes
#   itself (productized from the measurement rig's best-case attributor —
#   recall 814/814 on replay) and
#   pins them per token in the schema; the model only SELECTS. Dangling,
#   misdescription and spelling variance become schema-impossible.
#
# Enforcement semantics (user decision 2026-07-21): the enforcement key is
#   CANDIDATE EXISTENCE — a token with at least one verified origin lane is
#   grounded; a token with none is missing. This is Φ's substring semantics
#   computed per-lane at mint time: the verdict is deterministic and does not
#   depend on the model's answer at all (refuse-not-trust taken to its end —
#   the model's say-so can no longer cause a false refusal either). The model's
#   selection is provenance-TRAIL material only; a selection outside the minted
#   candidates is recorded invalid, never repaired, never enforced on.
#
# Origin lanes (user decision 2026-07-21): user / policy / whole-step $.sN
#   (resolve_ref semantics; non-JSON steps carry the degraded honesty bit).
#   Tool-call ARGUMENTS are deliberately NOT a lane: evidence is non-model-
#   authored wire text only — grounding a value to the model's own arguments
#   would let a fabricated value launder itself through any tool call (audit
#   independence; claims_scope "derivation parameters are recorded, not
#   enforced on").
#   Scars honored: parallel-list FK is fragile → each map entry carries value AND
#   source inline; request/prompt/parse share one shape (the prompt lists exactly
#   the capped candidate sets the schema pins); strict schema stays shallow.

# Schema-size backstop: candidates per token are capped in the SCHEMA and prompt
# (lane order: user, policy, $.sN ascending). The verdict is computed on the
# UNCAPPED mint, so the cap can never flip grounded→missing — it only coarsens
# the trail for pathologically value-dense steps. Generous on purpose; tighten
# only on measured legend inflation (induction-rig measured).
_CANDIDATE_CAP = 16


@dataclass(frozen=True)
class OriginLane:
    """One verified origin lane for one token: the mint proved the token is
    contained in this lane's observed text."""

    source: str  # "user" | "policy" | "$.sN"
    degraded: bool  # True ⇔ non-JSON step output (substring scope — documented boundary)


def mint_candidates(
    tokens: tuple[str, ...], ledger: Ledger
) -> dict[str, tuple[OriginLane, ...]]:
    """Reverse lookup: for each token, every origin lane the verifier accepts —
    computed with the SAME primitives the old claim-verification used
    (resolve_ref + _normalize), so mint acceptance ≡ verifier acceptance.
    Deterministic and pure; 'unknown' is never a lane (it is appended to the
    schema enum only, as the model's honest opt-out for the trail)."""
    user_norm = _normalize("\n".join([ledger.goal, *ledger.user_texts]))
    policy_norm = _normalize("\n".join(ledger.system_texts))
    steps: list[tuple[str, str, bool]] = []
    for i in range(1, len(ledger.records) + 1):
        try:
            res = resolve_ref(f"$.s{i}", ledger)
        except GroundingError:
            continue
        steps.append((f"$.s{i}", _normalize(res.value), res.degraded))
    schemes = numbering_schemes(tokens, ledger)
    out: dict[str, tuple[OriginLane, ...]] = {}
    for tok in tokens:
        n = _normalize(tok)
        lanes: list[OriginLane] = []
        if n in user_norm:
            lanes.append(OriginLane("user", False))
        if n in policy_norm:
            lanes.append(OriginLane("policy", False))
        if structure_licensed(tok, schemes):
            # document-numbering license (provenance.py rationale): the label is
            # structure the contract established — a wire-derived lane, not a
            # tokenizer blind spot.
            lanes.append(OriginLane("contract-numbering", False))
        lanes.extend(
            OriginLane(ref, degraded)
            for ref, text, degraded in steps
            if n in text
        )
        out[tok] = tuple(lanes)
    return out


def _capped_sources(lanes: tuple[OriginLane, ...]) -> list[str]:
    """The candidate list the schema pins and the prompt shows — one shared
    computation so request/prompt/parse stay the same shape."""
    return [lane.source for lane in lanes[:_CANDIDATE_CAP]]


def _value_map_schema(
    tokens: tuple[str, ...], candidates: dict[str, tuple[OriginLane, ...]]
) -> dict[str, Any]:
    """Strict schema: one anyOf branch per candidate-bearing token, its `source`
    enum pinned to that token's verified lanes (+ "unknown"). Candidate-zero
    tokens are not offered — there is nothing to select; they are already
    missing by mint."""
    branches = [
        {
            "type": "object",
            "properties": {
                "value": {"type": "string", "enum": [tok]},
                "source": {
                    "type": "string",
                    "enum": [*_capped_sources(candidates[tok]), "unknown"],
                },
            },
            "required": ["value", "source"],
            "additionalProperties": False,
        }
        for tok in tokens
        if candidates.get(tok)
    ]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "value_grounding_map",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "values": {"type": "array", "items": {"anyOf": branches}}
                },
                "required": ["values"],
                "additionalProperties": False,
            },
        },
    }


def build_value_map_request(
    original_body: dict[str, Any],
    answer: str,
    ledger: Ledger,
    tokens: tuple[str, ...],
    candidates: dict[str, tuple[OriginLane, ...]],
) -> dict[str, Any]:
    """The stage C hidden call, v3: for each candidate-bearing token the model
    SELECTS the true origin among the pre-verified candidates (provenance-trail
    material — the verdict is already fixed by the mint). Same conversation
    context and legend as before; tools never offered."""
    legend_lines = [
        f"s{i + 1} = {r.name}({_clip(r.arguments_raw, _LEGEND_ARGS_CHARS)})"
        f" -> {_clip(r.output, _LEGEND_OUTPUT_CHARS)}"
        for i, r in enumerate(ledger.records)
    ]
    legend = "\n".join(legend_lines) if legend_lines else "(no tool results observed)"
    listed = "\n".join(
        f"- {tok}: {', '.join(_capped_sources(candidates[tok]))}"
        for tok in tokens
        if candidates.get(tok)
    )
    instruction = (
        "The assistant message above is the final answer. For EACH value listed "
        "below, select which of its candidate sources it ACTUALLY came from. "
        "Every candidate has already been verified to contain the value; you are "
        "naming the true origin for the audit trail:\n"
        f"{listed}\n"
        "Candidate meanings: $.sN = the tool result of step N listed below; "
        '"user" = the user themselves stated it; "policy" = your system '
        'instructions state it. Select "unknown" ONLY if none of the listed '
        "candidates is where the value actually came from. Answer with the "
        "candidate exactly as listed — no explanations, no annotations.\n"
        f"Tool results observed in this conversation:\n{legend}"
    )
    hidden: dict[str, Any] = {
        "model": original_body.get("model"),
        "messages": [
            *(original_body.get("messages") or []),
            {"role": "assistant", "content": answer},
            {"role": "user", "content": instruction},
        ],
        "response_format": _value_map_schema(tokens, candidates),
    }
    if "temperature" in original_body:
        hidden["temperature"] = original_body["temperature"]
    return hidden


def parse_value_map(response_body: dict[str, Any]) -> list[dict[str, str]]:
    """Extract the value->source map — parsed with exactly the requested shape."""
    choices = response_body.get("choices") or []
    if not choices:
        raise GroundingError("hidden call returned no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise GroundingError("hidden call returned empty content")
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        raise GroundingError(f"hidden call content is not the requested JSON: {e}")
    values = obj.get("values") if isinstance(obj, dict) else None
    if not isinstance(values, list):
        raise GroundingError("hidden call JSON lacks a 'values' array")
    out: list[dict[str, str]] = []
    for entry in values:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("value"), str)
            or not isinstance(entry.get("source"), str)
        ):
            raise GroundingError(f"malformed map entry: {entry!r}")
        out.append({"value": entry["value"], "source": entry["source"]})
    return out


@dataclass
class ValueMapResult:
    """Deterministic existence verdict + the model's selection as trail material.
    `missing` is the enforcement key: candidate-zero tokens — values with no
    verified origin lane at all (with per-value reasons)."""

    mapping: list[dict[str, str]] = field(default_factory=list)
    candidates: dict[str, list[str]] = field(default_factory=dict)  # token -> lanes
    grounded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # value tokens, deduped, ordered
    reasons: dict[str, str] = field(default_factory=dict)  # value -> actionable why
    degraded: list[str] = field(default_factory=list)  # grounded ONLY via non-JSON scope
    attribution: dict[str, str] = field(default_factory=dict)  # token -> chosen lane
    attribution_invalid: list[dict[str, str]] = field(default_factory=list)
    verdict: str = "grounded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "mapping": self.mapping,
            "candidates": self.candidates,
            "grounded": self.grounded,
            "missing": self.missing,
            "reasons": self.reasons,
            "degraded": self.degraded,
            "attribution": self.attribution,
            "attribution_invalid": self.attribution_invalid,
        }


def evaluate_value_map(
    tokens: tuple[str, ...],
    candidates: dict[str, tuple[OriginLane, ...]],
    mapping: list[dict[str, str]],
    schemes: dict[str, NumberingScheme] | None = None,
) -> ValueMapResult:
    """The existence verdict over the mint, plus validation of the model's
    selections for the provenance trail. Grounded ⇔ at least one verified lane
    exists — the model's answer cannot move the verdict in either direction.
    A selection outside the token's minted candidates (a non-compliant backend)
    is recorded invalid — never resolved, never repaired, never enforced on.
    `schemes` (optional) enriches candidate-zero reasons when a numbering
    license was disabled by the data-collision guard — actionable for retry."""
    result = ValueMapResult(
        mapping=list(mapping),
        candidates={tok: [lane.source for lane in candidates.get(tok, ())] for tok in tokens},
    )
    by_value: dict[str, str] = {}
    for entry in mapping:
        by_value.setdefault(entry["value"], entry["source"])

    for tok in tokens:
        lanes = candidates.get(tok, ())
        if not lanes:
            result.missing.append(tok)
            reason = (
                "no observed origin: the value appears in no tool result, "
                "no user message, and no policy text"
            )
            m = _SCHEME_TOKEN_RE.fullmatch(tok)
            scheme = (schemes or {}).get(_normalize(m.group(1))) if m else None
            if scheme is not None and scheme.anchored and scheme.collision:
                reason += (
                    f" (document-numbering licensing for prefix "
                    f"{scheme.prefix!r} is disabled here: the same shape "
                    f"appears as data in tool results, e.g. {scheme.collision!r}"
                    f" — obtain the value from a tool or renumber)"
                )
            result.reasons[tok] = reason
            continue
        result.grounded.append(tok)
        if all(lane.degraded for lane in lanes):
            result.degraded.append(tok)
        chosen = by_value.get(tok)
        if chosen is None:
            continue  # unselected — visible by absence from `attribution`
        if chosen == "unknown" or chosen in {lane.source for lane in lanes}:
            result.attribution[tok] = chosen
        else:
            result.attribution_invalid.append({"value": tok, "source": chosen})
    if result.missing:
        result.verdict = "ungrounded"
    return result


def evaluate_answer_refs(answer: str, answer_refs: str, ledger: Ledger) -> StageBResult:
    """Resolve every ref in the rewrite from the ledger and judge the ORIGINAL
    answer against what actually resolves. Deterministic; the model is not
    consulted again."""
    result = StageBResult(answer_refs=answer_refs, rendered="")

    def substitute(m: re.Match[str]) -> str:
        ref = m.group(0)
        if ref not in result.refs:
            result.refs.append(ref)
        try:
            res = resolve_ref(ref, ledger)
        except GroundingError as e:
            if not any(d["ref"] == ref for d in result.dangling):
                result.dangling.append({"ref": ref, "reason": str(e)})
            return f"<dangling:{ref}>"  # visibly unresolved — never a guessed value
        result.resolved.setdefault(ref, res.value)
        if res.degraded and ref not in result.degraded_refs:
            result.degraded_refs.append(ref)
        return res.value

    result.rendered = _REF_RE.sub(substitute, answer_refs)

    given = _normalize("\n".join([ledger.goal, *ledger.user_texts]))
    evidence = _normalize("\n".join(ledger.evidence()))

    # Literal values remaining in the rewrite (refs stripped before tokenizing).
    stripped = _REF_RE.sub(" ", answer_refs)
    for tok in value_tokens(stripped):
        n = _normalize(tok)
        if n in given:
            continue  # user-stated literals are allowed by the instruction
        if n in evidence:
            if tok not in result.literal_copies:
                result.literal_copies.append(tok)  # copy-type: grounded but unref'd
        elif tok not in result.bare_ungrounded:
            result.bare_ungrounded.append(tok)

    # Direction 1 (the mismatch/coverage check): every value in the ORIGINAL answer must
    # appear in what the refs actually resolve to (∪ user-given text). Full-evidence
    # substring match is deliberately NOT used here — that would collapse stage B back
    # into the stage A token floor; coverage must come through the declared refs.
    corpus_b = _normalize("\n".join([given, result.rendered]))
    result.ungrounded = [
        tok for tok in value_tokens(answer) if _normalize(tok) not in corpus_b
    ]

    if result.dangling or result.bare_ungrounded or result.ungrounded:
        result.verdict = "ungrounded"
    return result
