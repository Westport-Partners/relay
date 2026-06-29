"""Tests for escalation timeline event emission in NodeHandler.

Verifies that the four append-only escalation events are recorded at the
correct dispatch sites, with the correct detail payloads, and that the
idempotency invariant holds: a duplicate/stale on_timeout call appends nothing.

Test matrix:
  T5.1  start()             → incident.triggered + escalation.page_sent (step 0)
  T5.2  real timeout advance → escalation.step_advanced + escalation.page_sent (step 1)
  T5.3  exhaust             → escalation.exhausted (no page_sent)
  T5.4  [regression] duplicate on_timeout (same step twice) → 0 new events
  T5.5  ack between steps   → no phantom step_advanced
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from relay.config.schema import EscalationConfig, RelayConfig, RoutingConfig
from relay.core.escalation import EscalationContext
from relay.core.model import (
    EscalationPolicy,
    EscalationStep,
    Incident,
    Severity,
    SignalSource,
    Stream,
)
from relay.node.handler import NodeHandler

# ---------------------------------------------------------------------------
# Environment variables (required by NodeHandler.__init__)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def handler_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "RELAY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
        "RELAY_HUB_EVENT_BUS_ARN": "arn:aws:events:us-east-1:123456789012:event-bus/hub",
        "RELAY_GITLAB_REPO": "12345",
        "RELAY_GITLAB_SECRET_NAME": "relay/gitlab-token",
        "RELAY_TABLE_NAME": "relay-test-table",
        "RELAY_ACCOUNT_ID": "123456789012",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "RELAY_TIMEOUT_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:relay-node",
        "RELAY_SCHEDULER_ROLE_ARN": "arn:aws:iam::123456789012:role/relay-scheduler-role",
    }.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TWO_STEP_POLICY = EscalationPolicy(
    policy_id="pol-test",
    name="Test Policy",
    team="team-test",
    steps=[
        EscalationStep(
            step_index=0,
            contact_ids=["cnt_primary"],
            timeout_minutes=5,
            notify_streams=[Stream.TEAM],
        ),
        EscalationStep(
            step_index=1,
            contact_ids=["cnt_secondary"],
            timeout_minutes=10,
            notify_streams=[Stream.TEAM],
        ),
    ],
)

_ONE_STEP_POLICY = EscalationPolicy(
    policy_id="pol-one",
    name="One-step Policy",
    team="team-test",
    steps=[
        EscalationStep(
            step_index=0,
            contact_ids=["cnt_primary"],
            timeout_minutes=5,
            notify_streams=[Stream.TEAM],
        ),
    ],
)


def _make_config(policy: EscalationPolicy = _TWO_STEP_POLICY) -> RelayConfig:
    escalation_data = {
        "policies": [
            {
                "policy_id": policy.policy_id,
                "name": policy.name,
                "team": policy.team,
                "steps": [
                    {
                        "step_index": s.step_index,
                        "contact_ids": s.contact_ids,
                        "timeout_minutes": s.timeout_minutes,
                        "notify_streams": [str(ns) for ns in s.notify_streams],
                    }
                    for s in policy.steps
                ],
            }
        ]
    }
    routing_data = {
        "rules": [],
        "default_escalation_policy_id": policy.policy_id,
        "default_streams": ["TEAM"],
    }
    return RelayConfig(
        escalation=EscalationConfig.model_validate(escalation_data),
        routing=RoutingConfig.model_validate(routing_data),
        loaded_at=datetime.now(UTC),
    )


def _make_incident(correlation_id: str = "inc-test-001") -> Incident:
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm",
    )


class _FakeConfigLoader:
    def __init__(self, config: RelayConfig) -> None:
        self._config = config

    def get(self) -> RelayConfig:
        return self._config

    def refresh(self) -> RelayConfig:
        return self._config


class _FakeAlarmSource:
    def __init__(self, incident: Incident) -> None:
        self._incident = incident

    def parse_event(self, event: dict[str, Any]) -> Incident:
        return self._incident


class _CapturingIncidentStore:
    """In-memory store that keeps the last put_incident() call for inspection."""

    def __init__(self, incident: Incident | None = None) -> None:
        self._store: dict[str, Incident] = {}
        if incident is not None:
            self._store[incident.correlation_id] = incident

    def put_incident(self, incident: Incident) -> None:
        # Keep a copy so timeline snapshots are not overwritten.
        self._store[incident.correlation_id] = incident

    def get_incident(self, correlation_id: str) -> Incident | None:
        return self._store.get(correlation_id)


class _FakeTimer:
    def schedule_timeout(self, incident_id: str, step_index: int, delay_minutes: int) -> str:
        return f"timer-{step_index}"

    def cancel_timeout(self, timer_handle: str) -> None:
        pass


class _FakeStateStore:
    def __init__(self) -> None:
        self._ctx: dict[str, EscalationContext] = {}

    def load(self, incident_id: str) -> EscalationContext | None:
        return self._ctx.get(incident_id)

    def save(self, ctx: EscalationContext) -> None:
        self._ctx[ctx.incident_id] = ctx


class FakeDispatchResult:
    team_stream_ok = True
    central_stream_ok = True


class _FakeDispatcher:
    def __init__(self, notifier: Any, transport: Any, contact_ids: list[str]) -> None:
        pass

    def dispatch(self, incident: Incident) -> FakeDispatchResult:
        return FakeDispatchResult()


def _make_handler(
    policy: EscalationPolicy = _TWO_STEP_POLICY,
    incident: Incident | None = None,
) -> tuple[NodeHandler, _CapturingIncidentStore, _FakeStateStore]:
    inc = incident or _make_incident()
    config = _make_config(policy)
    store = _CapturingIncidentStore(inc)
    state_store = _FakeStateStore()

    from relay.core.escalation import EscalationEngine

    engine = EscalationEngine(timer=_FakeTimer(), state_store=state_store)

    import relay.node.handler as handler_mod

    original_dispatcher = handler_mod.DualStreamDispatcher
    handler_mod.DualStreamDispatcher = _FakeDispatcher
    try:
        h = NodeHandler(
            _config_loader=_FakeConfigLoader(config),
            _alarm_source=_FakeAlarmSource(inc),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=store,
            _escalation_state_store=state_store,
            _escalation_engine=engine,
        )
    finally:
        handler_mod.DualStreamDispatcher = original_dispatcher

    # Keep the fake dispatcher active for the duration of each test.
    handler_mod.DualStreamDispatcher = _FakeDispatcher
    return h, store, state_store


# ---------------------------------------------------------------------------
# T5.1 — start() emits incident.triggered + escalation.page_sent (step 0)
# ---------------------------------------------------------------------------

class TestEscalationStart:
    def test_triggered_and_page_sent_emitted(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod

        handler_mod.DualStreamDispatcher = _FakeDispatcher
        h.process({})  # drives _handle_alarm via FakeAlarmSource

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None, "incident was not persisted"

        event_types = [ev.event_type for ev in persisted.timeline]
        assert "incident.triggered" in event_types, (
            f"incident.triggered missing from timeline: {event_types}"
        )
        assert "escalation.page_sent" in event_types, (
            f"escalation.page_sent missing from timeline: {event_types}"
        )

    def test_triggered_detail_fields(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher
        h.process({})

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        triggered = next(
            ev for ev in persisted.timeline if ev.event_type == "incident.triggered"
        )
        assert triggered.detail["severity"] == inc.severity
        assert triggered.detail["alarm_name"] == inc.alarm_name
        assert triggered.detail["policy_id"] == _TWO_STEP_POLICY.policy_id
        assert triggered.actor == "system"
        assert triggered.stream == Stream.TEAM

    def test_page_sent_step0_detail(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher
        h.process({})

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        page_sent = next(
            ev for ev in persisted.timeline if ev.event_type == "escalation.page_sent"
        )
        assert page_sent.detail["step_index"] == 0
        assert "cnt_primary" in page_sent.detail["contact_ids"]
        assert page_sent.detail["timeout_minutes"] == 5


# ---------------------------------------------------------------------------
# T5.2 — real timeout advance → step_advanced + page_sent (step 1)
# ---------------------------------------------------------------------------

class TestEscalationStepAdvance:
    def test_step_advanced_and_page_sent_emitted(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        # Trigger the incident first (step 0)
        h.process({})

        # Simulate step 0 timing out → advance to step 1
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        event_types = [ev.event_type for ev in persisted.timeline]
        assert "escalation.step_advanced" in event_types, (
            f"escalation.step_advanced missing: {event_types}"
        )
        # There should be two page_sent events: step 0 and step 1
        page_sent_events = [
            ev for ev in persisted.timeline if ev.event_type == "escalation.page_sent"
        ]
        assert len(page_sent_events) == 2, (
            f"Expected 2 page_sent events, got {len(page_sent_events)}"
        )

    def test_step_advanced_detail(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        h.process({})
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        advanced = next(
            ev for ev in persisted.timeline if ev.event_type == "escalation.step_advanced"
        )
        assert advanced.detail["from_step"] == 0
        assert advanced.detail["to_step"] == 1


# ---------------------------------------------------------------------------
# T5.3 — exhaust → exactly one escalation.exhausted, no page_sent for that step
# ---------------------------------------------------------------------------

class TestEscalationExhaust:
    def test_exhausted_emitted_once(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_ONE_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        # Trigger (step 0)
        h.process({})

        # Timeout on the only step → EXHAUSTED
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        event_types = [ev.event_type for ev in persisted.timeline]
        exhausted_events = [
            ev for ev in persisted.timeline if ev.event_type == "escalation.exhausted"
        ]
        assert len(exhausted_events) == 1, (
            f"Expected exactly 1 exhausted event, got {len(exhausted_events)}: {event_types}"
        )
        assert exhausted_events[0].detail["last_step_index"] == 0

    def test_no_page_sent_on_exhaust(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_ONE_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        h.process({})
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        # After exhaustion only the step-0 page_sent (from start) should exist.
        page_sent_events = [
            ev for ev in persisted.timeline if ev.event_type == "escalation.page_sent"
        ]
        assert len(page_sent_events) == 1, (
            f"Expected only step-0 page_sent, got {len(page_sent_events)}"
        )


# ---------------------------------------------------------------------------
# T5.4 [regression] — duplicate/stale on_timeout appends nothing
# ---------------------------------------------------------------------------

class TestDuplicateTimeoutIdempotency:
    def test_stale_timeout_appends_no_events(self) -> None:
        """Fire the same step_index twice. Second firing must be a no-op."""
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        # Start (step 0 paged)
        h.process({})
        persisted_after_start = store.get_incident(inc.correlation_id)
        assert persisted_after_start is not None
        count_after_start = len(persisted_after_start.timeline)

        # First timeout: real advance step 0→1
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })
        persisted_after_advance = store.get_incident(inc.correlation_id)
        assert persisted_after_advance is not None
        count_after_advance = len(persisted_after_advance.timeline)
        assert count_after_advance > count_after_start, (
            "Real advance should add timeline events"
        )

        # Second timeout for the SAME step_index=0 (stale/duplicate)
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })
        persisted_final = store.get_incident(inc.correlation_id)
        assert persisted_final is not None
        count_final = len(persisted_final.timeline)
        assert count_final == count_after_advance, (
            f"Stale timeout must not append events. "
            f"Before: {count_after_advance}, after: {count_final}. "
            f"Events: {[ev.event_type for ev in persisted_final.timeline]}"
        )

    def test_acked_then_timeout_appends_no_events(self) -> None:
        """Ack the incident, then fire a stale timeout — no new events."""
        inc = _make_incident()
        h, store, state_store = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        h.process({})
        persisted_after_start = store.get_incident(inc.correlation_id)
        assert persisted_after_start is not None
        count_before = len(persisted_after_start.timeline)

        # Acknowledge
        h.process({
            "relay_event": "ack",
            "incident_id": inc.correlation_id,
            "contact_id": "cnt_primary",
        })

        # Stale timeout after ack
        h.process({
            "relay_event": "escalation_timeout",
            "incident_id": inc.correlation_id,
            "step_index": 0,
        })

        persisted_final = store.get_incident(inc.correlation_id)
        assert persisted_final is not None
        count_final = len(persisted_final.timeline)
        # No timeline events should be appended by the stale timeout
        assert count_final == count_before, (
            f"Acked-then-timeout must not append escalation events. "
            f"Before: {count_before}, after: {count_final}. "
            f"New events: {[ev.event_type for ev in persisted_final.timeline[count_before:]]}"
        )


# ---------------------------------------------------------------------------
# T5.5 — ack between steps → no phantom step_advanced
# ---------------------------------------------------------------------------

class TestAckNoPhantomEvents:
    def test_ack_after_start_produces_no_step_advanced(self) -> None:
        inc = _make_incident()
        h, store, _ = _make_handler(policy=_TWO_STEP_POLICY, incident=inc)

        import relay.node.handler as handler_mod
        handler_mod.DualStreamDispatcher = _FakeDispatcher

        # Start (step 0 paged)
        h.process({})

        # Ack the incident before any timeout
        h.process({
            "relay_event": "ack",
            "incident_id": inc.correlation_id,
            "contact_id": "cnt_primary",
        })

        persisted = store.get_incident(inc.correlation_id)
        assert persisted is not None
        step_advanced_events = [
            ev for ev in persisted.timeline if ev.event_type == "escalation.step_advanced"
        ]
        assert len(step_advanced_events) == 0, (
            f"Ack must not produce step_advanced events: {step_advanced_events}"
        )
