"""Shared pytest fixtures for the relay test suite."""

from datetime import UTC, datetime

import pytest

from relay.core.model import Incident, Severity, SignalSource


@pytest.fixture
def incident() -> Incident:
    """Return a minimal valid Incident with fixed values for use in tests."""
    now = datetime.now(UTC)
    return Incident(
        correlation_id="inc-fixture-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-high-error-rate",
        created_at=now,
        updated_at=now,
    )
