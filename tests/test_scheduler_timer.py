"""Unit tests for SchedulerTimerPort (EventBridge Scheduler adapter).

Uses a simple fake boto3 scheduler client instead of moto so that tests
remain fast and dependency-free.  The fake captures all calls and raises
ClientError on demand.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from relay.adapters.aws.scheduler_timer import SchedulerTimerPort, _sanitize_name

# ---------------------------------------------------------------------------
# Fake boto3 scheduler client
# ---------------------------------------------------------------------------


def _make_client(
    *,
    raise_on_delete: str | None = None,
) -> MagicMock:
    """Return a MagicMock scheduler client.

    Args:
        raise_on_delete: If set to an error code string (e.g.
            ``"ResourceNotFoundException"``), ``delete_schedule`` will raise a
            matching ``ClientError``.
    """
    client = MagicMock()

    if raise_on_delete:
        error_response = {
            "Error": {
                "Code": raise_on_delete,
                "Message": f"Simulated {raise_on_delete}",
            }
        }
        client.delete_schedule.side_effect = ClientError(
            error_response, "DeleteSchedule"
        )

    return client


# Fixed clock — always returns 2026-01-15T10:00:00 UTC.
_FIXED_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
_FIXED_CLOCK = lambda: _FIXED_NOW  # noqa: E731


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------


class TestSanitizeName:
    def test_allows_safe_chars(self):
        assert _sanitize_name("relay-esc-inc001-0") == "relay-esc-inc001-0"

    def test_replaces_colon(self):
        assert _sanitize_name("inc:001") == "inc_001"

    def test_replaces_slash(self):
        assert _sanitize_name("inc/abc") == "inc_abc"

    def test_replaces_space(self):
        assert _sanitize_name("inc 001") == "inc_001"

    def test_truncates_to_64_chars(self):
        long_name = "a" * 100
        result = _sanitize_name(long_name)
        assert len(result) == 64

    def test_allows_periods(self):
        assert _sanitize_name("relay.esc.test") == "relay.esc.test"


# ---------------------------------------------------------------------------
# SchedulerTimerPort.schedule_timeout
# ---------------------------------------------------------------------------


class TestScheduleTimeout:
    """Tests for SchedulerTimerPort.schedule_timeout."""

    def _make_port(self, client: MagicMock) -> SchedulerTimerPort:
        return SchedulerTimerPort(
            target_lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:relay-test-node",
            scheduler_role_arn="arn:aws:iam::123456789012:role/relay-test-scheduler-invoke",
            scheduler_client=client,
            group_name="relay-test-escalation",
            clock=_FIXED_CLOCK,
        )

    def test_returns_deterministic_schedule_name(self):
        client = _make_client()
        port = self._make_port(client)

        handle = port.schedule_timeout("inc-abc-123", 0, 15)

        assert handle == "relay-esc-inc-abc-123-0"

    def test_name_sanitizes_special_chars(self):
        client = _make_client()
        port = self._make_port(client)

        handle = port.schedule_timeout("inc:special/chars", 1, 5)

        # colons and slashes must be replaced
        assert ":" not in handle
        assert "/" not in handle

    def test_schedule_expression_uses_fixed_clock_plus_delay(self):
        """at() expression must be clock + delay_minutes, truncated to the minute."""
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 15)

        call_kwargs = client.create_schedule.call_args.kwargs
        # _FIXED_NOW is 10:00:00; + 15 min = 10:15:00
        assert call_kwargs["ScheduleExpression"] == "at(2026-01-15T10:15:00)"

    def test_schedule_expression_crosses_hour_boundary(self):
        """Verify timedelta carries over correctly across an hour boundary."""
        client = _make_client()
        port = SchedulerTimerPort(
            target_lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:x",
            scheduler_role_arn="arn:aws:iam::123456789012:role/y",
            scheduler_client=client,
            clock=lambda: datetime(2026, 1, 15, 10, 55, 0, tzinfo=UTC),
        )
        port.schedule_timeout("inc-002", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        # 10:55 + 10 min = 11:05:00
        assert call_kwargs["ScheduleExpression"] == "at(2026-01-15T11:05:00)"

    def test_action_after_completion_is_delete(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["ActionAfterCompletion"] == "DELETE"

    def test_flexible_time_window_is_off(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_target_lambda_arn_in_call(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["Target"]["Arn"] == (
            "arn:aws:lambda:us-east-1:123456789012:function:relay-test-node"
        )

    def test_target_role_arn_in_call(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["Target"]["RoleArn"] == (
            "arn:aws:iam::123456789012:role/relay-test-scheduler-invoke"
        )

    def test_input_payload_contains_relay_event_and_ids(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-007", 2, 5)

        call_kwargs = client.create_schedule.call_args.kwargs
        payload = json.loads(call_kwargs["Target"]["Input"])
        assert payload["relay_event"] == "escalation_timeout"
        assert payload["incident_id"] == "inc-007"
        assert payload["step_index"] == 2

    def test_group_name_passed_to_api(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["GroupName"] == "relay-test-escalation"

    def test_schedule_expression_timezone_is_utc(self):
        client = _make_client()
        port = self._make_port(client)

        port.schedule_timeout("inc-001", 0, 10)

        call_kwargs = client.create_schedule.call_args.kwargs
        assert call_kwargs["ScheduleExpressionTimezone"] == "UTC"

    def test_client_error_is_reraised(self):
        client = _make_client()
        client.create_schedule.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "too fast"}},
            "CreateSchedule",
        )
        port = self._make_port(client)

        with pytest.raises(ClientError):
            port.schedule_timeout("inc-001", 0, 5)


# ---------------------------------------------------------------------------
# SchedulerTimerPort.cancel_timeout
# ---------------------------------------------------------------------------


class TestCancelTimeout:
    """Tests for SchedulerTimerPort.cancel_timeout."""

    def _make_port(self, client: MagicMock) -> SchedulerTimerPort:
        return SchedulerTimerPort(
            target_lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:relay-test-node",
            scheduler_role_arn="arn:aws:iam::123456789012:role/relay-test-scheduler-invoke",
            scheduler_client=client,
            group_name="relay-test-escalation",
        )

    def test_calls_delete_schedule_with_name_and_group(self):
        client = _make_client()
        port = self._make_port(client)

        port.cancel_timeout("relay-esc-inc-001-0")

        client.delete_schedule.assert_called_once_with(
            Name="relay-esc-inc-001-0",
            GroupName="relay-test-escalation",
        )

    def test_resource_not_found_is_swallowed(self):
        client = _make_client(raise_on_delete="ResourceNotFoundException")
        port = self._make_port(client)

        # Must not raise — schedule already fired/deleted is a valid no-op.
        port.cancel_timeout("relay-esc-inc-001-0")

    def test_other_client_error_is_reraised(self):
        client = _make_client(raise_on_delete="AccessDeniedException")
        port = self._make_port(client)

        with pytest.raises(ClientError):
            port.cancel_timeout("relay-esc-inc-001-0")

    def test_empty_handle_skips_api_call(self):
        """An empty handle (from _NoOpTimerPort or unset context) must be a no-op."""
        client = _make_client()
        port = self._make_port(client)

        port.cancel_timeout("")

        client.delete_schedule.assert_not_called()
