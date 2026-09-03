"""Audit event stream — A3 (stateless by design).

# Design rationale:
#   Every governance fact (terminal verdict, enforcement activation, fail-posture
#   activation, laundering detection) is emitted as ONE structured JSON event to
#   a customer-controlled destination — stdout / file / webhook, selected by env.
#   The ledger voucher NEVER persists these itself: logs live under the customer's
#   control (data sovereignty). The schema is versioned and fixed in README.md —
#   it is the substrate for certification evidence, billing true-up and future
#   registry/certification needs, so every event carries deployment/system identifiers from
#   day one (null when unset — a visible absence, never a silent drop).
#
# fail-fast boundary: an audit emit failure must never break shipping (the
#   response path). Failures are surfaced on stderr — degraded observability is
#   reported, not silently swallowed, and never traded against the client's
#   traffic. The webhook emitter posts fire-and-forget for the same reason.
#
# security (A5): events never carry credentials — the upstream key is not
#   available to this module at all (proxy.py keeps it inside the forward
#   closures).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

# v1 (2026-07-29): verdict events additionally carry sha_raw/sha_canon — the
# answer content hashes (content_hash.py, evidence-layer join key). Additive
# only; the README schema table is the fixed reference per version.
AUDIT_SCHEMA = "ledger-vouching.audit.v1"

# actions that constitute an enforcement activation (a body the client received
# that differs from what the model produced, or a deliberate keep-alive)
ENFORCEMENT_ACTIONS = ("block", "pushback", "repair", "posture_block")


def render_event(event: dict[str, Any]) -> str:
    """One audit line: the fixed envelope + the event payload. deployment_id /
    system_id identify this ledger voucher deployment and the governed agent system
    (certification / true-up / registry keys)."""
    return json.dumps(
        {
            "schema": AUDIT_SCHEMA,
            "ts": datetime.now(timezone.utc).isoformat(),
            "deployment_id": os.environ.get("LEDVOUCH_DEPLOYMENT_ID"),
            "system_id": os.environ.get("LEDVOUCH_SYSTEM_ID"),
            **event,
        },
        ensure_ascii=False,
    )


class StdoutAuditEmitter:
    """Default destination: one JSON line per event on stdout (the sidecar's
    log pipeline is customer infrastructure)."""

    async def emit(self, event: dict[str, Any]) -> None:
        print(render_event(event), flush=True)


class FileAuditEmitter:
    """Append-only JSONL file on the customer's disk."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def emit(self, event: dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(render_event(event) + "\n")


class WebhookAuditEmitter:
    """POST each event to a customer endpoint. Fire-and-forget: the response
    path is never delayed or broken by audit delivery; delivery failures go to
    stderr (visible degradation, not silence)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def emit(self, event: dict[str, Any]) -> None:
        line = render_event(event)
        asyncio.get_running_loop().create_task(self._post(line))

    async def _post(self, line: str) -> None:
        try:
            await self._client.post(
                self.url, content=line, headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            print(f"ledvouch audit webhook delivery failed: {e}", file=sys.stderr)


def audit_emitter_from_env():
    """LEDVOUCH_AUDIT_STREAM ∈ {stdout, file, webhook} (documented default:
    stdout). file/webhook require their destination env — missing destination
    is a startup refusal, not a silent fallback."""
    kind = os.environ.get("LEDVOUCH_AUDIT_STREAM", "stdout")
    if kind == "stdout":
        return StdoutAuditEmitter()
    if kind == "file":
        path = os.environ.get("LEDVOUCH_AUDIT_FILE")
        if not path:
            raise RuntimeError("LEDVOUCH_AUDIT_STREAM=file requires LEDVOUCH_AUDIT_FILE")
        return FileAuditEmitter(path)
    if kind == "webhook":
        url = os.environ.get("LEDVOUCH_AUDIT_WEBHOOK_URL")
        if not url:
            raise RuntimeError(
                "LEDVOUCH_AUDIT_STREAM=webhook requires LEDVOUCH_AUDIT_WEBHOOK_URL"
            )
        return WebhookAuditEmitter(url)
    raise RuntimeError(
        f"unknown LEDVOUCH_AUDIT_STREAM {kind!r} (expected stdout | file | webhook)"
    )


def posture_event(posture: str, trigger: str) -> dict[str, Any]:
    """A fail-posture activation outside the terminal path (e.g. upstream
    unreachable, stream parse failure)."""
    return {"event": "posture", "posture": posture, "trigger": trigger}


def terminal_events(
    *,
    mode: str,
    stage: str,
    floor_verdict: str,
    floor_missing: tuple[str, ...],
    payload: dict[str, Any] | None,
    sha_raw: str | None = None,
    sha_canon: str | None = None,
) -> list[dict[str, Any]]:
    """Derive the audit events of one terminal turn from its observation payload
    (the stage-C payload; the stage-B payload; None on stage A). Pure — unit
    testable without a rig. Event kinds (README schema v1):
      verdict     — every terminal turn (the always-on audit trail); carries the
                    answer content hashes (evidence-layer join key)
      enforcement — the shipped body was altered / a keep-alive was issued
      laundering  — laundered argument values detected (recorded, not enforced)
      posture     — the fail posture decided an outcome (open or closed)
    """
    p = payload or {}
    verdict = p.get("verdict", floor_verdict)
    action = p.get("action", "ship")
    ev = p.get("eval") or {}
    missing = ev.get("missing", list(floor_missing))
    events: list[dict[str, Any]] = [
        {
            "event": "verdict",
            "mode": mode,
            "stage": stage,
            "verdict": verdict,
            "action": action,
            "missing": missing,
            "sha_raw": sha_raw,
            "sha_canon": sha_canon,
        }
    ]
    if action in ENFORCEMENT_ACTIONS:
        events.append(
            {
                "event": "enforcement",
                "action": action,
                "missing": missing,
                "reasons": ev.get("reasons", {}),
            }
        )
    if p.get("laundered"):
        events.append({"event": "laundering", "laundered": p["laundered"]})
    if p.get("posture_trigger"):
        events.append(posture_event(p.get("posture", ""), p["posture_trigger"]))
    return events
