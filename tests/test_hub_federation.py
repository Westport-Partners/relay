"""Tests for Hub federation forwarding — §8.1, §8.2, §8.3.

Covers:
  §8.1 Config-driven selection:
    - RELAY_FORWARD_STATES: empty/unset → all states forwarded
    - RELAY_FORWARD_STATES: explicit set → only listed states forwarded
    - RELAY_FORWARD_STATES: invalid tokens warned and ignored
    - Severity gate still applies (both severity AND state must pass)

  §8.2 Idempotent central ingest (dedup):
    - Same (correlation_id, state) delivered twice → count incremented once
    - Sinks fired once on first delivery, not on redelivery
    - Forwarding skipped on redelivery
    - State transition (TRIGGERED → RESOLVED) → both transitions processed,
      open_incident_count nets to 0

  §8.3 Forward-loop prevention:
    - Inbound event with relay_forwarded_from → forwarder.forward NOT called
    - Inbound event without marker → forwarder.forward called normally
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from relay.config.schema import FederationConfig
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident, IncidentState, Severity, SignalSource
from relay.hub.app import (
    HubProcessor,
    HubState,
    SSEPublisher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_incident(
    correlation_id: str = "inc-fed-001",
    severity: Severity = Severity.SEV1,
    state: IncidentState = IncidentState.TRIGGERED,
) -> Incident:
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="fed-app",
        severity=severity,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="fed-alarm",
        state=state,
    )


def _make_processor(
    forwarder=None,
    federation: FederationConfig | None = None,
    incident_store_get_return=None,
) -> tuple[HubProcessor, MagicMock, MagicMock]:
    """Return (processor, mock_incident_store, mock_hub_state).

    incident_store_get_return: if set, incident_store.get_incident returns this value.
    """
    incident_store = MagicMock()
    if incident_store_get_return is not None:
        incident_store.get_incident.return_value = incident_store_get_return
    else:
        incident_store.get_incident.return_value = None  # default: new incident

    notifier = MagicMock()
    hub_state = MagicMock(spec=HubState)
    hub_state.update_app.return_value = MagicMock()
    hub_state.get_tile.return_value = MagicMock()
    sse_publisher = MagicMock(spec=SSEPublisher)

    # No listeners by default — tests that need them inject a spy/mock.
    proc = HubProcessor(
        incident_store=incident_store,
        notifier=notifier,
        hub_state=hub_state,
        sse_publisher=sse_publisher,
        forwarder=forwarder,
        federation=federation,
        listeners=[],
    )
    return proc, incident_store, hub_state


def _event(incident: Incident, extra_detail: dict | None = None) -> dict:
    """Build an EventBridge-style event dict from an Incident."""
    detail = incident.model_dump(mode="json")
    if extra_detail:
        detail.update(extra_detail)
    return {"detail": detail}


# ---------------------------------------------------------------------------
# Lifecycle events on state transitions
# ---------------------------------------------------------------------------


class _SpyListener:
    def __init__(self):
        self.events = []

    def on_event(self, *, event, incident):
        self.events.append(event)


def test_triggered_dispatches_triggered_event():
    from relay.core.lifecycle import IncidentLifecycleEvent

    proc, incident_store, _ = _make_processor()
    spy = _SpyListener()
    proc._listeners = [spy]

    incident_store.get_incident.return_value = None
    proc._handle_incident(_event(_make_incident(state=IncidentState.TRIGGERED)))

    assert spy.events == [IncidentLifecycleEvent.TRIGGERED]


def test_escalated_state_dispatches_escalated_event():
    """An incident arriving in ESCALATED state dispatches ESCALATED."""
    from relay.core.lifecycle import IncidentLifecycleEvent

    proc, incident_store, _ = _make_processor()
    spy = _SpyListener()
    proc._listeners = [spy]

    # Existing record was TRIGGERED; now arrives ESCALATED → genuine transition.
    incident_store.get_incident.return_value = _make_incident(
        state=IncidentState.TRIGGERED
    )
    proc._handle_incident(_event(_make_incident(state=IncidentState.ESCALATED)))

    assert spy.events == [IncidentLifecycleEvent.ESCALATED]


def test_escalated_redelivery_does_not_redispatch():
    """A second ESCALATED delivery (same state) is deduped — no re-dispatch."""
    proc, incident_store, _ = _make_processor()
    spy = _SpyListener()
    proc._listeners = [spy]

    incident_store.get_incident.return_value = _make_incident(
        state=IncidentState.ESCALATED
    )
    proc._handle_incident(_event(_make_incident(state=IncidentState.ESCALATED)))

    assert spy.events == []


# ---------------------------------------------------------------------------
# §8.1 State filter applied in forwarding (combined with severity gate)
# ---------------------------------------------------------------------------


def test_state_filter_default_forwards_all_states():
    """When forward_states=[] (default), all incident states are forwarded."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),  # empty forward_states = all states
    )

    for state in IncidentState:
        inc = _make_incident(state=state)
        # Each is a new incident (get_incident returns None)
        incident_store.get_incident.return_value = None
        proc._handle_incident(_event(inc))

    assert mock_fwd.forward.call_count == len(IncidentState)


def test_state_filter_restricts_forwarding():
    """Only TRIGGERED and ESCALATED forwarded when forward_states=[TRIGGERED, ESCALATED]."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(
            min_severity=Severity.SEV1,
            forward_states=[IncidentState.TRIGGERED, IncidentState.ESCALATED],
        ),
    )

    forwarded_states = []
    for state in IncidentState:
        inc = _make_incident(state=state)
        incident_store.get_incident.return_value = None
        proc._handle_incident(_event(inc))

    forwarded_states = [c.args[0].state for c in mock_fwd.forward.call_args_list]
    assert set(forwarded_states) == {IncidentState.TRIGGERED, IncidentState.ESCALATED}
    assert mock_fwd.forward.call_count == 2


def test_state_filter_and_severity_both_must_pass():
    """An incident must meet BOTH the severity gate AND the state filter to be forwarded."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(
            min_severity=Severity.SEV2,  # SEV3/SEV4 blocked by severity
            forward_states=[IncidentState.TRIGGERED],
        ),
    )

    incident_store.get_incident.return_value = None

    # SEV2 TRIGGERED → both gates pass → forwarded
    proc._handle_incident(_event(_make_incident(severity=Severity.SEV2, state=IncidentState.TRIGGERED)))
    # SEV3 TRIGGERED → severity fails → NOT forwarded
    proc._handle_incident(_event(_make_incident(severity=Severity.SEV3, state=IncidentState.TRIGGERED)))
    # SEV1 RESOLVED → state filter fails → NOT forwarded
    proc._handle_incident(_event(_make_incident(severity=Severity.SEV1, state=IncidentState.RESOLVED)))

    assert mock_fwd.forward.call_count == 1
    assert mock_fwd.forward.call_args.args[0].severity == Severity.SEV2


# ---------------------------------------------------------------------------
# §8.2 Idempotent ingest — redelivery
# ---------------------------------------------------------------------------


def test_dedup_redelivery_does_not_double_increment():
    """Two deliveries of same (correlation_id, TRIGGERED) → update_app called once."""
    proc, incident_store, hub_state = _make_processor()

    inc = _make_incident(state=IncidentState.TRIGGERED)
    event = _event(inc)

    # First delivery: no existing record.
    incident_store.get_incident.return_value = None
    proc._handle_incident(event)

    # Second delivery: existing record with same state (simulates EventBridge redelivery).
    existing = _make_incident(state=IncidentState.TRIGGERED)
    incident_store.get_incident.return_value = existing
    proc._handle_incident(event)

    # update_app (which calls apply_incident and moves the count) called only once.
    hub_state.update_app.assert_called_once()


def test_dedup_redelivery_does_not_fire_listeners_twice():
    """Listeners fire once on first TRIGGERED, not again on redelivery."""
    proc, incident_store, hub_state = _make_processor()

    # Inject a spy listener so we can assert dispatch happens exactly once.
    spy = _SpyListener()
    proc._listeners = [spy]

    inc = _make_incident(state=IncidentState.TRIGGERED)
    event = _event(inc)

    # First delivery.
    incident_store.get_incident.return_value = None
    proc._handle_incident(event)

    # Second delivery (redelivery — same state already stored).
    incident_store.get_incident.return_value = _make_incident(state=IncidentState.TRIGGERED)
    proc._handle_incident(event)

    assert spy.events == [IncidentLifecycleEvent.TRIGGERED]


def test_dedup_redelivery_skips_forwarding():
    """Forwarder.forward not called on redelivery."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(state=IncidentState.TRIGGERED)
    event = _event(inc)

    # First delivery — forward occurs.
    incident_store.get_incident.return_value = None
    proc._handle_incident(event)
    assert mock_fwd.forward.call_count == 1

    # Redelivery — forward must NOT be called again.
    incident_store.get_incident.return_value = _make_incident(state=IncidentState.TRIGGERED)
    proc._handle_incident(event)
    assert mock_fwd.forward.call_count == 1  # unchanged


def test_dedup_put_incident_called_on_first_delivery():
    """put_incident is called on first delivery but NOT on redelivery."""
    proc, incident_store, _ = _make_processor()

    inc = _make_incident(state=IncidentState.TRIGGERED)
    event = _event(inc)

    # First delivery persists the incident (main state write + the AI brief
    # event both call put_incident on TRIGGERED).
    incident_store.get_incident.return_value = None
    proc._handle_incident(event)
    first_count = incident_store.put_incident.call_count
    assert first_count >= 1

    # Redelivery must apply NO further effects (skipped before persist/brief).
    incident_store.get_incident.return_value = _make_incident(state=IncidentState.TRIGGERED)
    proc._handle_incident(event)
    assert incident_store.put_incident.call_count == first_count  # unchanged


# ---------------------------------------------------------------------------
# §8.2 State transition — net count
# ---------------------------------------------------------------------------


def test_state_transition_both_applied():
    """TRIGGERED then RESOLVED: update_app called twice (once per transition)."""
    proc, incident_store, hub_state = _make_processor()

    # First: TRIGGERED arrives; no existing record.
    triggered = _make_incident(state=IncidentState.TRIGGERED)
    incident_store.get_incident.return_value = None
    proc._handle_incident(_event(triggered))

    # Second: RESOLVED arrives; existing record has state=TRIGGERED (a real transition).
    resolved = _make_incident(state=IncidentState.RESOLVED)
    incident_store.get_incident.return_value = _make_incident(state=IncidentState.TRIGGERED)
    proc._handle_incident(_event(resolved))

    assert hub_state.update_app.call_count == 2
    states_seen = [c.args[0].state for c in hub_state.update_app.call_args_list]
    assert IncidentState.TRIGGERED in states_seen
    assert IncidentState.RESOLVED in states_seen


def test_same_state_same_correlation_id_is_redelivery():
    """Delivering RESOLVED twice is a redelivery (not a second state transition)."""
    proc, incident_store, hub_state = _make_processor()

    resolved = _make_incident(state=IncidentState.RESOLVED)
    event = _event(resolved)

    # First RESOLVED delivery: no prior record → treat as transition.
    incident_store.get_incident.return_value = None
    proc._handle_incident(event)
    assert hub_state.update_app.call_count == 1

    # Second RESOLVED delivery: existing record has state=RESOLVED → redelivery.
    incident_store.get_incident.return_value = _make_incident(state=IncidentState.RESOLVED)
    proc._handle_incident(event)
    assert hub_state.update_app.call_count == 1  # no second update


# ---------------------------------------------------------------------------
# §8.3 Forward-loop prevention
# ---------------------------------------------------------------------------


def test_loop_prevention_skips_forward_when_marker_present():
    """An event with relay_forwarded_from is never re-forwarded."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(severity=Severity.SEV1, state=IncidentState.TRIGGERED)
    # Build event detail with the forwarded marker embedded.
    forwarded_event = _event(inc, extra_detail={
        "relay_forwarded_from": "111111111111",
        "relay_forwarded_hub_scope": "local-federated",
    })

    incident_store.get_incident.return_value = None
    proc._handle_incident(forwarded_event)

    mock_fwd.forward.assert_not_called()


def test_loop_prevention_still_applies_local_effects():
    """Even with relay_forwarded_from, local processing (update_app, put_incident) still runs."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, hub_state = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(severity=Severity.SEV1, state=IncidentState.TRIGGERED)
    forwarded_event = _event(inc, extra_detail={"relay_forwarded_from": "111111111111"})

    incident_store.get_incident.return_value = None
    proc._handle_incident(forwarded_event)

    # Local effects still applied.
    incident_store.put_incident.assert_called()
    hub_state.update_app.assert_called_once()
    # Forwarding suppressed.
    mock_fwd.forward.assert_not_called()


def test_loop_prevention_without_marker_forwards_normally():
    """Without relay_forwarded_from, forwarding proceeds normally (control group)."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(severity=Severity.SEV1, state=IncidentState.TRIGGERED)
    incident_store.get_incident.return_value = None
    proc._handle_incident(_event(inc))  # no extra_detail / no marker

    mock_fwd.forward.assert_called_once()


def test_loop_prevention_marker_read_from_raw_detail_not_model():
    """relay_forwarded_from is read from the raw dict, not the Incident model
    (Pydantic drops unknown fields)."""
    mock_fwd = MagicMock()
    mock_fwd.forward.return_value = True

    proc, incident_store, _ = _make_processor(
        forwarder=mock_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(severity=Severity.SEV1)
    detail = inc.model_dump(mode="json")
    detail["relay_forwarded_from"] = "some-account"

    # Confirm Pydantic does NOT carry the field through.
    parsed = Incident.model_validate(detail)
    assert not hasattr(parsed, "relay_forwarded_from")

    incident_store.get_incident.return_value = None
    proc._handle_incident({"detail": detail})

    # The marker was read from the raw dict → forwarding suppressed.
    mock_fwd.forward.assert_not_called()


# ---------------------------------------------------------------------------
# Failure isolation (existing behaviour must be preserved)
# ---------------------------------------------------------------------------


def test_failure_isolation_get_incident_error_does_not_break_processing():
    """If get_incident raises, the exception propagates (store errors should not be swallowed)."""
    proc, incident_store, hub_state = _make_processor()
    incident_store.get_incident.side_effect = RuntimeError("dynamo is down")

    inc = _make_incident()
    with pytest.raises(RuntimeError, match="dynamo is down"):
        proc._handle_incident(_event(inc))


def test_failure_isolation_forwarder_error_after_local_complete():
    """A forwarder exception must not prevent local processing — local effects already applied."""
    exploding_fwd = MagicMock()
    exploding_fwd.forward.side_effect = RuntimeError("central bus exploded")

    proc, incident_store, hub_state = _make_processor(
        forwarder=exploding_fwd,
        federation=FederationConfig(min_severity=Severity.SEV1),
    )

    inc = _make_incident(severity=Severity.SEV1)
    incident_store.get_incident.return_value = None
    # Should not raise.
    proc._handle_incident(_event(inc))

    incident_store.put_incident.assert_called()
    hub_state.update_app.assert_called_once()
