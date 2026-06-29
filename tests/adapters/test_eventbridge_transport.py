"""Unit tests for EventBridgeTransport.emit_heartbeat using an injected fake session."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from relay.adapters.aws.eventbridge_transport import EventBridgeTransport

_HUB_BUS_ARN = "arn:aws:events:us-east-1:123456789012:event-bus/relay-hub"


def _transport_with_fake_eb():
    """Return (transport, fake_eb_client) with injected session."""
    session = MagicMock()
    eb = session.client.return_value
    t = EventBridgeTransport(
        hub_event_bus_arn=_HUB_BUS_ARN,
        boto3_session=session,
    )
    return t, eb


class TestEmitHeartbeat:

    def test_emit_heartbeat_puts_correct_event(self):
        """emit_heartbeat sends the right EventBridge entry with all fields."""
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        t.emit_heartbeat(
            account_id="123456789012",
            app_name="billing-api",
            timestamp="2026-06-21T00:00:00+00:00",
            environment="prod",
            deployment_id="billing-api-prod",
            service_path=["pl", "prod", "comp", "billing-api-prod"],
        )

        eb.put_events.assert_called_once()
        call_kwargs = eb.put_events.call_args.kwargs
        entries = call_kwargs["Entries"]
        assert len(entries) == 1
        entry = entries[0]

        assert entry["DetailType"] == "relay.heartbeat"
        assert entry["Source"] == "relay.node"
        assert entry["EventBusName"] == _HUB_BUS_ARN

        detail = json.loads(entry["Detail"])
        assert detail["relay_event"] == "heartbeat"
        assert detail["account_id"] == "123456789012"
        assert detail["app_name"] == "billing-api"
        assert detail["environment"] == "prod"
        assert detail["deployment_id"] == "billing-api-prod"
        assert detail["service_path"] == ["pl", "prod", "comp", "billing-api-prod"]
        assert detail["timestamp"] == "2026-06-21T00:00:00+00:00"

    def test_emit_heartbeat_omits_optional_when_none(self):
        """emit_heartbeat omits deployment_id and service_path when not provided."""
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        t.emit_heartbeat(
            account_id="123456789012",
            app_name="billing-api",
            timestamp="2026-06-21T00:00:00+00:00",
        )

        call_kwargs = eb.put_events.call_args.kwargs
        detail = json.loads(call_kwargs["Entries"][0]["Detail"])

        # Required fields present
        assert "relay_event" in detail
        assert "account_id" in detail
        assert "app_name" in detail
        assert "environment" in detail

        # Optional fields absent
        assert "deployment_id" not in detail
        assert "service_path" not in detail

    def test_emit_heartbeat_swallows_client_error(self):
        """emit_heartbeat does not raise when boto3 raises ClientError."""
        t, eb = _transport_with_fake_eb()
        eb.put_events.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "m"}}, "PutEvents"
        )

        # Must not raise
        result = t.emit_heartbeat(
            account_id="123456789012",
            app_name="billing-api",
            timestamp="2026-06-21T00:00:00+00:00",
        )
        assert result is None

    def test_emit_heartbeat_swallows_failed_entry(self):
        """emit_heartbeat does not raise when FailedEntryCount > 0."""
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "X"}],
        }

        # Must not raise
        result = t.emit_heartbeat(
            account_id="123456789012",
            app_name="billing-api",
            timestamp="2026-06-21T00:00:00+00:00",
        )
        assert result is None

    def test_emit_heartbeat_includes_org_path(self):
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        org_path = [
            {"id": "pl", "name": "PL", "level": "product_line", "parent": None},
            {"id": "dep", "name": "dep", "level": "deployment", "parent": "pl"},
        ]
        t.emit_heartbeat(
            account_id="123456789012",
            app_name="svc",
            timestamp="2026-06-21T00:00:00+00:00",
            org_path=org_path,
        )

        call_kwargs = eb.put_events.call_args.kwargs
        detail = json.loads(call_kwargs["Entries"][0]["Detail"])
        assert detail["org_path"] == org_path

    def test_emit_heartbeat_omits_org_path_when_none(self):
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        t.emit_heartbeat(
            account_id="123456789012",
            app_name="svc",
            timestamp="2026-06-21T00:00:00+00:00",
        )

        call_kwargs = eb.put_events.call_args.kwargs
        detail = json.loads(call_kwargs["Entries"][0]["Detail"])
        assert "org_path" not in detail

    def test_emit_heartbeat_omits_org_path_when_empty(self):
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        t.emit_heartbeat(
            account_id="123456789012",
            app_name="svc",
            timestamp="2026-06-21T00:00:00+00:00",
            org_path=[],
        )

        call_kwargs = eb.put_events.call_args.kwargs
        detail = json.loads(call_kwargs["Entries"][0]["Detail"])
        assert "org_path" not in detail

    def test_emit_heartbeat_includes_metadata_and_oncall(self):
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        meta = {"owner": "team-x", "aws_tags": {"env": "prod"}}
        oncall = {"source": "team_snapshot", "roles": {"primary": {"name": "Al"}}}
        t.emit_heartbeat(
            account_id="123456789012",
            app_name="svc",
            timestamp="2026-06-21T00:00:00+00:00",
            metadata=meta,
            on_call=oncall,
        )

        detail = json.loads(eb.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["metadata"] == meta
        assert detail["on_call"] == oncall

    def test_emit_heartbeat_omits_metadata_and_oncall_when_absent(self):
        # An un-enriched Node (no metadata/on_call) stays byte-compatible.
        t, eb = _transport_with_fake_eb()
        eb.put_events.return_value = {"FailedEntryCount": 0, "Entries": [{"EventId": "abc"}]}

        t.emit_heartbeat(
            account_id="123456789012",
            app_name="svc",
            timestamp="2026-06-21T00:00:00+00:00",
        )

        detail = json.loads(eb.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert "metadata" not in detail
        assert "on_call" not in detail
