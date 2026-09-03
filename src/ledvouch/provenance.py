"""Provenance tree + laundering walk over the wire-derived ledger (stage B B-3).

# Design rationale (lineage tree + laundering walk):
#   The ledger records, for every observed tool result, BOTH the output and the
#   arguments of the tool_call that produced it. That is enough to build, at the
#   terminal turn, the lineage of every value in the answer:
#       value → the tool_call whose output first contains it → that call's
#       arguments → each argument value's own origin → … (recurse).
#   Every leaf must be a prior tool output or user-given text. An argument value
#   with NO origin is an ungrounded literal leaf — a value the model computed in
#   its head and consumed as a tool argument so the terminal floor never saw it.
#   That is laundering, detected by walking the tree once at the terminal turn
#   (no per-turn intervention — junction-injection was rejected at design time).
#
#   This is PROVENANCE, not verification (the standing nail): the walk proves
#   where 5040 came from, never that 5040 = 4500 × 1.12 is the right arithmetic.
#   The tree doubles as the traceability matrix (the audit byproduct).
#
# Scope (deliberate): the walk covers VALUE tokens — the digit-bearing subset of
#   load_bearing_tokens (numbers, ids, dates, prices). The capitalized-word
#   heuristic is excluded here on purpose: it is the token-floor's observed
#   false-positive surface ("Request"/"Summary" — stage A NOTES), and stage B exists to
#   replace it with structural identification. Name provenance rides on the
#   hidden-call refs instead (hidden_call.py).
#
#   Only compute-type laundering is detectable by the walk (a NEW value with no
#   origin). Copy-type (a ledger value passed as literal instead of ref) is
#   grounded by construction here; closing it needs the strict-schema enum
#   — deferred until copy-type is actually observed.
#
# fail-fast honesty: origins are matched with the floor's normalization (substring
#   over normalized text) — match precision is the guarantee's precision; no
#   fuzzy repair, no "probably fine" defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

# grounding.py is the frozen Φ COPY — imported, never edited (_normalize is the
# floor's own normalization; the walk must ground exactly the way the floor does).
from .grounding import _TOKEN_RE, _normalize, load_bearing_tokens
from .ledger import Ledger


# Markdown list ordinals ("1. Headphones" / "**2.** ..." / "3) ...") tokenize as
# digit-bearing values and can have no origin — observed live as a pure
# false-ungrounded surface (stage C smoke 2026-07-17). Narrow strip: 1-2 digits with
# `.`/`)` right after a line-initial run of markdown decoration.
_ENUM_ORDINAL_RE = re.compile(r"(?m)^[\s*#>\-]{0,8}\d{1,2}[.)](?:[\s*]|$)")


# ---- tokenizer versioning (v2: decimal literals stay whole, 2026-08-27) ----
#
# v1 granularity: the Φ COPY's _SENTENCE_SPLIT_RE = [.!?\n]+ runs BEFORE
# tokenization, so a decimal literal splits at the period (1234.567 →
# '1234','567') and is audited as fragments. Fragment granularity is safe in the
# refusal direction (fragments rarely ground → flagged) but leaves a
# false-accept surface: a fabricated decimal passes when BOTH fragments
# independently ground somewhere in the observed record. v2 closes that surface
# at the split: a period BETWEEN digits is not a sentence boundary, so
# digits.digits survives as one token (_TOKEN_RE always allowed interior
# periods — only the pre-split broke decimals). Everything else — token shape,
# ordinal strip, digit filter, dedupe — is v1 by construction (differential
# tests pin the "identical except around digit.digit" contract).
#
# The version is deployment configuration (LEDVOUCH_TOKENIZER, default v1), not
# a code default: the tokenizer is a measuring instrument, and switching it
# changes published numbers (recall 814/814 etc. are v1-granularity), so v2
# ships as a versioned option — never a silent upgrade (the v1 lenient→strict
# inversion scar). Φ itself (grounding.py) stays untouched: versioning lives
# here, on the value_tokens side only.
_SENTENCE_SPLIT_RE_V2 = re.compile(r"(?:[!?\n]|(?<!\d)\.|\.(?!\d))+")
_TOKENIZER_VERSIONS = ("v1", "v2")


def tokenizer_version() -> str:
    """The deployed value-tokenizer version (LEDVOUCH_TOKENIZER, default v1).
    A malformed value raises — no silent default (validated again at startup)."""
    raw = os.environ.get("LEDVOUCH_TOKENIZER", "v1")
    if raw not in _TOKENIZER_VERSIONS:
        raise RuntimeError(
            f"LEDVOUCH_TOKENIZER must be one of {'|'.join(_TOKENIZER_VERSIONS)}, "
            f"got {raw!r} (no silent default)"
        )
    return raw


def _load_bearing_tokens_v2(text: str) -> tuple[str, ...]:
    # Mirror of the Φ COPY's load_bearing_tokens with the v2 sentence split; the
    # copied function is not edited (frozen), so the v2 variant lives here.
    tokens: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE_V2.split(text):
        for i, tok in enumerate(_TOKEN_RE.findall(sentence)):
            if any(ch.isdigit() for ch in tok):
                tokens.append(tok)
            elif i > 0 and len(tok) >= 2 and tok[0].isupper():
                tokens.append(tok)
    return tuple(dict.fromkeys(tokens))


def value_tokens(text: str, *, version: str | None = None) -> tuple[str, ...]:
    """The digit-bearing subset of load-bearing tokens: numbers, ids, dates, prices.
    (Capitalized-word tokens are deliberately excluded — see module docstring.
    Markdown enumeration ordinals are stripped — formatting, not values. Label
    tokens like `H3` are NOT stripped: they are checked like every value and
    ground through the numbering-scheme license below when — and only when —
    the wire shows they are document structure.)

    `version` pins the tokenizer explicitly (offline audits compare v1/v2 on the
    same record); None reads the deployment configuration (default v1)."""
    v = tokenizer_version() if version is None else version
    if v not in _TOKENIZER_VERSIONS:
        raise ValueError(f"unknown tokenizer version {v!r}")
    tokenize = load_bearing_tokens if v == "v1" else _load_bearing_tokens_v2
    return tuple(
        t
        for t in tokenize(_ENUM_ORDINAL_RE.sub(" ", text))
        if any(ch.isdigit() for ch in t)
    )


# ---- document-numbering license (scheme-licensing, 2026-07-21 user-approved) ----
#
# Contract-mandated section labels ("### H3: …" hypothesis numbering) are
# document STRUCTURE: their referent is a section of the answer itself, not the
# world. induction-rig measured them as the dominant clean-answer false-block driver and a
# structural retry blocker (a required H3 can ground in no lane). A global
# tokenizer exclusion (tried first, 2026-07-21 morning) kills the family but is
# NOT the durable form: it bakes a domain assumption ("H7 is never data") into
# domain-independent code, silently un-checks real H-shaped ids (invisible
# false-accept), and invites whack-a-mole for the next contract's Q1/S1/….
# The essential form DERIVES the license from the observed wire (same principle
# as the candidate mint): token <prefix><n> is structure iff ALL THREE hold —
#
#   1. anchor    — the CONTRACT text (user/policy lanes) instantiates the
#                  scheme: ≥2 distinct same-prefix numbers including 1
#                  ("H1, H2" exemplars; "H1..H5" ellipsis gives {1,5}).
#   2. run       — the answer's own same-prefix numbers form the consecutive
#                  run 1..m and n is inside it (a numbering licenses exactly
#                  the sections it actually has; a lone fabricated H7 is not
#                  a continuation of anything).
#   3. collision — NO tool output contains a same-prefix <prefix><digits>
#                  token. Tool outputs are the environment's voice — the only
#                  party that is neither the model nor the operator — so if the
#                  environment speaks in this shape, the shape is DATA here and
#                  the structure license yields (fails toward refusal, never
#                  toward acceptance; the disablement is recorded).
#
# Residual limit (stated in docs/claims_scope.md — customer-facing material):
# a fabricated label-shaped value passes as structure only when the contract
# anchors the scheme AND the answer fabricates the full consecutive run AND no
# tool result in the conversation ever shows the shape as data. Trail-visible
# ("contract-numbering" lane), never silent.

_SCHEME_TOKEN_RE = re.compile(r"([A-Za-z]{1,8})(\d+)")
# Boundary-aware extraction over raw normalized text (contract/tool lanes):
# tolerant of ellipsis and separator spellings ("h1..h5", "(h1/h2)") that fuse
# into one Φ token. The answer-side RUN reads the token stream instead — a fused
# answer spelling ("H1/H2") is a fusion-family value, not a clean label.
_SCHEME_IN_TEXT_RE = re.compile(
    r"(?<![0-9A-Za-z_])([A-Za-z]{1,8}?)(\d+)(?![0-9A-Za-z_])"
)


@dataclass(frozen=True)
class NumberingScheme:
    """The wire-derived license state for one label prefix (normalized)."""

    prefix: str
    anchored: bool  # contract instantiates the scheme (condition 1)
    collision: str | None  # data-shaped counter-evidence from tool outputs (condition 3)
    licensed_numbers: frozenset[int]  # answer-run numbers licensed (conditions 1+2+3)


def _text_scheme_numbers(texts: list[str]) -> dict[str, set[int]]:
    numbers: dict[str, set[int]] = {}
    for m in _SCHEME_IN_TEXT_RE.finditer(_normalize("\n".join(texts))):
        numbers.setdefault(m.group(1), set()).add(int(m.group(2)))
    return numbers


def numbering_schemes(tokens: tuple[str, ...], ledger: Ledger) -> dict[str, "NumberingScheme"]:
    """Compute the license state for every label prefix appearing in `tokens`.
    Pure and deterministic — no model involvement anywhere."""
    answer_numbers: dict[str, set[int]] = {}
    for tok in tokens:
        m = _SCHEME_TOKEN_RE.fullmatch(tok)
        if m:
            answer_numbers.setdefault(_normalize(m.group(1)), set()).add(int(m.group(2)))
    if not answer_numbers:
        return {}
    contract = _text_scheme_numbers([ledger.goal, *ledger.user_texts, *ledger.system_texts])
    data = _text_scheme_numbers([r.output for r in ledger.records])
    schemes: dict[str, NumberingScheme] = {}
    for prefix, present in answer_numbers.items():
        anchored = 1 in contract.get(prefix, set()) and len(contract.get(prefix, set())) >= 2
        collision = None
        if prefix in data:
            collision = f"{prefix}{min(data[prefix])}"
        licensed: set[int] = set()
        if anchored and collision is None:
            n = 1
            while n in present:  # the maximal consecutive run 1..m of the answer
                licensed.add(n)
                n += 1
        schemes[prefix] = NumberingScheme(
            prefix=prefix, anchored=anchored, collision=collision,
            licensed_numbers=frozenset(licensed),
        )
    return schemes


def structure_licensed(token: str, schemes: dict[str, "NumberingScheme"]) -> bool:
    """True ⇔ `token` is a licensed document-structure label under `schemes`."""
    m = _SCHEME_TOKEN_RE.fullmatch(token)
    if not m:
        return False
    scheme = schemes.get(_normalize(m.group(1)))
    return scheme is not None and int(m.group(2)) in scheme.licensed_numbers


@dataclass
class ProvenanceReport:
    """The terminal-turn walk's result: one lineage tree per answer value, plus the
    two failure surfaces (fabricated answer values / laundered argument values)."""

    tree: list[dict[str, Any]] = field(default_factory=list)
    ungrounded_answer: list[str] = field(default_factory=list)  # fabrication surface
    laundered: list[dict[str, Any]] = field(default_factory=list)  # compute-type


def build_provenance(answer: str, ledger: Ledger) -> ProvenanceReport:
    """Walk the lineage of every value token in `answer` over the ledger.

    A value's origin is the EARLIEST record whose output contains it (searching
    records strictly BEFORE the consuming call when tracing arguments); failing
    that, user-given text (goal ∪ every user message); failing that, ungrounded."""
    report = ProvenanceReport()
    given = _normalize("\n".join([ledger.goal, *ledger.user_texts]))
    policy = _normalize("\n".join(ledger.system_texts))
    outputs = [_normalize(r.output) for r in ledger.records]
    # Memoized on (token, upto): lineage is a DAG over strictly-decreasing record
    # indices, so the walk terminates; memoization keeps repeated values cheap.
    memo: dict[tuple[str, int], dict[str, Any]] = {}

    def trace(token: str, upto: int) -> dict[str, Any]:
        key = (_normalize(token), upto)
        if key in memo:
            return memo[key]
        node: dict[str, Any] = {"token": token}
        memo[key] = node  # placed before recursion; indices strictly decrease
        needle = _normalize(token)
        origin_idx = next(
            (i for i in range(upto) if needle in outputs[i]), None
        )
        if origin_idx is not None:
            rec = ledger.records[origin_idx]
            node["origin"] = "tool"
            node["step"] = origin_idx + 1
            node["tool"] = rec.name
            node["args"] = [
                trace(arg_tok, origin_idx)
                for arg_tok in value_tokens(rec.arguments_raw)
            ]
        elif needle in given:
            node["origin"] = "user"
        elif needle in policy:
            node["origin"] = "policy"  # operator-provided system text — legitimate
        else:
            node["origin"] = "ungrounded"
        return node

    # Document-numbering labels: licensed ANSWER-level tokens are structure, not
    # worldly claims — recorded with their own origin so the trail stays honest
    # (license state is wire-derived; see numbering_schemes). Argument tokens
    # (the laundering walk) never take this license: structure is a property of
    # the answer document only.
    schemes = numbering_schemes(value_tokens(answer), ledger)
    for tok in value_tokens(answer):
        if structure_licensed(tok, schemes):
            report.tree.append({"token": tok, "origin": "structure"})
        else:
            report.tree.append(trace(tok, len(ledger.records)))

    # Collect the two failure surfaces by walking the finished tree (kept separate
    # from trace(): memoized nodes are SHARED across depths, so recording during
    # recursion would drop a token that appears at both the answer level and an
    # argument level). Nodes are shared → dedupe by identity and by token.
    fabricated: dict[str, None] = {}
    laundered: dict[str, dict[str, Any]] = {}

    def collect(node: dict[str, Any], depth: int) -> None:
        if node["origin"] == "ungrounded":
            if depth == 0:
                fabricated.setdefault(node["token"])
            else:
                laundered.setdefault(node["token"], {"token": node["token"], "depth": depth})
        for child in node.get("args") or []:
            collect(child, depth + 1)

    for root in report.tree:
        collect(root, 0)
    report.ungrounded_answer = list(fabricated)
    report.laundered = list(laundered.values())

    # A laundered leaf names the call that consumed it (the audit wants "profit=900
    # went into tax_tool") — recover consuming steps by re-scanning arguments.
    for entry in report.laundered:
        needle = _normalize(entry["token"])
        entry["consumed_by"] = [
            {"step": i + 1, "tool": r.name}
            for i, r in enumerate(ledger.records)
            if needle in _normalize(r.arguments_raw)
        ]
    return report
