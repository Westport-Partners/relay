"""Tests for DualStreamDispatcher — verifies parallel fan-out and failure isolation."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from relay.core.dispatcher import DualStreamDispatcher
from relay.core.model import Incident, Severity, SignalSource, Stream

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeNotifier:
    calls: list = field(default_factory=list)
    should_fail: bool = False

    def send(self, *, incident, contact_ids, stream):
        if self.should_fail:
            raise RuntimeError("Notifier failure (simulated)")
        self.calls.append(
            {"incident_id": incident.correlation_id, "stream": stream, "contacts": contact_ids}
        )


@dataclass
class FakeTransport:
    calls: list = field(default_factory=list)
    should_fail: bool = False

    def emit(self, *, incident, stream):
        if self.should_fail:
            raise RuntimeError("Transport failure (simulated)")
        self.calls.append({"incident_id": incident.correlation_id, "stream": stream})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_incident() -> Incident:
    """Build a minimal valid Incident with fixed values for testing."""
    now = datetime.now(UTC)
    return Incident(
        correlation_id="inc-test-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dispatch_both_streams_success():
    notifier = FakeNotifier()
    transport = FakeTransport()
    incident = make_incident()

    dispatcher = DualStreamDispatcher(
        notifier=notifier,
        transport=transport,
        contact_ids=["c1", "c2"],
    )
    result = dispatcher.dispatch(incident)

    assert result.team_stream_ok is True
    assert result.central_stream_ok is True
    assert result.fully_successful is True
    assert len(notifier.calls) == 1
    assert len(transport.calls) == 1
    assert notifier.calls[0]["stream"] == Stream.TEAM
    assert transport.calls[0]["stream"] == Stream.CENTRAL


def test_dispatch_team_stream_failure_does_not_block_central():
    notifier = FakeNotifier(should_fail=True)
    transport = FakeTransport()
    incident = make_incident()

    dispatcher = DualStreamDispatcher(
        notifier=notifier,
        transport=transport,
        contact_ids=["c1"],
    )
    result = dispatcher.dispatch(incident)

    assert result.team_stream_ok is False
    assert result.central_stream_ok is True
    assert result.any_successful is True
    assert result.team_stream_error is not None
    assert "simulated" in result.team_stream_error
    # Central was still called despite the team failure.
    assert len(transport.calls) == 1


def test_dispatch_central_stream_failure_does_not_block_team():
    notifier = FakeNotifier()
    transport = FakeTransport(should_fail=True)
    incident = make_incident()

    dispatcher = DualStreamDispatcher(
        notifier=notifier,
        transport=transport,
        contact_ids=["c1"],
    )
    result = dispatcher.dispatch(incident)

    assert result.team_stream_ok is True
    assert result.central_stream_ok is False
    # Team was still called despite the central failure.
    assert len(notifier.calls) == 1


def test_dispatch_both_streams_fail():
    notifier = FakeNotifier(should_fail=True)
    transport = FakeTransport(should_fail=True)
    incident = make_incident()

    dispatcher = DualStreamDispatcher(
        notifier=notifier,
        transport=transport,
        contact_ids=["c1"],
    )
    result = dispatcher.dispatch(incident)

    assert result.fully_successful is False
    assert result.any_successful is False
    assert result.team_stream_error is not None
    assert result.central_stream_error is not None


def test_dispatch_invokes_both_streams():
    """Both streams must be invoked even when each has a non-trivial delay.

    The dispatcher is sequential (no ThreadPoolExecutor), so the correctness
    guarantee is: team stream runs first, central stream runs second, and both
    complete successfully regardless of their individual duration.
    """

    class SlowNotifier:
        def __init__(self) -> None:
            self.called: bool = False

        def send(self, *, incident, contact_ids, stream):
            time.sleep(0.05)
            self.called = True

    class SlowTransport:
        def __init__(self) -> None:
            self.called: bool = False

        def emit(self, *, incident, stream):
            time.sleep(0.05)
            self.called = True

    notifier = SlowNotifier()
    transport = SlowTransport()
    incident = make_incident()
    dispatcher = DualStreamDispatcher(
        notifier=notifier,
        transport=transport,
        contact_ids=["c1"],
    )

    result = dispatcher.dispatch(incident)

    assert result.fully_successful is True
    assert notifier.called is True, "team stream (notifier) was not invoked"
    assert transport.called is True, "central stream (transport) was not invoked"
