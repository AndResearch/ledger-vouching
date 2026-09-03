"""Conformance suite v0 — B1 (deterministic deployment checks).

# Machine-verifiability nail: every check is
# a deterministic pass / fail / skip with NO human judgment anywhere — whoever
# runs the suite against the same deployment gets the same verdict. That is the
# property that lets certification be independent of the sales channel. The
# report deliberately carries no timestamps or environment-dependent noise:
# two runs against the same deployment produce byte-identical reports.
#
# Three check groups:
#   shape.*  — deployment-shape inspection (env contract: posture, mode/stage,
#              audit destination validity/writability, upstream base syntax).
#              Reads env only; never opens network connections.
#   mech.*   — hermetic mechanism probes: an in-process ledger voucher app with a
#              CANNED fake upstream re-proves the standing guarantees on this
#              installed build — spectator transparency (non-stream byte
#              identity, stream chunk identity, buffered concat identity),
#              enforcement behavior (flag / block / retry keep-alive / retry
#              repair), fail-posture behavior (open / closed) and audit
#              emission. No env, no network, no tokens.
#   live.*   — OPTIONAL (--live): the same canned exchange against the REAL
#              configured upstream. Determinism is measured first (two direct
#              calls); only a deterministic upstream can carry a live Δ=0
#              claim — a nondeterministic one yields an honest skip, never a
#              guess (the hermetic transparency proof stands regardless).
#
# fail-fast: a crashed probe is a FAIL with the exception surfaced — the suite
# never converts its own breakage into a pass.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse

from .content_hash import answer_hashes
from .effect_gate import EffectConfigError, parse_effect_terminals
from .enforce import BLOCK, FAIL_CLOSED, FAIL_OPEN, FLAG, RETRY, ObservationSink
from .proxy import _default_forward, make_app
from .streaming import SSEScanner

SUITE = "ledger-vouching.conformance.v0"

_VALID_MODES = (FLAG, BLOCK, RETRY)
_VALID_STAGES = ("A", "B", "C")
_VALID_AUDIT = ("stdout", "file", "webhook")


@dataclass
class Check:
    id: str
    title: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


# ---- shape.* — deployment-shape inspection ---------------------------------


def shape_checks(env: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []

    posture = env.get("LEDVOUCH_FAIL_POSTURE")
    if posture in (FAIL_OPEN, FAIL_CLOSED):
        checks.append(Check("shape.fail_posture", "fail posture is set", "pass",
                            f"LEDVOUCH_FAIL_POSTURE={posture}"))
    else:
        checks.append(Check(
            "shape.fail_posture", "fail posture is set", "fail",
            f"LEDVOUCH_FAIL_POSTURE must be 'open' or 'closed' (got {posture!r}) — "
            "the ledger voucher refuses to start without it (A2, no silent default)",
        ))

    mode = env.get("LEDVOUCH_MODE", FLAG)
    stage = env.get("LEDVOUCH_STAGE", "C")
    if mode not in _VALID_MODES:
        checks.append(Check("shape.mode_stage", "mode/stage are valid", "fail",
                            f"LEDVOUCH_MODE={mode!r} (expected flag | block | retry)"))
    elif stage not in _VALID_STAGES:
        checks.append(Check("shape.mode_stage", "mode/stage are valid", "fail",
                            f"LEDVOUCH_STAGE={stage!r} (expected A | B | C)"))
    elif mode in (BLOCK, RETRY) and stage != "C":
        checks.append(Check("shape.mode_stage", "mode/stage are valid", "fail",
                            f"mode {mode!r} requires stage C (enforcement keys on the "
                            "value-map verdict)"))
    else:
        checks.append(Check("shape.mode_stage", "mode/stage are valid", "pass",
                            f"mode={mode} stage={stage}"))

    kind = env.get("LEDVOUCH_AUDIT_STREAM", "stdout")
    if kind not in _VALID_AUDIT:
        checks.append(Check("shape.audit_stream", "audit destination is valid", "fail",
                            f"LEDVOUCH_AUDIT_STREAM={kind!r} (expected stdout | file | webhook)"))
    elif kind == "file":
        path = env.get("LEDVOUCH_AUDIT_FILE")
        if not path:
            checks.append(Check("shape.audit_stream", "audit destination is valid", "fail",
                                "LEDVOUCH_AUDIT_STREAM=file requires LEDVOUCH_AUDIT_FILE"))
        else:
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
                checks.append(Check("shape.audit_stream", "audit destination is valid",
                                    "pass", f"file:{path} (append-writable)"))
            except OSError as e:
                checks.append(Check("shape.audit_stream", "audit destination is valid",
                                    "fail", f"audit file {path!r} is not writable: {e}"))
    elif kind == "webhook":
        url = env.get("LEDVOUCH_AUDIT_WEBHOOK_URL", "")
        if urlparse(url).scheme in ("http", "https") and urlparse(url).netloc:
            checks.append(Check("shape.audit_stream", "audit destination is valid",
                                "pass", f"webhook:{url}"))
        else:
            checks.append(Check("shape.audit_stream", "audit destination is valid", "fail",
                                f"LEDVOUCH_AUDIT_WEBHOOK_URL={url!r} is not an http(s) URL"))
    else:
        checks.append(Check("shape.audit_stream", "audit destination is valid",
                            "pass", "stdout"))

    tok = env.get("LEDVOUCH_TOKENIZER", "v1")
    if tok == "v1":
        checks.append(Check("shape.tokenizer", "value-tokenizer version is valid",
                            "pass", "LEDVOUCH_TOKENIZER=v1 (decimal literals audited "
                                    "as period-delimited fragments)"))
    elif tok == "v2":
        checks.append(Check("shape.tokenizer", "value-tokenizer version is valid",
                            "pass", "LEDVOUCH_TOKENIZER=v2 (decimal literals audited "
                                    "whole — digit.digit periods are not sentence breaks)"))
    else:
        checks.append(Check("shape.tokenizer", "value-tokenizer version is valid",
                            "fail", f"LEDVOUCH_TOKENIZER={tok!r} (expected v1 | v2 — "
                                    "startup refuses otherwise)"))

    hash_header = env.get("LEDVOUCH_ANSWER_HASH_HEADER", "off")
    if hash_header in ("on", "off"):
        checks.append(Check("shape.answer_hash_header", "answer-hash header option is valid",
                            "pass", f"LEDVOUCH_ANSWER_HASH_HEADER={hash_header}"))
    else:
        checks.append(Check("shape.answer_hash_header", "answer-hash header option is valid",
                            "fail", f"LEDVOUCH_ANSWER_HASH_HEADER={hash_header!r} "
                                    "(expected on | off — startup refuses otherwise)"))

    base = env.get("LEDVOUCH_UPSTREAM_BASE", "https://api.openai.com/v1")
    parsed = urlparse(base)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        checks.append(Check("shape.upstream_base", "upstream base URL is well-formed",
                            "pass", base))
    else:
        checks.append(Check("shape.upstream_base", "upstream base URL is well-formed",
                            "fail", f"LEDVOUCH_UPSTREAM_BASE={base!r} is not an http(s) URL"))

    raw_effects = env.get("LEDVOUCH_EFFECT_TERMINALS")
    if raw_effects is None or not raw_effects.strip():
        checks.append(Check("shape.effect_terminals", "effect-terminal declaration is valid",
                            "pass", "not configured (gate inert)"))
    else:
        try:
            terminals = parse_effect_terminals(raw_effects)
            checks.append(Check(
                "shape.effect_terminals", "effect-terminal declaration is valid",
                "pass", f"{len(terminals)} tool(s): {', '.join(sorted(terminals))}"))
        except EffectConfigError as e:
            checks.append(Check("shape.effect_terminals",
                                "effect-terminal declaration is valid", "fail", str(e)))
    return checks


# ---- ASGI drivers (no network, no test-framework dependency) ---------------


async def _asgi_request_full(
    app, body: dict[str, Any], path: str = "/v1/chat/completions",
    method: str = "POST",
) -> tuple[int, dict[str, str], list[bytes]]:
    """Drive the app as a plain ASGI callable; each http.response.body message is
    one received chunk (the stream checks assert on those boundaries). Response
    headers are returned lowercased (the header-only-delta probe reads them)."""
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"host", b"ledvouch-doctor")],
        "client": ("doctor", 0), "server": ("doctor", 80),
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start.get("headers") or []}
    chunks = [m["body"] for m in messages
              if m["type"] == "http.response.body" and m.get("body")]
    return start["status"], headers, chunks


async def _asgi_request(app, body: dict[str, Any], path: str = "/v1/chat/completions",
                        method: str = "POST") -> tuple[int, list[bytes]]:
    status, _headers, chunks = await _asgi_request_full(app, body, path, method)
    return status, chunks


def _json_of(chunks: list[bytes]) -> dict[str, Any]:
    return json.loads(b"".join(chunks))


def _data_concat(raw: bytes) -> bytes:
    scanner = SSEScanner()
    return b"".join(scanner.feed(raw))


# ---- canned fixtures (the conformance exchange) ----------------------------


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "what is Q3 revenue?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sql", "arguments": '{"q": "SELECT"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"revenue": 4500}'},
    ]


def _request_body(stream: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "conformance", "messages": _conversation(),
        "tools": [{"type": "function", "function": {"name": "sql"}}],
    }
    if stream:
        body["stream"] = True
    return body


GOOD_MAP = [{"value": "4500", "source": "$.s1.revenue"}]
BAD_MAP = [{"value": "3200", "source": "unknown"}]

_TOOL_CALL_RESPONSE = {"id": "t", "choices": [{"message": {
    "role": "assistant", "content": None,
    "tool_calls": [{"id": "c9", "type": "function",
                    "function": {"name": "sql", "arguments": "{}"}}]}}]}


def _fake_forward(terminal_content: str, mappings: list[list[dict[str, str]]],
                  retry_responses: list[dict[str, Any]] | None = None) -> Callable[..., Awaitable[Any]]:
    """The canned upstream: same request discrimination as the test rigs —
    response_format ⇒ value-map hidden call; close-gate feedback ⇒ retry;
    otherwise the agent's own turn."""
    mappings = list(mappings)
    retries = list(retry_responses or [])

    async def forward(body, headers):
        if body.get("response_format"):
            mapping = mappings.pop(0) if mappings else []
            content = json.dumps({"values": mapping})
            return 200, {"choices": [{"message": {"content": content}}]}
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            return 200, json.loads(json.dumps(retries.pop(0)))
        return 200, {"id": "t", "choices": [
            {"message": {"role": "assistant", "content": terminal_content}}]}

    return forward


def _sse(events: list[Any]) -> bytes:
    out = b""
    for e in events:
        payload = e if isinstance(e, str) else json.dumps(e)
        out += b"data: " + payload.encode() + b"\n\n"
    return out


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"id": "s", "object": "chat.completion.chunk", "created": 1,
            "model": "conformance",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _stream_forward_of(chunks: list[bytes]):
    async def stream_forward(body, headers):
        async def it():
            for c in chunks:
                yield c
        return 200, {"content-type": "text/event-stream"}, it()
    return stream_forward


# ---- mech.* — hermetic mechanism probes ------------------------------------


async def _probe_spectator_nonstream() -> None:
    upstream = json.loads(json.dumps(_TOOL_CALL_RESPONSE))
    sink = ObservationSink()

    async def forward(body, headers):
        return 200, json.loads(json.dumps(upstream))

    app = make_app(fail_posture=FAIL_OPEN, forward=forward, sink=sink, stage="C")
    status, chunks = await _asgi_request(app, _request_body())
    assert status == 200, f"expected 200, got {status}"
    assert _json_of(chunks) == upstream, "normal turn body was altered"
    assert sink.observations == [], "spectator recorded an observation on a normal turn"


async def _probe_terminal_flag_transparent() -> None:
    sink = ObservationSink()
    app = make_app(fail_posture=FAIL_OPEN, sink=sink, mode=FLAG, stage="C",
                   forward=_fake_forward("revenue is 3200", [BAD_MAP]))
    status, chunks = await _asgi_request(app, _request_body())
    body = _json_of(chunks)
    assert body["choices"][0]["message"]["content"] == "revenue is 3200", \
        "flag mode altered the terminal body"
    payload = sink.observations[0].stage_b
    assert payload["verdict"] == "ungrounded", \
        f"expected ungrounded verdict, got {payload.get('verdict')!r}"


async def _probe_stream_passthrough() -> None:
    raw = _sse([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"tool_calls": [{"index": 0, "id": "c9", "type": "function",
                                "function": {"name": "sql", "arguments": "{}"}}]}),
        _chunk({}, finish="tool_calls"),
        "[DONE]",
    ])
    sent = [raw[:17], raw[17:90], raw[90:]]  # events straddle chunk boundaries
    app = make_app(fail_posture=FAIL_OPEN, stream_forward=_stream_forward_of(sent))
    status, received = await _asgi_request(app, _request_body(stream=True))
    assert received == sent, "passthrough chunks are not byte-identical"


async def _probe_stream_buffered_concat() -> None:
    raw = _sse([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "revenue is "}),
        _chunk({"content": "3200"}),
        _chunk({}, finish="stop"),
        "[DONE]",
    ])
    sent = [raw[:31], raw[31:]]
    app = make_app(fail_posture=FAIL_OPEN, mode=FLAG, stage="C",
                   forward=_fake_forward("unused", [BAD_MAP]),
                   stream_forward=_stream_forward_of(sent))
    status, received = await _asgi_request(app, _request_body(stream=True))
    assert _data_concat(b"".join(received)) == _data_concat(raw), \
        "buffered-path data payloads are not concatenation-identical"


async def _probe_enforcement_block() -> None:
    sink = ObservationSink()
    app = make_app(fail_posture=FAIL_OPEN, sink=sink, mode=BLOCK, stage="C",
                   forward=_fake_forward("revenue is 3200", [BAD_MAP]))
    status, chunks = await _asgi_request(app, _request_body())
    content = _json_of(chunks)["choices"][0]["message"]["content"]
    assert "could not be verified" in content and "3200" in content, \
        f"block refusal missing or not actionable: {content[:120]!r}"
    assert sink.observations[0].shipped is False, "block still marked as shipped"


async def _probe_enforcement_retry_keepalive() -> None:
    app = make_app(fail_posture=FAIL_OPEN, mode=RETRY, stage="C",
                   forward=_fake_forward("revenue is 3200", [BAD_MAP],
                                         retry_responses=[_TOOL_CALL_RESPONSE]))
    status, chunks = await _asgi_request(app, _request_body())
    msg = _json_of(chunks)["choices"][0]["message"]
    assert (msg.get("tool_calls") or [{}])[0].get("id") == "c9", \
        "keep-alive did not hand the model's tool request back to the harness"


async def _probe_enforcement_retry_repair() -> None:
    repaired = {"id": "r", "choices": [
        {"message": {"role": "assistant", "content": "revenue is 4500"}}]}
    sink = ObservationSink()
    app = make_app(fail_posture=FAIL_OPEN, sink=sink, mode=RETRY, stage="C",
                   forward=_fake_forward("revenue is 3200", [BAD_MAP, GOOD_MAP],
                                         retry_responses=[repaired]))
    status, chunks = await _asgi_request(app, _request_body())
    content = _json_of(chunks)["choices"][0]["message"]["content"]
    assert content == "revenue is 4500", \
        f"repair did not ship the model's own corrected answer: {content[:120]!r}"
    assert sink.observations[0].stage_b["action"] == "repair"


def _retry_forward_dies(terminal_content: str) -> Callable[..., Awaitable[Any]]:
    """Canned upstream whose RETRY forward fails — the remaining non-stream A2
    posture site (since v3 the value-map verdict is deterministic at mint time,
    so a hidden-call failure can no longer lose the verdict)."""

    async def forward(body, headers):
        last = (body.get("messages") or [])[-1]
        if "grounding ledger voucher" in str(last.get("content", "")):
            raise ConnectionError("canned retry-forward failure")
        return 200, {"id": "t", "choices": [
            {"message": {"role": "assistant", "content": terminal_content}}]}

    return forward


async def _probe_posture_closed() -> None:
    app = make_app(fail_posture=FAIL_CLOSED, mode=RETRY, stage="C",
                   forward=_retry_forward_dies("revenue is 3200"))
    status, chunks = await _asgi_request(app, _request_body())
    content = _json_of(chunks)["choices"][0]["message"]["content"]
    assert "verification unavailable" in content, \
        "fail-closed did not refuse on machinery failure"


async def _probe_posture_open() -> None:
    app = make_app(fail_posture=FAIL_OPEN, mode=RETRY, stage="C",
                   forward=_retry_forward_dies("revenue is 3200"))
    status, chunks = await _asgi_request(app, _request_body())
    content = _json_of(chunks)["choices"][0]["message"]["content"]
    assert content == "revenue is 3200", \
        "fail-open did not ship the original on machinery failure"


async def _probe_value_map_enum() -> None:
    # v3 (candidate-path enum, candidate-path enum): the schema pins each token to its
    # reverse-lookup-verified candidate lanes, and the verdict is candidate
    # EXISTENCE — the canned model answers "unknown" for everything, yet the
    # observed value still grounds and only the candidate-zero value blocks.
    seen: dict[str, Any] = {}

    async def forward(body, headers):
        rf = body.get("response_format") or {}
        if rf:
            seen["schema"] = rf["json_schema"]["schema"]
            branches = seen["schema"]["properties"]["values"]["items"]["anyOf"]
            mapping = [{"value": b["properties"]["value"]["enum"][0],
                        "source": "unknown"} for b in branches]
            return 200, {"choices": [{"message": {
                "content": json.dumps({"values": mapping})}}]}
        return 200, {"id": "t", "choices": [{"message": {
            "role": "assistant", "content": "revenue was 4500, forecast 9999"}}]}

    sink = ObservationSink()
    app = make_app(fail_posture=FAIL_OPEN, sink=sink, mode=BLOCK, stage="C",
                   forward=forward)
    status, chunks = await _asgi_request(app, _request_body())
    content = _json_of(chunks)["choices"][0]["message"]["content"]
    branches = seen["schema"]["properties"]["values"]["items"]["anyOf"]
    assert len(branches) == 1 \
        and branches[0]["properties"]["value"]["enum"] == ["4500"] \
        and branches[0]["properties"]["source"]["enum"] == ["$.s1", "unknown"], \
        f"schema did not pin the minted candidates: {branches!r}"
    ev = sink.observations[0].stage_b["eval"]
    assert ev["grounded"] == ["4500"] and ev["missing"] == ["9999"], \
        f"existence verdict is not model-independent: {ev['grounded']}/{ev['missing']}"
    assert "9999" in content and "could not be verified" in content, \
        f"block refusal did not name the candidate-zero value: {content[:120]!r}"


async def _probe_audit_observation() -> None:
    # The opt-in observation event (evidence substrate): OFF must leave the
    # audit stream as before; ON adds exactly one observation event per
    # terminal turn, carrying answer + hashes + steps.
    events: list[dict[str, Any]] = []

    class Capture:
        async def emit(self, event):
            events.append(event)

    app = make_app(fail_posture=FAIL_OPEN, mode=FLAG, stage="C", audit=Capture(),
                   forward=_fake_forward("revenue is 4500", [GOOD_MAP]),
                   emit_observation=True)
    await _asgi_request(app, _request_body())
    kinds = [e.get("event") for e in events]
    assert kinds == ["observation", "verdict"], \
        f"expected [observation, verdict], got {kinds}"
    obs = events[0]
    assert obs["answer"] == "revenue is 4500" and obs["steps"] and \
        obs["sha_canon"] == answer_hashes("revenue is 4500")["sha_canon"], \
        "observation event is missing its substrate (answer/steps/sha_canon)"


async def _probe_answer_hash_header() -> None:
    # The optional response header (default OFF): OFF must be indistinguishable
    # from the pre-header build; ON must differ by exactly the
    # X-Ledvouch-Answer-Hash header (sha_canon of the observed answer) on an
    # unaltered terminal ship — the body stays byte-identical.
    answer = "revenue is 4500"
    expected = answer_hashes(answer)["sha_canon"]

    def app_of(on: bool):
        return make_app(fail_posture=FAIL_OPEN, mode=FLAG, stage="C",
                        forward=_fake_forward(answer, [GOOD_MAP]),
                        answer_hash_header=on)

    _s, headers_off, chunks_off = await _asgi_request_full(app_of(False), _request_body())
    _s, headers_on, chunks_on = await _asgi_request_full(app_of(True), _request_body())
    assert "x-ledvouch-answer-hash" not in headers_off, \
        "OFF still carried the answer-hash header"
    assert b"".join(chunks_off) == b"".join(chunks_on), \
        "ON altered the body (the contract is a header-only delta)"
    assert headers_on.get("x-ledvouch-answer-hash") == expected, \
        f"ON header is not sha_canon of the observed answer: " \
        f"{headers_on.get('x-ledvouch-answer-hash')!r}"


async def _probe_audit_emission() -> None:
    events: list[dict[str, Any]] = []

    class Capture:
        async def emit(self, event):
            events.append(event)

    app = make_app(fail_posture=FAIL_OPEN, mode=BLOCK, stage="C", audit=Capture(),
                   forward=_fake_forward("revenue is 3200", [BAD_MAP]))
    await _asgi_request(app, _request_body())
    kinds = [e.get("event") for e in events]
    assert kinds == ["verdict", "enforcement"], \
        f"expected [verdict, enforcement] audit events, got {kinds}"


# ---- mech.effect_* — effect-terminal gate, stage E0 (observation only) -----

_EFFECT_DECLARATION = json.dumps([
    {"tool": "post_journal", "param_fields": ["$.threshold"]},
])


def _effect_call_response(arguments: str) -> dict[str, Any]:
    return {"id": "t", "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "w1", "type": "function",
                        "function": {"name": "post_journal",
                                     "arguments": arguments}}]}}]}


class _CaptureAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


async def _probe_effect_unconfigured() -> None:
    # Regression: with NO declaration the gate must be fully inert — the
    # designated-looking call ships byte-identically and no effect_* event
    # exists (the unconfigured deployment behaves like a pre-E0 build).
    upstream = _effect_call_response('{"amount": 9999}')
    audit = _CaptureAudit()

    async def forward(body, headers):
        return 200, json.loads(json.dumps(upstream))

    app = make_app(fail_posture=FAIL_OPEN, forward=forward, audit=audit, stage="C")
    _status, chunks = await _asgi_request(app, _request_body())
    assert _json_of(chunks) == upstream, "unconfigured gate altered a normal turn"
    effect_events = [e for e in audit.events if str(e.get("event", "")).startswith("effect_")]
    assert effect_events == [], f"unconfigured gate emitted events: {effect_events}"


async def _probe_effect_observe_and_receipt() -> None:
    # The E0 core: a designated call with an unobserved data value ships
    # UNCHANGED while an effect_verdict(ungrounded) event is emitted (param
    # fields recorded, never judged); the call's observed result on the next
    # request is correlated as an effect_receipt carrying verdict_at_call.
    upstream = _effect_call_response('{"amount": 9999, "threshold": 3}')
    audit = _CaptureAudit()

    async def forward(body, headers):
        return 200, json.loads(json.dumps(upstream))

    app = make_app(fail_posture=FAIL_OPEN, forward=forward, audit=audit, stage="C",
                   effect_terminals=parse_effect_terminals(_EFFECT_DECLARATION))
    _status, chunks = await _asgi_request(app, _request_body())
    assert _json_of(chunks) == upstream, "E0 observation altered the shipped body"
    verdicts = [e for e in audit.events if e.get("event") == "effect_verdict"]
    assert len(verdicts) == 1, f"expected one effect_verdict, got {verdicts}"
    v = verdicts[0]
    assert v["verdict"] == "ungrounded" and v["missing"] == ["9999"], \
        f"expected ungrounded/[9999], got {v['verdict']}/{v['missing']}"
    assert v["param_tokens"] == ["3"] and "3" not in v["missing"], \
        "param field value was judged instead of recorded (D4' boundary broken)"

    receipt_output = '{"journal_id": 4470123}'
    followup = _request_body()
    followup["messages"] = [
        *followup["messages"],
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "post_journal",
                          "arguments": '{"amount": 9999, "threshold": 3}'}}]},
        {"role": "tool", "tool_call_id": "w1", "content": receipt_output},
    ]
    await _asgi_request(app, followup)
    receipts = [e for e in audit.events if e.get("event") == "effect_receipt"]
    assert len(receipts) == 1, f"expected one effect_receipt, got {receipts}"
    r = receipts[0]
    assert r["call_id"] == "w1" and r["verdict_at_call"] == "ungrounded" and \
        r["receipt_sha_canon"] == answer_hashes(receipt_output)["sha_canon"], \
        "receipt correlation lost the join key, the write-time stain, or the hash"


async def _probe_effect_grounded_observed_value() -> None:
    # A data value copied from an observed tool result is grounded — the gate
    # judges argument provenance with the same reverse-lookup the terminal uses.
    upstream = _effect_call_response('{"amount": 4500}')  # $.s1 revenue, observed
    audit = _CaptureAudit()

    async def forward(body, headers):
        return 200, json.loads(json.dumps(upstream))

    app = make_app(fail_posture=FAIL_OPEN, forward=forward, audit=audit, stage="C",
                   effect_terminals=parse_effect_terminals(_EFFECT_DECLARATION))
    await _asgi_request(app, _request_body())
    v = [e for e in audit.events if e.get("event") == "effect_verdict"][0]
    assert v["verdict"] == "grounded", \
        f"observed value judged {v['verdict']}: {v.get('reasons')}"


async def _probe_effect_stream_tee() -> None:
    # Stream passthrough with a designated call: the tee must keep the
    # chunk-unit byte identity contract AND still yield an effect_verdict after
    # the last byte (E0 observes, never buffers the passthrough).
    raw = _sse([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"tool_calls": [{"index": 0, "id": "w1", "type": "function",
                                "function": {"name": "post_journal",
                                             "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {
            "arguments": '{\"amount\": 9999}'}}]}),
        _chunk({}, finish="tool_calls"),
        "[DONE]",
    ])
    sent = [raw[:17], raw[17:120], raw[120:]]
    audit = _CaptureAudit()
    app = make_app(fail_posture=FAIL_OPEN, audit=audit,
                   stream_forward=_stream_forward_of(sent),
                   effect_terminals=parse_effect_terminals(_EFFECT_DECLARATION))
    _status, received = await _asgi_request(app, _request_body(stream=True))
    assert received == sent, "effect tee broke passthrough chunk identity"
    verdicts = [e for e in audit.events if e.get("event") == "effect_verdict"]
    assert len(verdicts) == 1 and verdicts[0]["verdict"] == "ungrounded", \
        f"tee observation missing or wrong: {verdicts}"


async def _probe_effect_config_refusal() -> None:
    # fail-fast declaration: malformation refuses (startup-refusal semantics) —
    # nothing is coerced, defaulted around, or ignored.
    for bad, needle in [
        ('[{"tool": "t", "mode": "maybe"}]', "mode"),
        ('[{"tool": "t", "degrade": "retry"}]', "degrade"),
        ('[{"tool": "t"}, {"tool": "t"}]', "twice"),
        ('[{"tool": "t", "data_fields": ["amount"]}]', "dot path"),
        ('[{"tool": "t", "data_fields": ["$.a"], "param_fields": ["$.a.b"]}]', "overlap"),
    ]:
        try:
            parse_effect_terminals(bad)
        except EffectConfigError as e:
            assert needle in str(e), f"refusal message lacks {needle!r}: {e}"
        else:
            raise AssertionError(f"malformed declaration accepted: {bad}")


# ---- mech.effect_* — stage E1 (enforcement) --------------------------------

_EFFECT_BLOCK_DECL = json.dumps([{"tool": "post_journal", "mode": "block"}])
_EFFECT_RETRY_DECL = json.dumps([{"tool": "post_journal", "mode": "retry"}])


def _effect_forward_of(arguments: str, retry_responses: list[dict[str, Any]] | None = None,
                       seen: list[dict[str, Any]] | None = None):
    """Canned upstream for effect probes: the agent's turn is a designated
    call; a request whose last message is the gate's synthetic tool feedback
    is an effect retry round."""
    retries = list(retry_responses or [])

    async def forward(body, headers):
        if seen is not None:
            seen.append(json.loads(json.dumps(body)))
        last = (body.get("messages") or [])[-1]
        if last.get("role") == "tool" and "grounding ledger voucher" in str(last.get("content", "")):
            return 200, json.loads(json.dumps(retries.pop(0)))
        return 200, json.loads(json.dumps(_effect_call_response(arguments)))

    return forward


async def _probe_effect_block() -> None:
    # ② an ungrounded designated call is stopped BEFORE execution: the
    # tool_calls response is replaced by an honest refusal text (no surviving
    # tool_call for the harness to execute), and nothing enters the pending
    # receipt map (a blocked call can never produce a receipt).
    audit = _CaptureAudit()
    app = make_app(fail_posture=FAIL_OPEN, audit=audit,
                   forward=_effect_forward_of('{"amount": 9999}'),
                   effect_terminals=parse_effect_terminals(_EFFECT_BLOCK_DECL))
    _status, chunks = await _asgi_request(app, _request_body())
    msg = _json_of(chunks)["choices"][0]["message"]
    assert not msg.get("tool_calls"), "block left an executable tool_call in the response"
    assert "9999" in (msg.get("content") or "") and "before execution" in msg["content"], \
        f"block refusal missing or not actionable: {str(msg.get('content'))[:120]!r}"
    assert len(app.state.effect_pending) == 0, "a blocked call was registered for a receipt"
    actions = [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]
    assert actions == ["block"], f"expected [block] enforcement, got {actions}"


async def _probe_effect_enforce_grounded() -> None:
    # ③ a grounded designated call ships VERBATIM under enforcement — the gate
    # never rewrites arguments; a call ships exactly as the model wrote it,
    # or not at all.
    upstream = _effect_call_response('{"amount": 4500}')
    app = make_app(fail_posture=FAIL_OPEN,
                   forward=_effect_forward_of('{"amount": 4500}'),
                   effect_terminals=parse_effect_terminals(_EFFECT_BLOCK_DECL))
    _status, chunks = await _asgi_request(app, _request_body())
    assert _json_of(chunks) == upstream, "enforcement altered a grounded call"
    assert len(app.state.effect_pending) == 1, "shipped call was not registered for its receipt"


async def _probe_effect_retry_repair() -> None:
    # Effect retry (D5): the push-back is a synthetic tool exchange — the
    # model's own rejected call verbatim, answered by the gate's refusal in
    # the tool role, never claiming execution — and the SHIPPED response is
    # the model's own corrected call.
    corrected = _effect_call_response('{"amount": 4500}')
    seen: list[dict[str, Any]] = []
    audit = _CaptureAudit()
    app = make_app(fail_posture=FAIL_OPEN, audit=audit,
                   forward=_effect_forward_of('{"amount": 9999}', [corrected], seen),
                   effect_terminals=parse_effect_terminals(_EFFECT_RETRY_DECL))
    _status, chunks = await _asgi_request(app, _request_body())
    assert _json_of(chunks) == corrected, "retry did not ship the model's own corrected call"
    retry_req = seen[1]
    tail = retry_req["messages"][-2:]
    assert tail[0]["role"] == "assistant" and \
        tail[0]["tool_calls"][0]["function"]["arguments"] == '{"amount": 9999}', \
        "push-back did not carry the rejected call verbatim"
    assert tail[1]["role"] == "tool" and tail[1]["tool_call_id"] == "w1" and \
        "NOT executed" in tail[1]["content"], \
        "synthetic tool result missing or dishonest about execution"
    actions = [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]
    assert actions == ["repair"], f"expected [repair], got {actions}"


async def _probe_effect_degrade() -> None:
    # ④ retry non-convergence (the missing set did not shrink) degrades to
    # BLOCK (D1 — an executed unverified write is the harm itself); the
    # explicit per-tool "flag" override (D2) ships the original instead.
    upstream = _effect_call_response('{"amount": 9999}')

    def app_of(decl: str, audit):
        return make_app(fail_posture=FAIL_OPEN, audit=audit,
                        forward=_effect_forward_of('{"amount": 9999}', [upstream]),
                        effect_terminals=parse_effect_terminals(decl))

    audit = _CaptureAudit()
    _status, chunks = await _asgi_request(app_of(_EFFECT_RETRY_DECL, audit), _request_body())
    msg = _json_of(chunks)["choices"][0]["message"]
    assert not msg.get("tool_calls") and "9999" in (msg.get("content") or ""), \
        "non-convergence did not degrade to an honest block (D1)"
    actions = [e["action"] for e in audit.events if e.get("event") == "effect_enforcement"]
    assert actions == ["degrade_block"], f"expected [degrade_block], got {actions}"

    flag_decl = json.dumps([{"tool": "post_journal", "mode": "retry", "degrade": "flag"}])
    audit2 = _CaptureAudit()
    _status, chunks2 = await _asgi_request(app_of(flag_decl, audit2), _request_body())
    assert _json_of(chunks2) == upstream, "degrade=flag did not ship the original"
    actions2 = [e["action"] for e in audit2.events if e.get("event") == "effect_enforcement"]
    assert actions2 == ["degrade_flag"], f"expected [degrade_flag], got {actions2}"


async def _probe_effect_posture() -> None:
    # ⑤ unparseable designated arguments under enforcement leave NO verdict —
    # the A2 fail posture decides: closed refuses (nothing executes), open
    # ships the original with the absence recorded.
    upstream = _effect_call_response("amount=9999,not json")

    def app_of(posture: str, audit):
        return make_app(fail_posture=posture, audit=audit,
                        forward=_effect_forward_of("amount=9999,not json"),
                        effect_terminals=parse_effect_terminals(_EFFECT_BLOCK_DECL))

    audit_closed = _CaptureAudit()
    _status, chunks = await _asgi_request(app_of(FAIL_CLOSED, audit_closed), _request_body())
    msg = _json_of(chunks)["choices"][0]["message"]
    assert not msg.get("tool_calls") and "verification unavailable" in (msg.get("content") or ""), \
        "fail-closed did not refuse an unverifiable designated call"

    audit_open = _CaptureAudit()
    _status, chunks2 = await _asgi_request(app_of(FAIL_OPEN, audit_open), _request_body())
    assert _json_of(chunks2) == upstream, "fail-open did not ship the original"
    actions = [e["action"] for e in audit_open.events if e.get("event") == "effect_enforcement"]
    assert actions == ["degrade_flag"], f"expected [degrade_flag], got {actions}"


async def _probe_effect_stream_buffered() -> None:
    # §4.1 contract revision: a conversation whose request tools[] offers an
    # enforcement-mode designated tool BUFFERS its tool_calls turns (the
    # verdict must precede the first shipped byte) — an ungrounded write ships
    # as a synthesized refusal stream, never as the model's call bytes.
    raw = _sse([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"tool_calls": [{"index": 0, "id": "w1", "type": "function",
                                "function": {"name": "post_journal",
                                             "arguments": '{"amount": 9999}'}}]}),
        _chunk({}, finish="tool_calls"),
        "[DONE]",
    ])
    body = _request_body(stream=True)
    body["tools"] = [*body["tools"],
                     {"type": "function", "function": {"name": "post_journal"}}]
    app = make_app(fail_posture=FAIL_OPEN,
                   stream_forward=_stream_forward_of([raw[:40], raw[40:]]),
                   effect_terminals=parse_effect_terminals(_EFFECT_BLOCK_DECL))
    _status, received = await _asgi_request(app, body)
    concat = _data_concat(b"".join(received)).decode()
    assert "9999" not in concat or "before execution" in concat, \
        "buffered enforcement did not gate the stream"
    assert "before execution" in concat and "tool_calls" not in concat, \
        f"expected a synthesized refusal stream, got: {concat[:200]!r}"


_MECH_PROBES: list[tuple[str, str, Callable[[], Awaitable[None]]]] = [
    ("mech.spectator_nonstream", "normal turn ships byte-identical", _probe_spectator_nonstream),
    ("mech.terminal_flag_transparent", "flag terminal ships unchanged with verdict recorded", _probe_terminal_flag_transparent),
    ("mech.stream_passthrough", "stream tool_calls turn is chunk-byte-identical", _probe_stream_passthrough),
    ("mech.stream_buffered_concat", "stream terminal is payload-concat-identical", _probe_stream_buffered_concat),
    ("mech.enforcement_block", "block replaces ungrounded answer with actionable refusal", _probe_enforcement_block),
    ("mech.enforcement_retry_keepalive", "retry hands tool_calls keep-alive to the harness", _probe_enforcement_retry_keepalive),
    ("mech.enforcement_retry_repair", "retry ships the model's own repaired answer", _probe_enforcement_retry_repair),
    ("mech.posture_closed", "fail-closed refuses when verification is unavailable", _probe_posture_closed),
    ("mech.posture_open", "fail-open ships the original when verification is unavailable", _probe_posture_open),
    ("mech.audit_emission", "verdict + enforcement audit events are emitted", _probe_audit_emission),
    ("mech.answer_hash_header", "answer-hash header: OFF identical, ON header-only delta", _probe_answer_hash_header),
    ("mech.audit_observation", "opt-in observation event carries the evidence substrate", _probe_audit_observation),
    ("mech.value_map_enum", "candidate-enum verdict is deterministic and model-independent", _probe_value_map_enum),
    ("mech.effect_unconfigured", "unconfigured effect gate is fully inert (pre-E0 behavior)", _probe_effect_unconfigured),
    ("mech.effect_observe", "designated call ships unchanged; verdict + receipt are recorded", _probe_effect_observe_and_receipt),
    ("mech.effect_grounded", "observed argument value is judged grounded", _probe_effect_grounded_observed_value),
    ("mech.effect_stream_tee", "effect tee keeps stream chunk identity and still observes", _probe_effect_stream_tee),
    ("mech.effect_config_refusal", "malformed effect declaration refuses, never ignores", _probe_effect_config_refusal),
    ("mech.effect_block", "ungrounded designated call is stopped before execution", _probe_effect_block),
    ("mech.effect_enforce_grounded", "grounded designated call ships verbatim under enforcement", _probe_effect_enforce_grounded),
    ("mech.effect_retry_repair", "effect retry ships the model's own corrected call", _probe_effect_retry_repair),
    ("mech.effect_degrade", "effect non-convergence degrades to block (D1; per-tool flag override)", _probe_effect_degrade),
    ("mech.effect_posture", "unparseable designated arguments follow the fail posture", _probe_effect_posture),
    ("mech.effect_stream_buffered", "enforced conversation buffers the stream; no unverified write ships", _probe_effect_stream_buffered),
]


async def _mechanism_checks() -> list[Check]:
    out: list[Check] = []
    for check_id, title, probe in _MECH_PROBES:
        try:
            await probe()
            out.append(Check(check_id, title, "pass"))
        except AssertionError as e:
            out.append(Check(check_id, title, "fail", str(e)))
        except Exception as e:  # a crashed probe is a FAIL, never a silent pass
            out.append(Check(check_id, title, "fail",
                             f"probe crashed: {type(e).__name__}: {e}"))
    return out


# ---- live.* — the canned exchange against the real upstream ----------------


def _live_request(model: str) -> dict[str, Any]:
    # deterministic on a deterministic upstream: temp 0, tiny, closed-form
    return {
        "model": model,
        "messages": [{"role": "user", "content":
                      "Answer with exactly one word: what is 2+2? "}],
        "temperature": 0,
        "max_tokens": 16,
    }


def _content_of(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


async def _live_checks(env: Mapping[str, str]) -> list[Check]:
    model = env.get("LEDVOUCH_DOCTOR_MODEL")
    if not model:
        return [Check("live.upstream_reachable", "real upstream answers the canned request",
                      "fail", "set LEDVOUCH_DOCTOR_MODEL to the model name for live probes"),
                Check("live.transparency_delta", "ledger voucher path matches direct path (Δ=0)",
                      "fail", "set LEDVOUCH_DOCTOR_MODEL to the model name for live probes")]
    checks: list[Check] = []
    forward = _default_forward()
    req = _live_request(model)
    try:
        status_a, body_a = await forward(dict(req), {})
        status_a2, body_a2 = await forward(dict(req), {})
    except Exception as e:
        return [Check("live.upstream_reachable", "real upstream answers the canned request",
                      "fail", f"upstream unreachable: {type(e).__name__}: {e}"),
                Check("live.transparency_delta", "ledger voucher path matches direct path (Δ=0)",
                      "skip", "unreachable upstream — Δ not measurable")]
    if status_a != 200 or status_a2 != 200:
        return [Check("live.upstream_reachable", "real upstream answers the canned request",
                      "fail", f"upstream returned {status_a}/{status_a2}: "
                              f"{str(body_a)[:200]}"),
                Check("live.transparency_delta", "ledger voucher path matches direct path (Δ=0)",
                      "skip", "non-200 upstream — Δ not measurable")]
    checks.append(Check("live.upstream_reachable", "real upstream answers the canned request",
                        "pass", f"model={model}"))

    if _content_of(body_a) != _content_of(body_a2):
        # honest skip: only a deterministic upstream carries a live Δ=0 claim;
        # the hermetic transparency proof (mech.*) stands regardless.
        checks.append(Check(
            "live.transparency_delta", "ledger voucher path matches direct path (Δ=0)", "skip",
            "upstream is nondeterministic at temperature 0 — live Δ is not "
            "measurable (hermetic transparency checks still apply)"))
        return checks

    # spectator app over the real upstream: stage A flag = pure passthrough
    # (no hidden calls, no enforcement — the transparency posture).
    app = make_app(fail_posture=FAIL_OPEN, mode=FLAG, stage="A")
    status_b, chunks = await _asgi_request(app, dict(req))
    body_b = _json_of(chunks)
    if status_b == 200 and _content_of(body_b) == _content_of(body_a):
        checks.append(Check("live.transparency_delta",
                            "ledger voucher path matches direct path (Δ=0)", "pass"))
    else:
        checks.append(Check(
            "live.transparency_delta", "ledger voucher path matches direct path (Δ=0)", "fail",
            f"direct={_content_of(body_a)!r} via-ledger voucher={_content_of(body_b)!r} "
            f"(status {status_b})"))
    return checks


# ---- suite -----------------------------------------------------------------


def run_suite(*, live: bool = False, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run the conformance suite. Returns the v0 report: deterministic, no
    timestamps — same deployment, same verdict, whoever runs it."""
    env = os.environ if env is None else env
    checks = shape_checks(env)
    checks += asyncio.run(_mechanism_checks())
    if live:
        checks += asyncio.run(_live_checks(env))
    else:
        checks += [
            Check("live.upstream_reachable", "real upstream answers the canned request",
                  "skip", "run with --live to probe the real upstream"),
            Check("live.transparency_delta", "ledger voucher path matches direct path (Δ=0)",
                  "skip", "run with --live to probe the real upstream"),
        ]
    verdict = "pass" if all(c.status != "fail" for c in checks) else "fail"
    return {"suite": SUITE, "live": live, "verdict": verdict,
            "checks": [asdict(c) for c in checks]}
