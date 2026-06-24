"""Incident KPI computation — pure domain (no AWS).

Derives operational metrics from a list of incidents, mirroring the metrics AWS
Incident Manager published (NumberOfCreateIncidents, NumberOfResolveIncidents,
TimeToFirstAcknowledgement, TimeToResolveIncident) plus a few extras Relay can
offer for free from its timeline.

All durations are in **seconds**. Aggregates use the mean and the median (p50)
because incident-response time distributions are heavily skewed and the median
is the more honest "typical" number.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from relay.core.model import IncidentState, Severity

if TYPE_CHECKING:
    from relay.core.model import Incident

# Timeline event_type strings that mark resolution (see hub/app.py resolve path).
_RESOLVED_EVENT_TYPES = frozenset({"resolved", "incident.resolved"})


def _resolved_at(incident: Incident) -> datetime | None:
    """When the incident was resolved, or None if still open.

    Prefers an explicit ``resolved`` timeline event; falls back to
    ``updated_at`` when the incident is in a terminal state but carries no such
    event (older records).
    """
    for ev in incident.timeline:
        if ev.event_type in _RESOLVED_EVENT_TYPES:
            return ev.occurred_at
    if incident.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
        return incident.updated_at
    return None


def _seconds(a: datetime, b: datetime) -> float | None:
    """(b - a) in seconds, or None if either is missing or b < a."""
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


@dataclass
class DurationStat:
    """Summary of a set of durations (seconds)."""

    count: int = 0
    mean: float | None = None
    p50: float | None = None
    p90: float | None = None
    max: float | None = None

    @classmethod
    def from_samples(cls, samples: list[float]) -> DurationStat:
        vals = sorted(s for s in samples if s is not None)
        if not vals:
            return cls()
        return cls(
            count=len(vals),
            mean=round(statistics.fmean(vals), 1),
            p50=round(_percentile(vals, 0.50), 1),
            p90=round(_percentile(vals, 0.90), 1),
            max=round(vals[-1], 1),
        )

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "mean": self.mean,
            "p50": self.p50,
            "p90": self.p90,
            "max": self.max,
        }


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])
    return sorted_vals[lo]


@dataclass
class IncidentMetrics:
    """Computed KPIs over a set of incidents in a window."""

    total: int = 0
    # How many of ``total`` were synthetic ("test"/"fake") incidents. Synthetic
    # incidents are counted in every metric (that's how the pipeline is verified);
    # this field lets the UI flag that some figures rest on test data.
    synthetic_total: int = 0
    open: int = 0
    resolved: int = 0
    acknowledged: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    by_state: dict[str, int] = field(default_factory=dict)
    # Time-to-first-acknowledgement and time-to-resolve, in seconds.
    time_to_ack: DurationStat = field(default_factory=DurationStat)
    time_to_resolve: DurationStat = field(default_factory=DurationStat)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "synthetic_total": self.synthetic_total,
            "open": self.open,
            "resolved": self.resolved,
            "acknowledged": self.acknowledged,
            "by_severity": self.by_severity,
            "by_source": self.by_source,
            "by_state": self.by_state,
            "time_to_ack_seconds": self.time_to_ack.as_dict(),
            "time_to_resolve_seconds": self.time_to_resolve.as_dict(),
        }


def compute_metrics(incidents: list[Incident]) -> IncidentMetrics:
    """Compute KPIs over ``incidents``.

    - count totals + breakdowns by severity / source / state
    - time-to-first-ack: acknowledged_at - created_at (where acked)
    - time-to-resolve (MTTR): resolved time - created_at (where resolved)

    Synthetic ("test"/"fake") incidents ARE included in every number: triggering
    a synthetic incident and watching it flow through to the metrics is exactly
    how a team verifies the pipeline works end-to-end. To keep that honest we
    also report ``synthetic_total`` — the count of incidents in this window that
    were synthetic — so the UI can flag that some figures rest on test data and
    an operator can purge them when they're done smoke-testing.
    """
    m = IncidentMetrics(total=len(incidents))
    m.synthetic_total = sum(1 for inc in incidents if getattr(inc, "synthetic", False))
    by_sev: dict[str, int] = {s.value: 0 for s in Severity}
    by_source: dict[str, int] = {}
    by_state: dict[str, int] = {}
    ack_samples: list[float] = []
    resolve_samples: list[float] = []

    for inc in incidents:
        by_sev[inc.severity.value] = by_sev.get(inc.severity.value, 0) + 1
        by_source[inc.signal_source.value] = by_source.get(inc.signal_source.value, 0) + 1
        by_state[inc.state.value] = by_state.get(inc.state.value, 0) + 1

        if inc.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
            m.resolved += 1
        else:
            m.open += 1
        if inc.acknowledged_at is not None:
            m.acknowledged += 1
            tta = _seconds(inc.created_at, inc.acknowledged_at)
            if tta is not None:
                ack_samples.append(tta)

        resolved_at = _resolved_at(inc)
        if resolved_at is not None:
            ttr = _seconds(inc.created_at, resolved_at)
            if ttr is not None:
                resolve_samples.append(ttr)

    # Drop zero-valued severity buckets only if there were no incidents at all,
    # otherwise keep the full SEV1-4 keys so the UI can render a stable axis.
    m.by_severity = by_sev
    m.by_source = by_source
    m.by_state = by_state
    m.time_to_ack = DurationStat.from_samples(ack_samples)
    m.time_to_resolve = DurationStat.from_samples(resolve_samples)
    return m


def humanize_seconds(seconds: float | None) -> str:
    """Render a duration like '4m 12s' / '1h 3m' / '—' for display."""
    if seconds is None:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


__all__ = [
    "DurationStat",
    "IncidentMetrics",
    "compute_metrics",
    "humanize_seconds",
]
