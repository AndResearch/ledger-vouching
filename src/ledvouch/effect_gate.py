"""Effect-terminal gate — judge and (stage E1) enforce designated side-effect calls.

# Design rationale (effect boundary; stages E0 observation → E1 enforcement):
#   The terminal turn is one instance of a more general boundary: the last point
#   the ledger voucher can intervene before an irreversible effect leaves the wire —
#   a final text answer reaches the user; a designated tool call (a ledger
#   write, a document render) reaches the harness that will EXECUTE it. This
#   module generalizes the verdict engine to that boundary: the judged object
#   changes from answer text to the call's arguments; the machinery (value
#   tokens → candidate mint reverse-lookup → deterministic existence verdict)
#   is the stage-C v3 engine unchanged. Design authority: the effect-terminal
#   design draft (2026-08-15, HQ docs), decisions D1-D5 fixed 2026-08-17.
#
#   Stage E1 (2026-08-17) adds enforcement: a designated call whose declared
#   data values lack an observed origin is stopped BEFORE execution (block), or
#   pushed back ledger voucher-internally for the model to fix (retry, D5 synthetic
#   tool result) — the harness never executes a call the gate refused. The
#   asymmetry with the text terminal is deliberate (D1): a text answer that
#   degrades to flag can still be caught by a human, but a side-effect's
#   execution IS the harm — so retry non-convergence degrades to BLOCK, not to
#   flag (per-tool operator override to "flag" exists for the observation
#   introduction period — D2 — and is itself visible configuration).
#   This module stays a pure library (§9.3 placement): verdicts, decisions and
#   body transforms live here; proxy.py only wires them to the HTTP paths.
#
# The two directions of the standing "arguments" nails, kept distinct on purpose:
#   - args are NOT a grounding lane (2026-07-21 user decision): an argument can
#     never make a value count as observed — that would be a self-grounding
#     laundering hole. Unchanged here: the mint never reads arguments.
#   - args ARE a judged object (this module): the gate asks where the argument
#     values CAME FROM. The reverse direction opens no hole.
#   - arguments are never REWRITTEN (fail-dangerous, claims_scope): a designated
#     call ships exactly as the model wrote it, or (E1) not at all. The ledger voucher
#     never originates a side-effect call.
#
# data / param declaration (D4'): argument values are ontologically two kinds —
#   data values being written (amounts, dates, ids: must ground) and derivation
#   parameters (thresholds, filter constants: the model's or the user's
#   JUDGMENT, which by nature has no observable origin — the family
#   claims_scope already carves out as recorded-not-enforced). No semantics can
#   split them deterministically, so the boundary is the operator's DECLARED
#   configuration: per tool, data_fields (judged) and param_fields (recorded in
#   the trail, never enforced on, never a lane). Default = every argument is
#   data — errs toward refusal, never toward silent acceptance. The declaration
#   itself is auditable surface (same standing as the fail posture).
#
# kind (D3'): scheme-licensing (contract-numbering) follows the judged object's
#   kind, not a toggle. "document" fields (a render source IS an answer-shaped
#   document) keep the license machinery — its guards only ever err toward
#   refusal. "record" fields (an API write) carry data only: a label-shaped
#   argument value is data by declaration, so the structure license does not
#   apply (the same line provenance.py draws for the laundering walk:
#   "structure is a property of the answer document only").
#
# fail-fast configuration: unknown keys and invalid values refuse startup —
#   nothing is coerced, defaulted around, or ignored (a configuration that
#   silently does nothing is a silent fail-open).
#
# Receipt correlation: tool_call_id is the wire-native join key. A judged call's
#   observed result (next request's role:"tool" message) is emitted as an
#   effect_receipt carrying verdict_at_call — the write-time stain survives on
#   the trail even though the receipt's echoed values enter the ledger as
#   legitimately observed from then on (flag-mode post-execution laundering,
#   design draft §4.3: the evidence layer must propagate this stain).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from .content_hash import answer_hashes
from .enforce import posture_refusal_text
from .hidden_call import evaluate_value_map, mint_candidates
from .ledger import Ledger
from .provenance import numbering_schemes, value_tokens

KIND_RECORD = "record"
KIND_DOCUMENT = "document"

# Verdicts: grounded / ungrounded come from the existence verdict; the two
# observation-only outcomes are visible absences, never silent skips.
VERDICT_SKIPPED = "skipped"  # no data-value tokens — nothing to ground
VERDICT_UNPARSEABLE = "unparseable"  # arguments are not JSON — no verdict possible

# Stage tag carried on effect_* audit events (the gate generation, not the
# deployment's text-terminal stage).
EFFECT_STAGE = "E1"

# Per-tool enforcement (E1). mode=None follows the deployment's global mode;
# degrade is the retry non-convergence target — "block" by default (D1: an
# executed unverified write is the harm itself) with an explicit per-tool
# "flag" override for observation-period rollouts (D2 — the one true dial,
# shaped like the A2 posture: explicit, visible in config and audit).
_MODES = ("flag", "block", "retry")
_DEGRADES = ("block", "flag")

_ALLOWED_KEYS = {
    "tool", "data_fields", "param_fields", "kind", "mode", "degrade",
    "receipt_fields",
}

# Backstop for calls whose receipts never return (a harness that drops the
# conversation): the pending map is bounded; evictions are counted in metrics —
# visible, never silent.
PENDING_MAX = 4096


class EffectConfigError(RuntimeError):
    """A malformed LEDVOUCH_EFFECT_TERMINALS declaration — a startup refusal
    (no silent default, no partial acceptance), same standing as A2."""


@dataclass(frozen=True)
class EffectTerminal:
    """One designated side-effect tool (operator-declared; never inferred)."""

    tool: str
    data_fields: tuple[str, ...] | None  # None = every argument is data (default)
    param_fields: tuple[str, ...]
    kind: str  # KIND_RECORD | KIND_DOCUMENT
    mode: str | None = None  # None = follow the deployment's global mode
    degrade: str = "block"  # retry non-convergence target (D1; "flag" = D2 override)
    # Domain data for the receipt correlation (§3.4: the domain-specific part
    # of the crosswalk is ONLY additional data riding the correlation — e.g.
    # freee's journal id): dot paths extracted from the observed result and
    # carried on the effect_receipt event. Declared-but-absent paths are
    # listed visibly — never silently dropped.
    receipt_fields: tuple[str, ...] = ()


def effective_mode(terminal: EffectTerminal, global_mode: str) -> str:
    return terminal.mode if terminal.mode is not None else global_mode


def _validate_path(path: Any, *, tool: str, key: str) -> str:
    if not isinstance(path, str) or not path.startswith("$.") or len(path) <= 2:
        raise EffectConfigError(
            f"effect terminal {tool!r}: {key} entry {path!r} must be a dot path "
            f"starting with '$.' (e.g. '$.amount', '$.journal.lines')"
        )
    segments = path[2:].split(".")
    if any(not s for s in segments):
        raise EffectConfigError(
            f"effect terminal {tool!r}: {key} entry {path!r} has an empty path segment"
        )
    return path


def _under(path: str, declared: str) -> bool:
    return path == declared or path.startswith(declared + ".")


def parse_effect_terminals(raw: str | None) -> dict[str, EffectTerminal]:
    """Parse the LEDVOUCH_EFFECT_TERMINALS JSON declaration. Unset/empty → {}
    (the gate is fully inert — the unconfigured deployment is byte-identical to
    a build without this module). Any malformation is a startup refusal with an
    actionable message; nothing is coerced, defaulted around, or ignored."""
    if raw is None or not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EffectConfigError(f"LEDVOUCH_EFFECT_TERMINALS is not valid JSON: {e}")
    if not isinstance(entries, list):
        raise EffectConfigError(
            f"LEDVOUCH_EFFECT_TERMINALS must be a JSON list of objects, "
            f"got {type(entries).__name__}"
        )
    terminals: dict[str, EffectTerminal] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise EffectConfigError(
                f"LEDVOUCH_EFFECT_TERMINALS entries must be objects, got {entry!r}"
            )
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool:
            raise EffectConfigError(
                f"LEDVOUCH_EFFECT_TERMINALS entry {entry!r} needs a non-empty "
                f"'tool' (the tool name exactly as it appears on the wire)"
            )
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise EffectConfigError(
                f"effect terminal {tool!r}: unknown key(s) {sorted(unknown)} "
                f"(allowed: {sorted(_ALLOWED_KEYS)})"
            )
        if tool in terminals:
            raise EffectConfigError(
                f"effect terminal {tool!r} is declared twice — one declaration "
                f"per tool (merge the field lists)"
            )
        raw_data = entry.get("data_fields")
        if raw_data is not None and not isinstance(raw_data, list):
            raise EffectConfigError(
                f"effect terminal {tool!r}: data_fields must be null (= every "
                f"argument is data) or a list of paths"
            )
        if isinstance(raw_data, list) and not raw_data:
            raise EffectConfigError(
                f"effect terminal {tool!r}: data_fields=[] would judge nothing — "
                f"omit the tool, or use null to judge every argument"
            )
        data_fields = (
            None if raw_data is None
            else tuple(_validate_path(p, tool=tool, key="data_fields") for p in raw_data)
        )
        raw_param = entry.get("param_fields", [])
        if not isinstance(raw_param, list):
            raise EffectConfigError(
                f"effect terminal {tool!r}: param_fields must be a list of paths"
            )
        param_fields = tuple(
            _validate_path(p, tool=tool, key="param_fields") for p in raw_param
        )
        for p in param_fields:
            for d in data_fields or ():
                if _under(p, d) or _under(d, p):
                    raise EffectConfigError(
                        f"effect terminal {tool!r}: data field {d!r} and param "
                        f"field {p!r} overlap — the data/param boundary must be "
                        f"unambiguous (declare disjoint subtrees)"
                    )
        kind = entry.get("kind", KIND_RECORD)
        if kind not in (KIND_RECORD, KIND_DOCUMENT):
            raise EffectConfigError(
                f"effect terminal {tool!r}: kind must be {KIND_RECORD!r} or "
                f"{KIND_DOCUMENT!r}, got {kind!r}"
            )
        mode = entry.get("mode")
        if mode is not None and mode not in _MODES:
            raise EffectConfigError(
                f"effect terminal {tool!r}: mode must be null (follow the "
                f"deployment's global mode) or one of {list(_MODES)}, got {mode!r}"
            )
        degrade = entry.get("degrade", "block")
        if degrade not in _DEGRADES:
            raise EffectConfigError(
                f"effect terminal {tool!r}: degrade must be one of "
                f"{list(_DEGRADES)} (default 'block' — D1), got {degrade!r}"
            )
        raw_receipt = entry.get("receipt_fields", [])
        if not isinstance(raw_receipt, list):
            raise EffectConfigError(
                f"effect terminal {tool!r}: receipt_fields must be a list of paths"
            )
        receipt_fields = tuple(
            _validate_path(p, tool=tool, key="receipt_fields") for p in raw_receipt
        )
        terminals[tool] = EffectTerminal(
            tool=tool, data_fields=data_fields, param_fields=param_fields,
            kind=kind, mode=mode, degrade=degrade, receipt_fields=receipt_fields,
        )
    return terminals


def _collect_leaves(value: Any, path: str, out: list[tuple[str, str]]) -> None:
    """Flatten the arguments object to (path, text) leaves. List elements share
    their container's path (indices are not addressable in the declaration —
    a declared field governs the whole subtree). None carries no text."""
    if isinstance(value, dict):
        for k, v in value.items():
            _collect_leaves(v, f"{path}.{k}", out)
    elif isinstance(value, list):
        for item in value:
            _collect_leaves(item, path, out)
    elif value is None:
        return
    elif isinstance(value, str):
        out.append((path, value))
    else:  # numbers / booleans — JSON spelling, the form the model wrote
        out.append((path, json.dumps(value)))


@dataclass
class EffectJudgment:
    """One designated call's observation: the deterministic existence verdict
    over its declared data values, plus the recorded derivation parameters."""

    tool: str
    call_id: str
    kind: str
    args_sha256: str
    verdict: str = "grounded"
    data_tokens: tuple[str, ...] = ()
    param_tokens: tuple[str, ...] = ()  # recorded, never enforced on, never a lane
    missing: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    candidates: dict[str, list[str]] = field(default_factory=dict)
    fields_absent: list[str] = field(default_factory=list)
    unjudged_paths: list[str] = field(default_factory=list)
    error: str = ""

    def to_event(self) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event": "effect_verdict",
            "stage": EFFECT_STAGE,
            "tool": self.tool,
            "call_id": self.call_id,
            "kind": self.kind,
            "verdict": self.verdict,
            "missing": self.missing,
            "reasons": self.reasons,
            "degraded": self.degraded,
            "candidates": self.candidates,
            "data_tokens": list(self.data_tokens),
            "param_tokens": list(self.param_tokens),
            "fields_absent": self.fields_absent,
            "unjudged_paths": self.unjudged_paths,
            "args_sha256": self.args_sha256,
        }
        if self.error:
            event["error"] = self.error
        return event


def judge_effect_call(
    terminal: EffectTerminal, *, call_id: str, arguments_raw: str, ledger: Ledger
) -> EffectJudgment:
    """The deterministic existence verdict over one designated call's declared
    data values, against the ledger of results observed BEFORE the call (the
    request's messages — temporal semantics are correct by construction). Pure;
    no hidden call (attribution is trail enrichment, not part of the verdict —
    E0 spends zero upstream calls)."""
    judgment = EffectJudgment(
        tool=terminal.tool,
        call_id=call_id,
        kind=terminal.kind,
        args_sha256=hashlib.sha256(arguments_raw.encode("utf-8")).hexdigest(),
    )
    try:
        args = json.loads(arguments_raw) if arguments_raw.strip() else {}
    except json.JSONDecodeError as e:
        # No verdict is possible — a visible absence (E0 has no enforcement to
        # withhold; E1 makes this a fail-posture site).
        judgment.verdict = VERDICT_UNPARSEABLE
        judgment.error = f"arguments are not JSON: {e}"
        return judgment

    leaves: list[tuple[str, str]] = []
    _collect_leaves(args, "$", leaves)

    def lane_of(path: str) -> str | None:
        if any(_under(path, p) for p in terminal.param_fields):
            return "param"
        if terminal.data_fields is None or any(
            _under(path, d) for d in terminal.data_fields
        ):
            return "data"
        return None

    data_texts = [text for path, text in leaves if lane_of(path) == "data"]
    param_texts = [text for path, text in leaves if lane_of(path) == "param"]
    judgment.unjudged_paths = sorted(
        {path for path, _ in leaves if lane_of(path) is None}
    )
    declared = [*(terminal.data_fields or ()), *terminal.param_fields]
    judgment.fields_absent = [
        f for f in declared if not any(_under(path, f) for path, _ in leaves)
    ]

    judgment.data_tokens = value_tokens("\n".join(data_texts))
    judgment.param_tokens = value_tokens("\n".join(param_texts))
    if not judgment.data_tokens:
        judgment.verdict = VERDICT_SKIPPED
        return judgment

    candidates = mint_candidates(judgment.data_tokens, ledger)
    if terminal.kind == KIND_DOCUMENT:
        schemes = numbering_schemes(judgment.data_tokens, ledger)
    else:
        # D3': record-kind values are data by declaration — the structure
        # license the mint grants for answer-shaped documents does not apply.
        # Stripping the lane can only move a token toward refusal, never
        # toward acceptance.
        schemes = None
        candidates = {
            tok: tuple(l for l in lanes if l.source != "contract-numbering")
            for tok, lanes in candidates.items()
        }
    vm = evaluate_value_map(judgment.data_tokens, candidates, [], schemes)
    judgment.verdict = vm.verdict
    judgment.missing = vm.missing
    judgment.reasons = vm.reasons
    judgment.degraded = vm.degraded
    judgment.candidates = vm.candidates
    return judgment


def iter_designated_calls(
    response_body: dict[str, Any], terminals: dict[str, EffectTerminal]
) -> Iterator[tuple[str, str, str]]:
    """(call_id, tool name, raw arguments) for every designated tool_call in the
    response — every choice, wire order. Non-designated calls are not touched."""
    for choice in response_body.get("choices") or []:
        msg = choice.get("message") or {}
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            if name in terminals:
                yield (call.get("id") or "", name, fn.get("arguments") or "")


@dataclass
class PendingEffects:
    """Judged designated calls awaiting their observed result. tool_call_id is
    the join key (wire-native, already observed by the ledger). Bounded: beyond
    `cap` the oldest entry is evicted and counted — visible in /metrics, never
    silent."""

    cap: int = PENDING_MAX
    evicted: int = 0
    _by_call: dict[str, tuple[str, str, tuple[str, ...]]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._by_call)

    def register(self, call_id: str, tool: str, verdict: str,
                 receipt_fields: tuple[str, ...] = ()) -> None:
        if not call_id:  # no join key on the wire — nothing to correlate (the
            return  # effect_verdict event still carries the empty id, visibly)
        self._by_call[call_id] = (tool, verdict, receipt_fields)
        while len(self._by_call) > self.cap:
            oldest = next(iter(self._by_call))
            del self._by_call[oldest]
            self.evicted += 1

    def match(self, ledger: Ledger) -> list[dict[str, Any]]:
        """Emit-ready effect_receipt events for every observed result of a
        pending judged call. Each receipt carries verdict_at_call — the
        write-time stain (§4.3) — and the result's content hashes (the
        evidence-layer join keys). Matched entries are consumed: one receipt
        per call, ever."""
        events: list[dict[str, Any]] = []
        for record in ledger.records:
            entry = self._by_call.pop(record.call_id, None)
            if entry is None:
                continue
            tool, verdict, receipt_fields = entry
            hashes = answer_hashes(record.output)
            event = {
                "event": "effect_receipt",
                "stage": EFFECT_STAGE,
                "tool": tool,
                "call_id": record.call_id,
                "verdict_at_call": verdict,
                "receipt_sha_raw": hashes["sha_raw"],
                "receipt_sha_canon": hashes["sha_canon"],
            }
            if receipt_fields:  # domain crosswalk data (§3.4) — declared only
                found, absent = extract_receipt_fields(record.output, receipt_fields)
                event["receipt_data"] = found
                event["receipt_fields_absent"] = absent
            events.append(event)
        return events


def extract_receipt_fields(
    output: str, paths: tuple[str, ...]
) -> tuple[dict[str, str], list[str]]:
    """Extract declared receipt fields (domain crosswalk data — e.g. the
    journal id freee returns) from an observed tool result. Returns
    (found {path: value-as-written}, absent [paths]). A non-JSON result, or a
    declared path the result does not carry, lands in `absent` — a visible
    absence, never an error and never a guess (many receipt declarations span
    several endpoint shapes; only the matching ones are expected to appear)."""
    try:
        obj = json.loads(output)
    except json.JSONDecodeError:
        return {}, list(paths)
    found: dict[str, str] = {}
    absent: list[str] = []
    for path in paths:
        node: Any = obj
        for segment in path[2:].split("."):
            if isinstance(node, dict) and segment in node:
                node = node[segment]
            else:
                node = _ABSENT
                break
        if node is _ABSENT or isinstance(node, (dict, list)):
            absent.append(path)
        else:
            found[path] = node if isinstance(node, str) else json.dumps(node)
    return found, absent


_ABSENT = object()  # sentinel: a path segment the result does not carry


# ---- stage E1: enforcement ---------------------------------------------------
#
# One response, one decision (§3.2: no partial shipping — a response whose
# designated calls are only partly groundable is gated WHOLE; the feedback
# names each call and value). Per-call effective modes can differ (D2), so the
# decision takes the strictest applicable action:
#   block  > posture > retry > ship
# block dominates because the operator explicitly demanded fail-closed for that
# tool — a retry on a sibling call could never ship this response anyway.
# An unparseable-arguments call under enforcement has NO verdict, so the A2
# fail posture decides (§3.2) — retry is not attempted on it (a push-back needs
# named values to refuse; "not JSON" has none).

ACTION_SHIP = "ship"
ACTION_BLOCK = "block"
ACTION_RETRY = "retry"
ACTION_POSTURE = "posture"


@dataclass
class EffectDecision:
    """The whole-response gate decision over one response's designated calls."""

    action: str
    blocking: list[EffectJudgment] = field(default_factory=list)
    retrying: list[EffectJudgment] = field(default_factory=list)
    unparseable: list[EffectJudgment] = field(default_factory=list)  # enforced, verdict-less


def decide_effect_action(
    judgments: list[tuple[EffectJudgment, EffectTerminal]], global_mode: str
) -> EffectDecision:
    decision = EffectDecision(action=ACTION_SHIP)
    for judgment, terminal in judgments:
        mode = effective_mode(terminal, global_mode)
        if mode == "flag":  # observation only — E0 semantics, recorded, ships
            continue
        if judgment.verdict == "ungrounded":
            (decision.blocking if mode == "block" else decision.retrying).append(judgment)
        elif judgment.verdict == VERDICT_UNPARSEABLE:
            decision.unparseable.append(judgment)
    if decision.blocking:
        decision.action = ACTION_BLOCK
    elif decision.unparseable:
        decision.action = ACTION_POSTURE
    elif decision.retrying:
        decision.action = ACTION_RETRY
    return decision


def union_missing(judgments: list[EffectJudgment]) -> tuple[str, ...]:
    """The deterministic convergence key for effect retry: the union of missing
    tokens across the gated calls (call ids change round to round; the value
    set is what must strictly shrink)."""
    return tuple(sorted({tok for j in judgments for tok in j.missing}))


def degrade_target(
    judgments: list[EffectJudgment], terminals: dict[str, EffectTerminal]
) -> str:
    """D1: retry non-convergence degrades to block unless EVERY gated call's
    tool carries the explicit per-tool "flag" override (D2) — one block-target
    tool gates the whole response (no partial shipping)."""
    return (
        "flag"
        if all(terminals[j.tool].degrade == "flag" for j in judgments)
        else "block"
    )


def _call_lines(judgments: list[EffectJudgment]) -> list[str]:
    lines = []
    for j in judgments:
        if j.verdict == VERDICT_UNPARSEABLE:
            lines.append(f"- {j.tool}: arguments could not be parsed ({j.error})")
            continue
        for tok in j.missing:
            lines.append(f"- {j.tool}: {tok} ({j.reasons.get(tok, 'no verified origin')})")
    return lines


def effect_refusal_text(judgments: list[EffectJudgment]) -> str:
    """The honest client-facing refusal shipped in place of a gated response
    (block / D1 degrade): states plainly that the call was NOT executed and
    which values lacked a verified origin — never a fake success, never a
    silent drop (run-or-help)."""
    tools = ", ".join(sorted({j.tool for j in judgments}))
    return "\n".join([
        f"I can't perform this action: the {tools} call was stopped by the "
        "grounding ledger voucher before execution. The following argument value(s) "
        "could not be verified against any tool result I actually retrieved "
        "or information you provided:",
        *_call_lines(judgments),
        "Rather than write unverified values, I'm stopping here. I can re-check "
        "the data with the available tools, or you can provide the missing "
        "information.",
    ])


def close_gate_feedback_effect(judgment: EffectJudgment) -> str:
    """The push-back content of ONE synthetic tool result (D5): the protocol
    demands a tool-role reply to the rejected assistant tool_calls turn, and
    the gate's refusal IS what actually happened — the wording claims only
    that (the call was stopped BEFORE execution; the tool never ran). The
    close-gate elements mirror the text terminal's measured wording
    (enforce.py close_gate_feedback): name the values, name the fix, forbid
    re-asserting rejected values, and give the honest way out — for a write,
    that is NOT writing (there is no hypothesis lane inside an executed
    side effect)."""
    what = "; ".join(
        f"{tok} ({judgment.reasons.get(tok, 'no verified origin')})"
        for tok in judgment.missing
    )
    return (
        f"call refused (grounding ledger voucher): this {judgment.tool} call was NOT "
        f"executed — the gate stopped it before execution. These argument values "
        f"do not trace to any observed tool output or user-provided text: {what}. "
        f"Ground each value before writing: obtain or compute it with an "
        f"available tool so it appears in an observed result, then issue the "
        f"call again. Do not use any rejected value above again unless a tool "
        f"result you actually obtained shows it. If a value cannot be obtained "
        f"with the available tools, do not perform this action — answer in text "
        f"instead, saying honestly what you could not verify. Never write a "
        f"value you did not observe."
    )


def apply_effect_block(
    response_body: dict[str, Any], judgments: list[EffectJudgment]
) -> dict[str, Any]:
    """block — replace the gated tool_calls response with the honest refusal
    TEXT response. The whole choices list is replaced (any surviving tool_call
    would be executed by the harness — partial shipping is exactly what §3.2
    rules out); the envelope is kept."""
    blocked = {**response_body}
    blocked["choices"] = [{
        "index": 0,
        "message": {"role": "assistant", "content": effect_refusal_text(judgments)},
        "finish_reason": "stop",
    }]
    return blocked


def apply_effect_posture_block(
    reason: str, response_body: dict[str, Any]
) -> dict[str, Any]:
    """fail-closed replacement for a gated response the ledger voucher could not
    verify (unparseable arguments, a failed retry forward). Same shape as
    apply_effect_block — the WHOLE choices list is replaced, because a
    surviving tool_call would be executed by the harness — but the refusal
    names the verification failure, not values (there is no verdict to cite)."""
    blocked = {**response_body}
    blocked["choices"] = [{
        "index": 0,
        "message": {"role": "assistant", "content": posture_refusal_text(reason)},
        "finish_reason": "stop",
    }]
    return blocked


def rejected_call_objects(
    response_body: dict[str, Any], judgments: list[EffectJudgment]
) -> list[dict[str, Any]]:
    """The gated calls' raw tool_call objects, verbatim from the response (D5:
    the model's own utterance, unmodified — the ledger voucher never rewrites
    arguments, not even inside its own push-back)."""
    wanted = {j.call_id for j in judgments}
    calls = []
    for choice in response_body.get("choices") or []:
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            if call.get("id") in wanted:
                calls.append(call)
    return calls


def build_effect_retry_turn(
    response_body: dict[str, Any], judgments: list[EffectJudgment]
) -> list[dict[str, Any]]:
    """The ledger voucher-internal push-back exchange (D5 synthetic tool result):
    the rejected calls as the model's own assistant turn, each answered by one
    tool-role message carrying the gate's refusal. VOUCHER-INTERNAL ONLY —
    these messages never enter the client's conversation, and the feedback
    text never enters the ledger as evidence (§3.2's nail: a refusal that
    echoes the rejected value must not become a candidate lane next round —
    judging always runs against the ORIGINAL request's ledger)."""
    by_id = {c.get("id"): c for c in rejected_call_objects(response_body, judgments)}
    turn: list[dict[str, Any]] = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [by_id[j.call_id] for j in judgments if j.call_id in by_id],
    }]
    for j in judgments:
        if j.call_id in by_id:
            turn.append({
                "role": "tool",
                "tool_call_id": j.call_id,
                "content": close_gate_feedback_effect(j),
            })
    return turn


def enforcement_event(
    *,
    action: str,
    global_mode: str,
    judgments: list[tuple[EffectJudgment, EffectTerminal]],
    degrade_reason: str | None = None,
    posture: str | None = None,
    posture_trigger: str | None = None,
) -> dict[str, Any]:
    """The effect_enforcement audit event (§4.4) — emitted whenever enforcement
    was ACTIVE for a response's designated calls (pure-flag responses keep the
    E0 audit volume: verdict + receipt only)."""
    event: dict[str, Any] = {
        "event": "effect_enforcement",
        "stage": EFFECT_STAGE,
        "mode": global_mode,
        "action": action,
        "calls": [
            {
                "tool": j.tool,
                "call_id": j.call_id,
                "mode": effective_mode(t, global_mode),
                "verdict": j.verdict,
                "missing": j.missing,
            }
            for j, t in judgments
        ],
    }
    if degrade_reason is not None:
        event["degrade_reason"] = degrade_reason
    if posture is not None:
        event["posture"] = posture
        event["posture_trigger"] = posture_trigger
    return event


def enforced_tools_in_request(
    request_tools: list[dict[str, Any]] | None,
    terminals: dict[str, EffectTerminal],
    global_mode: str,
) -> set[str]:
    """Designated tools offered in this request's tools[] whose effective mode
    needs a verdict BEFORE bytes ship (block/retry). Drives the A1 contract
    revision (§4.1): only conversations that can actually fire effect
    enforcement lose the tool_calls chunk-identity passthrough (they buffer to
    payload-concat identity); flag-only and undesignated conversations keep
    the standing contract. The membership test is the request's own tools[] —
    no mid-stream name guessing (§4.1: the first tool_call delta cannot rule
    out a later parallel designated call)."""
    offered = set()
    for tool in request_tools or []:
        name = ((tool.get("function") or {}).get("name")) if isinstance(tool, dict) else None
        if name:
            offered.add(name)
    return {
        name
        for name, terminal in terminals.items()
        if name in offered and effective_mode(terminal, global_mode) in ("block", "retry")
    }
