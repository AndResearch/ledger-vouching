"""Ledger — read-only bookkeeping of a wire-visible OpenAI conversation.

# Design rationale (spectator bookkeeping; provenance-tree raw material):
#   The ledger voucher is a SPECTATOR. It never mutates messages. On every turn it reads
#   the request `messages[]` and copies each `role:"tool"` result into a ledger,
#   keyed by the tool_call_id that produced it. This is possible for free because
#   the OpenAI function-calling protocol is stateless: the client re-sends the whole
#   conversation each turn, so every past tool output flows past the proxy.
#
#   For stage A the ledger only needs the tool OUTPUTS (the evidence corpus for the
#   terminal floor). But we also record, for each tool output, the ARGUMENTS of the
#   tool_call that produced it (available on the assistant message's
#   `tool_calls[].function.arguments`). That is the raw material for stage B's
#   provenance tree and laundering detection — recorded now, walked later.
#
# fail-fast / no implicit repair: malformed tool_call arguments are stored
#   verbatim as the raw string (never coerced/defaulted); the tree-walk decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolRecord:
    """One observed tool result, tied to the call that produced it."""

    call_id: str
    name: str
    arguments_raw: str  # the tool_call's function.arguments, verbatim (may be non-JSON)
    output: str  # the role:"tool" message content, verbatim


@dataclass
class Ledger:
    """The wire-derived record of a conversation up to now."""

    goal: str = ""  # the first user message — the Env-given task text
    records: list[ToolRecord] = field(default_factory=list)
    # EVERY user message text (goal included). In a multi-turn dialogue the user keeps
    # supplying values (order ids, zip codes) after the first message; those are
    # Env-given input, a legitimate origin for the stage B provenance walk — only the
    # walk uses this. The stage A floor keeps goal-only semantics (unchanged).
    user_texts: list[str] = field(default_factory=list)
    # System-message texts: operator-provided policy/instructions. Values quoted
    # from them ("5-7 business days") have a legitimate wire-visible origin —
    # observed live as a false-ungrounded family ("policy" claims, tau2-bench stage C round 1).
    system_texts: list[str] = field(default_factory=list)

    def evidence(self) -> list[str]:
        """The corpus the grounding floor checks against: every observed tool output."""
        return [r.output for r in self.records]

    def by_call_id(self, call_id: str) -> ToolRecord | None:
        for r in self.records:
            if r.call_id == call_id:
                return r
        return None


def _content_to_text(content: Any) -> str:
    """OpenAI content is a string OR a list of parts ({type:text,text:...}). Flatten
    to text; anything else is stringified (never dropped — a spectator loses nothing)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", part.get("content", part))))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def build_ledger(messages: list[dict[str, Any]]) -> Ledger:
    """Walk the OpenAI messages[] and record goal + every observed tool result with
    the arguments of its producing tool_call. Pure; never mutates `messages`."""
    ledger = Ledger()
    # Map tool_call_id -> (tool name, raw arguments) from assistant tool_calls, so a
    # later role:"tool" message can be tied back to the call that produced it.
    call_meta: dict[str, tuple[str, str]] = {}
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            ledger.system_texts.append(_content_to_text(msg.get("content")))
        elif role == "user":
            text = _content_to_text(msg.get("content"))
            ledger.user_texts.append(text)
            if not ledger.goal:
                ledger.goal = text
        elif role == "assistant":
            for call in msg.get("tool_calls") or []:
                cid = call.get("id") or ""
                fn = call.get("function") or {}
                call_meta[cid] = (fn.get("name") or "", fn.get("arguments") or "")
        elif role == "tool":
            cid = msg.get("tool_call_id") or ""
            name, args_raw = call_meta.get(cid, ("", ""))
            ledger.records.append(
                ToolRecord(
                    call_id=cid,
                    name=name,
                    arguments_raw=args_raw,
                    output=_content_to_text(msg.get("content")),
                )
            )
    return ledger
