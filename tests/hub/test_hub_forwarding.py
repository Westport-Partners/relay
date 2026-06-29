"""Tests for Hub forwarding topology (scope + Forwarder seam).

Covers:
  1. HubScope parsing from env (default, explicit values, invalid fallback)
  2. EventBridgeForwarder.forward() — success path (fake boto3 client)
  3. EventBridgeForwarder.forward() — relay_forwarded_from marker present
  4. EventBridgeForwarder.forward() — boto3 ClientError returns False
  5. EventBridgeForwarder.forward() — FailedEntryCount > 0 returns False
  6. Severity threshold: below threshold → not forwarded; at/above → forwarded
  7. Failure isolation: forwarder exception does NOT prevent local processing
  8. NoOpForwarder always returns False
  9. NoOpForwarder used when scope=local
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from relay.adapters.aws.eventbridge_forwarder import EventBridgeForwarder, NoOpForwarder
from relay.config.schema import FederationConfig
from relay.core.model import Incident, IncidentState, Severity, SignalSource
from relay.hub.app import (
    HubProcessor,
    HubScope,
    HubState,
    SSEPublisher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_incident(severity: Severity = Severity.SEV2) -> Incident:
    return Incident(
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=severity,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm",
        state=IncidentState.TRIGGERED,
    )


def _fake_eb_client(failed_count: int = 0, raise_exc: Exception | None = None) -> MagicMock:
    """Build a fake boto3 events client."""
    client = MagicMock()
    if raise_exc is not None:
        client.put_events.side_effect = raise_exc
    else:
        entries = []
        if failed_count == 0:
            entries = [{"EventId": "evt-123"}]
        else:
            entries = [{"ErrorCode": "ThrottlingException", "ErrorMessage": "throttled"}]
        client.put_events.return_value = {
            "FailedEntryCount": failed_count,
            "Entries": entries,
        }
    return client


def _make_processor(
    forwarder=None,
    federation: FederationConfig | None = None,
) -> tuple[HubProcessor, MagicMock, MagicMock]:
    """Return (processor, mock_incident_store, mock_hub_state)."""
    incident_store = MagicMock()
    notifier = MagicMock()
    hub_state = MagicMock(spec=HubState)
    hub_state.update_app.return_value = MagicMock()
    sse_publisher = MagicMock(spec=SSEPublisher)

    # No listeners — these tests exercise forwarding/dedup, not adapter sinks.
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


# ---------------------------------------------------------------------------
# 1. HubScope parsing
# ---------------------------------------------------------------------------


def test_hubscope_default_is_local(monkeypatch):
    monkeypatch.delenv("RELAY_HUB_SCOPE", raising=False)
    assert HubScope.from_env() == HubScope.LOCAL


def test_hubscope_explicit_local(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "local")
    assert HubScope.from_env() == HubScope.LOCAL


def test_hubscope_explicit_local_federated(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "local-federated")
    assert HubScope.from_env() == HubScope.LOCAL_FEDERATED


def test_hubscope_explicit_central(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "central")
    assert HubScope.from_env() == HubScope.CENTRAL


def test_hubscope_case_insensitive(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "LOCAL-FEDERATED")
    assert HubScope.from_env() == HubScope.LOCAL_FEDERATED


def test_hubscope_invalid_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "nonsense")
    result = HubScope.from_env()
    assert result == HubScope.LOCAL


# ---------------------------------------------------------------------------
# 2 & 3. EventBridgeForwarder — success + relay_forwarded_from marker
# ---------------------------------------------------------------------------


def test_eventbridge_forwarder_success():
    eb = _fake_eb_client(failed_count=0)
    forwarder = EventBridgeForwarder(
        central_bus_arn="arn:aws:events:us-east-1:999:event-bus/central",
        source_account_id="123456789012",
        hub_scope="local-federated",
        boto3_session=MagicMock(client=MagicMock(return_value=eb)),
    )
    incident = _make_incident(Severity.SEV1)
    result = forwarder.forward(incident)
    assert result is True
    eb.put_events.assert_called_once()


def test_eventbridge_forwarder_includes_relay_forwarded_from():
    eb = _fake_eb_client(failed_count=0)
    forwarder = EventBridgeForwarder(
        central_bus_arn="arn:aws:events:us-east-1:999:event-bus/central",
        source_account_id="123456789012",
        hub_scope="local-federated",
        boto3_session=MagicMock(client=MagicMock(return_value=eb)),
    )
    incident = _make_incident(Severity.SEV1)
    forwarder.forward(incident)

    call_args = eb.put_events.call_args
    entry = call_args.kwargs["Entries"][0]
    detail = json.loads(entry["Detail"])
    assert detail["relay_forwarded_from"] == "123456789012"
    assert detail["relay_forwarded_hub_scope"] == "local-federated"


def test_eventbridge_forwarder_publishes_to_central_bus():
    eb = _fake_eb_client(failed_count=0)
    central_arn = "arn:aws:events:us-east-1:999:event-bus/central"
    forwarder = EventBridgeForwarder(
        central_bus_arn=central_arn,
        source_account_id="123456789012",
        boto3_session=MagicMock(client=MagicMock(return_value=eb)),
    )
    forwarder.forward(_make_incident())

    entry = eb.put_events.call_args.kwargs["Entries"][0]
    assert entry["EventBusName"] == central_arn


# ---------------------------------------------------------------------------
# 4. EventBridgeForwarder — ClientError returns False
# ---------------------------------------------------------------------------


def test_eventbridge_forwarder_client_error_returns_false():
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
        "PutEvents",
    )
    eb = _fake_eb_client(raise_exc=error)
    forwarder = EventBridgeForwarder(
        central_bus_arn="arn:aws:events:us-east-1:999:event-bus/central",
        boto3_session=MagicMock(client=MagicMock(return_value=eb)),
    )
    result = forwarder.forward(_make_incident())
    assert result is False


# ---------------------------------------------------------------------------
# 5. EventBridgeForwarder — FailedEntryCount > 0 returns False
# ---------------------------------------------------------------------------


def test_eventbridge_forwarder_failed_entry_returns_false():
    eb = _fake_eb_client(failed_count=1)
    forwarder = EventBridgeForwarder(
        central_bus_arn="arn:aws:events:us-east-1:999:event-bus/central",
        boto3_session=MagicMock(client=MagicMock(return_value=eb)),
    )
    result = forwarder.forward(_make_incident())
    assert result is False


# ---------------------------------------------------------------------------
# 6. Severity threshold
# ---------------------------------------------------------------------------


def test_processor_only_forwards_at_or_above_threshold():
    mock_forwarder = MagicMock()
    mock_forwarder.forward.return_value = True

    proc, incident_store, hub_state = _make_processor(
        forwarder=mock_forwarder,
        federation=FederationConfig(min_severity=Severity.SEV2),
    )

    # SEV1 and SEV2 should be forwarded
    for sev in (Severity.SEV1, Severity.SEV2):
        inc = _make_incident(sev)
        proc._handle_incident({"detail": inc.model_dump(mode="json")})

    # SEV3 and SEV4 should NOT be forwarded
    for sev in (Severity.SEV3, Severity.SEV4):
        inc = _make_incident(sev)
        proc._handle_incident({"detail": inc.model_dump(mode="json")})

    assert mock_forwarder.forward.call_count == 2
    forwarded_severities = [
        call.args[0].severity for call in mock_forwarder.forward.call_args_list
    ]
    assert Severity.SEV1 in forwarded_severities
    assert Severity.SEV2 in forwarded_severities


# ---------------------------------------------------------------------------
# 7. Failure isolation
# ---------------------------------------------------------------------------


def test_forwarder_exception_does_not_break_local_processing():
    """A forwarder that raises must not prevent incident_store.put_incident or hub_state.update_app."""
    exploding_forwarder = MagicMock()
    exploding_forwarder.forward.side_effect = RuntimeError("central bus exploded")

    proc, incident_store, hub_state = _make_processor(
        forwarder=exploding_forwarder,
        federation=FederationConfig(min_severity=Severity.SEV1),  # ensure SEV1 is attempted
    )

    inc = _make_incident(Severity.SEV1)
    # Should not raise
    proc._handle_incident({"detail": inc.model_dump(mode="json")})

    # Local processing must have happened
    incident_store.put_incident.assert_called()
    hub_state.update_app.assert_called_once()


# ---------------------------------------------------------------------------
# 8. NoOpForwarder
# ---------------------------------------------------------------------------


def test_noop_forwarder_returns_false():
    noop = NoOpForwarder()
    result = noop.forward(_make_incident())
    assert result is False


def test_noop_forwarder_never_calls_aws():
    noop = NoOpForwarder()
    # Just ensure no boto3 call is made (no client set up, so any AWS call would error)
    incident = _make_incident()
    result = noop.forward(incident)
    assert result is False


# ---------------------------------------------------------------------------
# 9. NoOpForwarder used when scope=local
# ---------------------------------------------------------------------------


def test_processor_uses_noop_forwarder_by_default():
    """HubProcessor with no forwarder arg defaults to NoOpForwarder."""
    proc, _, _ = _make_processor(forwarder=None)
    assert isinstance(proc._forwarder, NoOpForwarder)


def test_processor_noop_forwarder_does_not_forward_anything():
    proc, incident_store, hub_state = _make_processor(forwarder=None)

    inc = _make_incident(Severity.SEV1)
    proc._handle_incident({"detail": inc.model_dump(mode="json")})

    # Local processing still happened
    incident_store.put_incident.assert_called()
    hub_state.update_app.assert_called_once()
