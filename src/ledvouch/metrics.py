"""Operational metrics — A4.

In-memory counters served by GET /metrics (JSON). Deliberately minimal: request
counts, verdict distribution, enforcement actions, hidden/retry call counts and
main-upstream latency — the operationally load-bearing fields only. No persistence
(stateless ledger voucher); a scrape pipeline is customer infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Metrics:
    requests_total: int = 0
    requests_stream: int = 0
    turns_normal: int = 0
    turns_terminal: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    # effect-terminal gate: per-verdict counts for designated side-effect
    # calls, enforcement actions (E1) and emitted receipt correlations.
    # Surfaced in /metrics only when effect terminals are configured (the
    # endpoint assembles the section) — the unconfigured snapshot stays
    # byte-identical.
    effect_verdicts: dict[str, int] = field(default_factory=dict)
    effect_actions: dict[str, int] = field(default_factory=dict)
    effect_receipts: int = 0
    hidden_calls: int = 0
    retry_calls: int = 0
    pushbacks: int = 0
    upstream_errors: int = 0
    upstream_latency_count: int = 0
    upstream_latency_sum_ms: float = 0.0
    upstream_latency_max_ms: float = 0.0

    def count(self, table: str, key: str) -> None:
        d: dict[str, int] = getattr(self, table)
        d[key] = d.get(key, 0) + 1

    def observe_upstream_ms(self, ms: float) -> None:
        self.upstream_latency_count += 1
        self.upstream_latency_sum_ms += ms
        self.upstream_latency_max_ms = max(self.upstream_latency_max_ms, ms)

    def snapshot(self) -> dict[str, Any]:
        avg = (
            self.upstream_latency_sum_ms / self.upstream_latency_count
            if self.upstream_latency_count
            else None
        )
        return {
            "requests": {"total": self.requests_total, "stream": self.requests_stream},
            "turns": {"normal": self.turns_normal, "terminal": self.turns_terminal},
            "verdicts": dict(self.verdicts),
            "actions": dict(self.actions),
            "calls": {
                "hidden": self.hidden_calls,
                "retry": self.retry_calls,
                "pushbacks": self.pushbacks,
            },
            "upstream": {
                "errors": self.upstream_errors,
                "latency_ms": {
                    "count": self.upstream_latency_count,
                    "avg": avg,
                    "max": self.upstream_latency_max_ms,
                },
            },
        }
