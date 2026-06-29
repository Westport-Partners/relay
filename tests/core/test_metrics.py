"""Tests for incident KPI computation (relay.core.metrics)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from relay.core.metrics import (
    DurationStat,
    compute_metrics,
    humanize_seconds,
)
from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _inc(cid, sev=Severity.SEV2, source=SignalSource.CLOUDWATCH_ALARM,
         state=IncidentState.TRIGGERED, created=T0, acked=None, resolved=None):
    timeline = []
    if resolved is not None:
        timeline.append(TimelineEvent(
            event_id=f"res-{cid}", incident_id=cid, stream=Stream.CENTRAL,
            occurred_at=resolved, actor="op", event_type="resolved", detail={},
        ))
    return Incident(
        correlation_id=cid, account_id="111111111111", region="us-east-1",
        app_name="api", severity=sev, signal_source=source, state=state,
        alarm_name="prod-5xx", created_at=created, updated_at=created,
        acknowledged_at=acked, acknowledged_by=("op" if acked else None),
        timeline=timeline,
    )


def test_empty():
    m = compute_metrics([]).as_dict()
    assert m["total"] == 0
    assert m["time_to_ack_seconds"]["count"] == 0
    assert m["time_to_resolve_seconds"]["p50"] is None


def test_counts_and_breakdowns():
    incs = [
        _inc("a", sev=Severity.SEV1, source=SignalSource.CLOUDWATCH_ALARM),
        _inc("b", sev=Severity.SEV1, source=SignalSource.SYNTHETIC,
             state=IncidentState.RESOLVED, resolved=T0 + timedelta(minutes=10)),
        _inc("c", sev=Severity.SEV3, source=SignalSource.MANUAL,
             state=IncidentState.ACKNOWLEDGED, acked=T0 + timedelta(minutes=2)),
    ]
    m = compute_metrics(incs).as_dict()
    assert m["total"] == 3
    assert m["resolved"] == 1
    assert m["open"] == 2
    assert m["acknowledged"] == 1
    assert m["by_severity"]["SEV1"] == 2
    assert m["by_severity"]["SEV3"] == 1
    assert m["by_severity"]["SEV2"] == 0  # stable axis: zero bucket retained
    assert m["by_source"]["SYNTHETIC"] == 1
    assert m["by_state"]["RESOLVED"] == 1


def test_time_to_ack():
    incs = [
        _inc("a", acked=T0 + timedelta(seconds=60)),
        _inc("b", acked=T0 + timedelta(seconds=180)),
    ]
    m = compute_metrics(incs).as_dict()
    tta = m["time_to_ack_seconds"]
    assert tta["count"] == 2
    assert tta["mean"] == 120.0
    assert tta["p50"] == 120.0
    assert tta["max"] == 180.0


def test_time_to_resolve_uses_resolved_event():
    incs = [
        _inc("a", state=IncidentState.RESOLVED, resolved=T0 + timedelta(minutes=5)),
        _inc("b", state=IncidentState.RESOLVED, resolved=T0 + timedelta(minutes=15)),
    ]
    m = compute_metrics(incs).as_dict()
    ttr = m["time_to_resolve_seconds"]
    assert ttr["count"] == 2
    assert ttr["mean"] == 600.0  # (300 + 900) / 2
    assert ttr["max"] == 900.0


def test_resolve_falls_back_to_updated_at_without_event():
    # Terminal state but no 'resolved' timeline event -> use updated_at.
    inc = _inc("a", state=IncidentState.CLOSED)
    inc.updated_at = T0 + timedelta(minutes=20)
    m = compute_metrics([inc]).as_dict()
    assert m["time_to_resolve_seconds"]["count"] == 1
    assert m["time_to_resolve_seconds"]["p50"] == 1200.0


def test_negative_durations_ignored():
    # resolved before created (clock skew) -> dropped, not negative.
    inc = _inc("a", state=IncidentState.RESOLVED, resolved=T0 - timedelta(minutes=5))
    m = compute_metrics([inc]).as_dict()
    assert m["time_to_resolve_seconds"]["count"] == 0


def test_durationstat_percentiles():
    st = DurationStat.from_samples([10, 20, 30, 40, 100])
    assert st.count == 5
    assert st.p50 == 30.0
    assert st.max == 100.0
    assert st.p90 is not None and st.p90 > 40


def test_humanize_seconds():
    assert humanize_seconds(None) == "—"
    assert humanize_seconds(45) == "45s"
    assert humanize_seconds(125) == "2m 5s"
    assert humanize_seconds(3700) == "1h 1m"
    assert humanize_seconds(90000) == "1d 1h"


def test_synthetic_incidents_are_counted_in_metrics():
    """Synthetic incidents are INCLUDED in every metric — that's how the
    pipeline is verified — and reported separately via synthetic_total."""
    real = _inc("real-1", state=IncidentState.RESOLVED,
                acked=T0 + timedelta(minutes=1), resolved=T0 + timedelta(minutes=5))
    synth = _inc("syn-1", state=IncidentState.TRIGGERED)
    synth.synthetic = True

    m = compute_metrics([real, synth]).as_dict()

    # Both incidents counted in the totals — synthetic is not filtered out.
    assert m["total"] == 2
    assert m["open"] == 1
    assert m["resolved"] == 1
    # The synthetic count is surfaced so the UI can flag test-data figures.
    assert m["synthetic_total"] == 1


def test_synthetic_total_zero_when_no_synthetic():
    m = compute_metrics([_inc("a"), _inc("b")]).as_dict()
    assert m["total"] == 2
    assert m["synthetic_total"] == 0
