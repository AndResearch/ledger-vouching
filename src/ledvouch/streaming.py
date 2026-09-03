"""SSE streaming primitives — scanner, aggregator, synthesizer (A1).

# Design selection (2026-07-20 — spectate-passthrough + terminal-only buffering,
# selected over full buffering at kickoff):
#   Full buffering (buffer every turn) degrades TTFT on EVERY turn while enforcement can
#   only ever fire on the terminal turn, and it demotes the spectator's PROVEN
#   chunk-level byte transparency (tau2-bench Δ=0.000) to mere payload-concat equality
#   in production's dominant mode. The selected design keeps spectator (tool_calls) turns
#   chunk-byte-identical under stream and confines the deep-enforcement TTFT
#   cost — unavoidable in principle (the verdict must precede the first shipped byte) —
#   to the terminal turn, matching the light/deep structure.
#   Forcing stream=false stays rejected: it rewrites the request body.
#
# Transparency definitions (fixed BEFORE the tests were written):
#   - passthrough path (tool_calls turn): CHUNK-UNIT BYTE IDENTITY — the client
#     receives the exact byte chunks the upstream sent, in order (a bounded
#     classification prefix is held back briefly, then flushed verbatim).
#   - buffered path (content turn, unaltered ship): SSE data-payload
#     CONCATENATION identity. Chunk-boundary reproduction is explicitly NOT
#     guaranteed (the implementation replays the buffered chunks verbatim,
#     which is stronger, but only the concatenation is the contract).
#   - altered ship (block / retry repair / keep-alive): a synthesized SSE
#     stream carrying the enforced body — transparency deliberately absent.
#
# Classification (first-delta discrimination): OpenAI SSE reveals in the first
#   non-empty delta whether a turn is tool_calls or content. Role-only / empty
#   deltas are undecided and stay held. A content-classified turn that later
#   grows tool_calls (mixed turn) is detected after aggregation and shipped
#   verbatim — misclassification costs latency, never correctness.
#
# fail-fast / degrade ("OUR failure never punishes the client"): an
#   unparseable stream is shipped verbatim with an error observation recorded —
#   the ledger voucher degrades to pure spectator; it never repairs, never blocks on
#   its own parse failure, and never coerces a malformed event.
"""

from __future__ import annotations

import json
from typing import Any

DONE = b"[DONE]"

TOOL_CALLS = "tool_calls"
CONTENT = "content"


class SSEParseError(Exception):
    """A stream that cannot be understood as SSE chat-completion chunks. The
    caller degrades to verbatim spectator (never repair, never block)."""


class SSEScanner:
    """Incremental SSE event splitter. feed() returns the data payloads of every
    event completed by the given chunk; raw bytes are NOT retained here (the
    caller keeps its own verbatim chunk list for transparent replay)."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        payloads: list[bytes] = []
        while True:
            cut, sep_len = _find_event_end(self._buf)
            if cut is None:
                return payloads
            block, self._buf = self._buf[:cut], self._buf[cut + sep_len:]
            data_lines = [
                line[5:].lstrip(b" ")
                for line in block.replace(b"\r\n", b"\n").split(b"\n")
                if line.startswith(b"data:")
            ]
            if data_lines:
                payloads.append(b"\n".join(data_lines))

    def residual(self) -> bytes:
        """Bytes after the last complete event — non-blank residual at stream
        end means a truncated/foreign stream (degrade, don't guess)."""
        return self._buf


def _find_event_end(buf: bytes) -> tuple[int | None, int]:
    """Earliest event terminator (blank line), tolerant of CRLF."""
    best: tuple[int | None, int] = (None, 0)
    for sep in (b"\n\n", b"\r\n\r\n"):
        i = buf.find(sep)
        if i != -1 and (best[0] is None or i < best[0]):
            best = (i, len(sep))
    return best


def classify_payload(payload: bytes) -> str | None:
    """First-delta discrimination: TOOL_CALLS / CONTENT / None (undecided).
    [DONE] and empty/role-only deltas are undecided. Raises SSEParseError on a
    payload that is not a chat-completion chunk object."""
    if payload.strip() == DONE:
        return None
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as e:
        raise SSEParseError(f"SSE data payload is not JSON: {e}")
    if not isinstance(obj, dict):
        raise SSEParseError(f"SSE data payload is not an object: {str(obj)[:200]}")
    for choice in obj.get("choices") or []:
        delta = (choice.get("delta") or {}) if isinstance(choice, dict) else {}
        if delta.get("tool_calls"):
            return TOOL_CALLS
        content = delta.get("content")
        if isinstance(content, str) and content != "":
            return CONTENT
        if isinstance(content, list) and content:
            return CONTENT
    return None


def parse_chunk_payloads(payloads: list[bytes]) -> list[dict[str, Any]]:
    """All non-[DONE] payloads as chunk objects. Raises SSEParseError."""
    chunks: list[dict[str, Any]] = []
    for p in payloads:
        if p.strip() == DONE:
            continue
        try:
            obj = json.loads(p)
        except (json.JSONDecodeError, ValueError) as e:
            raise SSEParseError(f"SSE data payload is not JSON: {e}")
        if not isinstance(obj, dict):
            raise SSEParseError(f"SSE data payload is not an object: {str(obj)[:200]}")
        chunks.append(obj)
    return chunks


def aggregate_stream(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Reassemble chat.completion.chunk deltas into a non-stream response body.
    Used for GOVERNANCE ONLY (terminal detection + answer text) — an unaltered
    ship always replays the buffered upstream bytes, never this object."""
    body: dict[str, Any] = {"object": "chat.completion"}
    acc: dict[int, dict[str, Any]] = {}
    tools: dict[int, dict[int, dict[str, Any]]] = {}
    for ch in chunks:
        for key in ("id", "created", "model", "system_fingerprint"):
            if ch.get(key) is not None and key not in body:
                body[key] = ch[key]
        if isinstance(ch.get("usage"), dict):
            body["usage"] = ch["usage"]
        for choice in ch.get("choices") or []:
            idx = choice.get("index", 0)
            slot = acc.setdefault(
                idx,
                {"index": idx,
                 "message": {"role": "assistant", "content": None},
                 "finish_reason": None},
            )
            delta = choice.get("delta") or {}
            if delta.get("role"):
                slot["message"]["role"] = delta["role"]
            content = delta.get("content")
            if isinstance(content, str):
                slot["message"]["content"] = (slot["message"]["content"] or "") + content
            elif isinstance(content, list):  # content-parts deltas
                joined = "".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
                slot["message"]["content"] = (slot["message"]["content"] or "") + joined
            for tc in delta.get("tool_calls") or []:
                ti = tc.get("index", 0)
                t = tools.setdefault(idx, {}).setdefault(
                    ti, {"id": None, "type": "function",
                         "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    t["id"] = tc["id"]
                if tc.get("type"):
                    t["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    t["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    t["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                slot["finish_reason"] = choice["finish_reason"]
    choices = []
    for idx in sorted(acc):
        slot = acc[idx]
        per_choice = tools.get(idx)
        if per_choice:
            slot["message"]["tool_calls"] = [per_choice[i] for i in sorted(per_choice)]
        choices.append(slot)
    body["choices"] = choices
    return body


def synthesize_sse(body: dict[str, Any], *, include_usage: bool = False) -> list[bytes]:
    """Render a non-stream response body (a block refusal, a repaired retry
    answer, or a keep-alive tool_calls response) as SSE chunk bytes. Chunk
    boundaries are ours — the altered-ship path makes no transparency claim.
    `include_usage` mirrors the client's stream_options.include_usage opt-in."""
    envelope = {
        "id": body.get("id", "ledvouch"),
        "object": "chat.completion.chunk",
        "created": body.get("created", 0),
        "model": body.get("model", ""),
    }
    events: list[dict[str, Any]] = []

    def chunk(index: int, delta: dict[str, Any], finish: str | None = None) -> None:
        events.append({**envelope, "choices": [
            {"index": index, "delta": delta, "finish_reason": finish}]})

    for choice in body.get("choices") or []:
        idx = choice.get("index", 0)
        msg = choice.get("message") or {}
        chunk(idx, {"role": msg.get("role", "assistant")})
        content = msg.get("content")
        if isinstance(content, list):  # content-parts form
            content = "\n".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            )
        if content:
            chunk(idx, {"content": content})
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            chunk(idx, {"tool_calls": [
                {"index": i, **tc} for i, tc in enumerate(tool_calls)]})
        finish = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        chunk(idx, {}, finish)
    if include_usage and isinstance(body.get("usage"), dict):
        events.append({**envelope, "choices": [], "usage": body["usage"]})
    out = [b"data: " + json.dumps(e).encode() + b"\n\n" for e in events]
    out.append(b"data: " + DONE + b"\n\n")
    return out
