"""Parity test: metrics-compute.js must reproduce core/metrics.py exactly.

The dashboard recomputes env-scoped KPIs client-side (issue #40). To guarantee
the client numbers match the server's, this test runs ONE shared incident
fixture through both:

  * Python  — relay.core.metrics.compute_metrics(...).as_dict()  (expected)
  * Node    — dashboard_modules/metrics-compute.js computeMetrics(...) (actual)

fed the same serialized incident shape the /incidents endpoints emit. If the JS
port drifts from the domain math, this fails. Skipped if node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from relay.core.metrics import _resolved_at, compute_metrics
from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node not available")

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file()
)
_MODULE = _REPO_ROOT / "src/relay/hub/dashboard_modules/metrics-compute.js"

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _inc(cid, sev, source, state, created=T0, acked=None, resolved=None,
         synthetic=False):
    timeline = []
    if resolved is not None:
        timeline.append(TimelineEvent(
            event_id=f"res-{cid}", incident_id=cid, stream=Stream.CENTRAL,
            occurred_at=resolved, actor="op", event_type="resolved", detail={},
        ))
    return Incident(
        correlation_id=cid, account_id="111111111111", region="us-east-1",
        app_name="api", severity=sev, signal_source=source, state=state,
        alarm_name="prod-5xx", environment="prod",
        created_at=created, updated_at=(resolved or created),
        acknowledged_at=acked, acknowledged_by=("op" if acked else None),
        synthetic=synthetic, timeline=timeline,
    )


def _serialize(inc: Incident) -> dict[str, Any]:
    """Mirror the enriched /incidents serialization the client consumes."""
    resolved = _resolved_at(inc)
    return {
        "correlation_id": inc.correlation_id,
        "environment": inc.environment,
        "severity": inc.severity,
        "state": inc.state,
        "created_at": inc.created_at.isoformat() if inc.created_at else None,
        "acknowledged_at": inc.acknowledged_at.isoformat() if inc.acknowledged_at else None,
        "resolved_at": resolved.isoformat() if resolved is not None else None,
        "signal_source": inc.signal_source,
        "synthetic": inc.synthetic,
    }


def _run_js(serialized: list[dict[str, Any]]) -> dict[str, Any]:
    """Invoke computeMetrics in node on the serialized incidents."""
    script = f"""
import {{ computeMetrics }} from {json.dumps(str(_MODULE))};
let raw = "";
process.stdin.on("data", d => raw += d);
process.stdin.on("end", () => {{
  const incidents = JSON.parse(raw);
  process.stdout.write(JSON.stringify(computeMetrics(incidents)));
}});
"""
    assert _NODE is not None
    proc = subprocess.run(
        [_NODE, "--input-type=module", "-e", script],
        input=json.dumps(serialized),
        capture_output=True, text=True, check=True,
    )
    return cast(dict[str, Any], json.loads(proc.stdout))


def _fixture() -> list[Incident]:
    return [
        _inc("a", Severity.SEV1, SignalSource.CLOUDWATCH_ALARM,
             IncidentState.TRIGGERED),
        _inc("b", Severity.SEV1, SignalSource.SYNTHETIC, IncidentState.RESOLVED,
             acked=T0 + timedelta(seconds=30), resolved=T0 + timedelta(minutes=10),
             synthetic=True),
        _inc("c", Severity.SEV3, SignalSource.MANUAL, IncidentState.ACKNOWLEDGED,
             acked=T0 + timedelta(minutes=2)),
        _inc("d", Severity.SEV2, SignalSource.CLOUDWATCH_ALARM,
             IncidentState.CLOSED, acked=T0 + timedelta(seconds=45),
             resolved=T0 + timedelta(minutes=7)),
        _inc("e", Severity.SEV4, SignalSource.OTEL, IncidentState.RESOLVED,
             resolved=T0 + timedelta(minutes=3)),
    ]


def test_metrics_compute_js_matches_python():
    incidents = _fixture()
    expected = compute_metrics(incidents).as_dict()
    actual = _run_js([_serialize(i) for i in incidents])
    assert actual == expected


def test_metrics_compute_js_matches_python_empty():
    expected = compute_metrics([]).as_dict()
    actual = _run_js([])
    assert actual == expected
