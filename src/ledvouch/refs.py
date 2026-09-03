"""refs — `$.<step>.<jsonpath>` resolution over the wire-derived ledger (stage B B-1).

# Design rationale (terminal hidden call):
#   The hidden call asks the model to rewrite its final answer with references of the
#   form `$.sN.<jsonpath>` (e.g. `$.s1.[0].revenue`) into the tool outputs the ledger voucher
#   already bookkeeps (ledger.py). This module resolves those references FROM THE
#   LEDGER — the model is never asked again (the model names the source; the ledger voucher
#   reads the value).
#
#   This re-implements the IDEA of shell grounding.py:resolve_ref (refuse, never
#   fabricate, on a dangling reference) — the shell version depends on RequestContext
#   and its step-fact ledger, so it cannot be copied; here the ledger is the
#   wire-visible conversation and the path language is JSONPath-ish.
#
# Boundary:
#   - JSON tool output → the path resolves STRICTLY (missing key/index refuses).
#   - non-JSON tool output → resolution DEGRADES to "the whole step output" so the
#     caller can at best do a step-scoped substring match. This is documented
#     false-accept surface: any token appearing anywhere in that step's output will
#     ground. `ResolvedRef.degraded` carries the honesty bit.
#
# fail-fast / no implicit repair: a dangling step or an unresolvable path
#   raises GroundingError with an actionable message — never a fabricated value,
#   never a silent default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger, ToolRecord


class GroundingError(Exception):
    """A reference that cannot be resolved from observed evidence (refuse, not repair)."""


# `$.sN` followed by JSONPath-ish segments: `[i]` (optionally dot-prefixed), `.key`,
# a quoted key `["key with space"]` (τ retail outputs carry keys like "switch type"
# — a bare-word grammar cannot express them and the model's underscored guess
# becomes a false dangling), or a bare NUMERIC segment `.7706410293` (τ `variants`
# dicts are keyed by numeric strings and models write them dot-bare; observed live
# 2026-07-17). Numeric/bracket segments resolve polymorphically — dict key first,
# then list index — which cannot fabricate: resolution either finds the recorded
# value or refuses.
_REF_RE = re.compile(
    r"\$\.s(\d+)"
    r"((?:\.?\[\d+\]|\.?\[\"[^\"\]]*\"\]|\.?\['[^'\]]*'\]"
    r"|\.\"[^\"]*\"|\.'[^']*'"  # dot-quoted key without brackets (observed live)
    r"|\.[A-Za-z0-9_][A-Za-z0-9_\-]*)*)"
)
_SEGMENT_RE = re.compile(
    r"\.?\[(\d+)\]|\.?\[\"([^\"\]]*)\"\]|\.?\['([^'\]]*)'\]"
    r"|\.\"([^\"]*)\"|\.'([^']*)'"
    r"|\.([A-Za-z0-9_][A-Za-z0-9_\-]*)"
)
# Models also drop the `$.` prefix (`s2.items.[2].price` — observed live); accept
# that spelling by normalization before the strict parse. This is grammar
# tolerance on OUR query language, not value repair — verification is unchanged.
_BARE_PREFIX_RE = re.compile(r"s\d+(?:[.\[].*)?$")


def find_refs(text: str) -> list[str]:
    """Every `$.sN...` reference in `text`, in order of appearance (with duplicates —
    the caller decides whether occurrences matter)."""
    return [m.group(0) for m in _REF_RE.finditer(text)]


@dataclass(frozen=True)
class ResolvedRef:
    """One reference resolved against the ledger."""

    ref: str
    step: int  # 1-based index into ledger.records
    record: ToolRecord
    value: str  # the resolved value; for degraded refs, the WHOLE step output
    degraded: bool  # True ⇔ non-JSON output → step-scoped substring semantics


def _value_to_text(value: Any) -> str:
    """Render a resolved JSON value for textual comparison: strings stay bare
    (no quotes), scalars/containers via json.dumps (deterministic)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def resolve_ref(ref: str, ledger: Ledger) -> ResolvedRef:
    """Resolve one `$.sN.<jsonpath>` against the ledger, or refuse (GroundingError).

    JSON output → strict path walk. Non-JSON output → degrade to the whole step
    output (degraded=True; the caller's match is step-scoped substring, false-accept
    possible — the documented boundary)."""
    raw = ref.strip()
    if _BARE_PREFIX_RE.fullmatch(raw):
        raw = "$." + raw  # accept the `$.`-less spelling (see _BARE_PREFIX_RE)
    m = _REF_RE.fullmatch(raw)
    if m is None:
        raise GroundingError(
            f"reference {ref!r} is not of the form $.sN.<jsonpath> — unparsable"
        )
    step = int(m.group(1))
    if not 1 <= step <= len(ledger.records):
        raise GroundingError(
            f"reference {ref!r}: step s{step} does not exist — the ledger has "
            f"{len(ledger.records)} observed tool result(s) (dangling step refused)"
        )
    record = ledger.records[step - 1]

    try:
        node: Any = json.loads(record.output)
    except (json.JSONDecodeError, ValueError):
        # Non-JSON tool output: the path cannot be walked. Degrade to the whole
        # step output (step-scoped substring semantics) — never fabricate.
        return ResolvedRef(
            ref=ref, step=step, record=record, value=record.output, degraded=True
        )

    # Tabular convention: a {"columns": [...], "rows": [[...]]} output (the common
    # SQL-tool shape) is naturally referenced BY COLUMN NAME ($.sN.rows.[0].revenue)
    # while the row is positional. Column-name → index via the step's own
    # `columns` array is deterministic and verified — measured as the DOMINANT
    # false-dangling family on Northwind reports (2026-07-17: e.g. 86-missing
    # turns mostly this shape).
    columns = (
        node.get("columns")
        if isinstance(node, dict) and isinstance(node.get("columns"), list)
        else None
    )

    path = m.group(2) or ""
    for seg in _SEGMENT_RE.finditer(path):
        idx = seg.group(1)
        key = next((g for g in seg.groups()[1:] if g is not None), None)
        token = idx if idx is not None else key
        # Polymorphic step: dict key first (numeric-string keys are common in τ
        # outputs), then list index, then the tabular spellings. Every branch
        # either finds the recorded value or refuses.
        is_table_root = isinstance(node, dict) and columns is not None and "rows" in node
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        elif (
            isinstance(node, list)
            and columns is not None
            and token in columns
            and columns.index(token) < len(node)
        ):
            node = node[columns.index(token)]
        elif (
            # rows-omitted index spelling: $.sN.[0].col — [i] on the table root
            # hops into rows (measured NW round 2: the 2nd dominant false family).
            is_table_root
            and token.isdigit()
            and isinstance(node["rows"], list)
            and int(token) < len(node["rows"])
        ):
            node = node["rows"][int(token)]
        elif (
            # single-row shorthand: $.sN.col on the table root — unambiguous ONLY
            # when the table has exactly one row; multi-row stays refused.
            is_table_root
            and token in columns
            and isinstance(node["rows"], list)
            and len(node["rows"]) == 1
            and isinstance(node["rows"][0], list)
            and columns.index(token) < len(node["rows"][0])
        ):
            node = node["rows"][0][columns.index(token)]
        else:
            have = sorted(node) if isinstance(node, dict) else type(node).__name__
            raise GroundingError(
                f"reference {ref!r}: segment {token!r} not present in step s{step}'s "
                f"JSON output (has: {have}) — dangling path refused"
            )
    return ResolvedRef(
        ref=ref, step=step, record=record, value=_value_to_text(node), degraded=False
    )
