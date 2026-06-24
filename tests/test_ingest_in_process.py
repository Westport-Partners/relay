"""Tests for the in-process Node→Hub pipeline collapse (Step 1).

Covers:
  1. HubProcessor.on_local_incident — tile effects + lifecycle dispatch
  2. DetectionPipeline.handle_alarm — on_incident sink invoked once
  3. POST /ingest/alarm — 403 when runtime=fargate; routes to pipeline when allowed
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
)

if TYPE_CHECKING:
    from relay.hub.health import FleetTile

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.hub.app import HubApp, HubProcessor, HubState, SSEPublisher  # noqa: E402
from relay.node.pipeline import DetectionPipeline  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_incident(
    correlation_id: str = "inc-pipeline-001",
    state: IncidentState = IncidentState.TRIGGERED,
) -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm-high-error",
        state=state,
        environment="prod",
        deployment_id="dep-test-prod",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_tile(account_id: str = "123456789012", app_name: str = "test-app") -> FleetTile:
    from relay.hub.health import FleetTile, Liveness
    return FleetTile(
        account_id=account_id,
        app_name=app_name,
        status="red",
        liveness=Liveness.LIVE,
        open_incidents=1,
        worst_severity=Severity.SEV2,
        last_heartbeat_at=None,
        registered_at=datetime.now(UTC),
    )


class FakeFleetStore:
    """Minimal FleetStore fake that satisfies HubState's interface."""

    def hydrate(self):
        return []

    def put_tile(self, tile):
        pass

    def apply_incident(self, incident):
        return _make_tile(incident.account_id, incident.app_name)

    def record_heartbeat(self, *args, **kwargs):
        account_id = args[0] if args else ""
        app_name = args[1] if len(args) > 1 else ""
        return _make_tile(account_id, app_name)


class FakeIncidentStore:
    """In-memory incident store."""

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}

    def put_incident(self, incident: Incident) -> None:
        self._incidents[incident.correlation_id] = incident

    def get_incident(self, correlation_id: str) -> Incident | None:
        return self._incidents.get(correlation_id)

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        return list(self._incidents.values())


class FakeSSEPublisher:
    """Captures publish_delta calls."""

    def __init__(self) -> None:
        self.deltas: list = []

    def publish_delta(self, tile) -> None:
        self.deltas.append(tile)

    def publish_ping(self) -> None:
        pass


class FakeNoOpForwarder:
    def forward(self, incident: Incident) -> bool:
        return False


# ---------------------------------------------------------------------------
# HubState factory
# ---------------------------------------------------------------------------


def _make_hub_state() -> HubState:
    hs = HubState.__new__(HubState)
    hs._store = FakeFleetStore()
    hs._tiles = {}
    hs._org_paths = {}
    hs._org_tree = None
    hs.lock = threading.Lock()
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    return hs


# ---------------------------------------------------------------------------
# Test 1 — HubProcessor.on_local_incident applies tile + dispatches lifecycle
# ---------------------------------------------------------------------------


def test_on_local_incident_updates_tile_and_dispatches_lifecycle():
    """on_local_incident must update the tile and call lifecycle listeners."""
    incident = _make_incident()
    incident_store = FakeIncidentStore()
    sse = FakeSSEPublisher()
    hub_state = _make_hub_state()

    lifecycle_calls: list[tuple] = []

    class CapturingListener:
        def on_event(self, *, event, incident):
            lifecycle_calls.append((event, incident.correlation_id))

    processor = HubProcessor(
        incident_store=incident_store,
        notifier=MagicMock(),
        hub_state=hub_state,
        sse_publisher=sse,
        forwarder=FakeNoOpForwarder(),
        listeners=[CapturingListener()],
    )

    processor.on_local_incident(incident)

    # Tile delta must have been published.
    assert len(sse.deltas) >= 1, "SSE publish_delta was not called"

    # Incident must have been persisted.
    assert incident_store.get_incident(incident.correlation_id) is not None

    # Lifecycle TRIGGERED event must have been dispatched.
    from relay.core.lifecycle import IncidentLifecycleEvent
    assert any(
        ev == IncidentLifecycleEvent.TRIGGERED for ev, _ in lifecycle_calls
    ), f"TRIGGERED lifecycle event not dispatched; got {lifecycle_calls}"


def test_on_local_incident_does_not_recheck_row_existence():
    """on_local_incident must NOT guard on row-already-present.

    The Node just wrote the row, so a naive dedup guard would wrongly treat
    every in-process delivery as a redelivery and swallow the tile update.
    """
    incident = _make_incident()
    incident_store = FakeIncidentStore()
    # Pre-populate the row to simulate that the Node already wrote it.
    incident_store.put_incident(incident)

    sse = FakeSSEPublisher()
    hub_state = _make_hub_state()

    lifecycle_calls: list = []

    class CapturingListener:
        def on_event(self, *, event, incident):
            lifecycle_calls.append(event)

    processor = HubProcessor(
        incident_store=incident_store,
        notifier=MagicMock(),
        hub_state=hub_state,
        sse_publisher=sse,
        forwarder=FakeNoOpForwarder(),
        listeners=[CapturingListener()],
    )

    processor.on_local_incident(incident)

    # Tile delta must still be published even though the row was pre-populated.
    assert len(sse.deltas) >= 1, "SSE publish_delta must fire even when row pre-exists"
    # Lifecycle event must fire.
    from relay.core.lifecycle import IncidentLifecycleEvent
    assert IncidentLifecycleEvent.TRIGGERED in lifecycle_calls


# ---------------------------------------------------------------------------
# Test 2 — DetectionPipeline.handle_alarm triggers the on_incident sink
# ---------------------------------------------------------------------------


def test_detection_pipeline_calls_on_incident_sink():
    """DetectionPipeline.handle_alarm must invoke the on_incident sink once."""
    incident = _make_incident()

    class FakeAlarmSource:
        def parse_event(self, event):
            return incident

        def bind_config(self, **kwargs):
            pass

    class FakeEscalationEngine:
        class _T:
            old_phase = "IDLE"
            new_phase = "NOTIFIED"
            contact_ids_to_page: list = []
            note = "ok"

        def start(self, incident, policy):
            return self._T()

    class FakeDispatchResult:
        team_stream_ok = True
        central_stream_ok = True

    class FakeDispatcher:
        def __init__(self, notifier, transport, contact_ids):
            pass

        def dispatch(self, incident):
            return FakeDispatchResult()

    on_incident_calls: list[Incident] = []

    def _on_incident(inc: Incident) -> None:
        on_incident_calls.append(inc)

    import textwrap

    import yaml

    import relay.node.handler as handler_mod
    from relay.config.schema import EscalationConfig, RelayConfig, RoutingConfig

    ESCALATION_YAML = textwrap.dedent("""\
        policies:
          - policy_id: pol-default
            name: default
            team: team-platform
            steps:
              - step_index: 0
                contact_ids: []
                timeout_minutes: 5
                notify_streams: [TEAM]
    """)
    ROUTING_YAML = textwrap.dedent("""\
        rules: []
        default_escalation_policy_id: pol-default
        default_streams: [TEAM]
    """)

    class FakeConfigLoader:
        def get(self):
            return RelayConfig(
                escalation=EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML)),
                routing=RoutingConfig.model_validate(yaml.safe_load(ROUTING_YAML)),
                loaded_at=datetime.now(UTC),
            )

        def refresh(self):
            return self.get()

    original = handler_mod.DualStreamDispatcher
    handler_mod.DualStreamDispatcher = FakeDispatcher  # type: ignore[assignment]
    try:
        from relay.node.handler import NodeHandler

        handler = NodeHandler(
            _alarm_source=FakeAlarmSource(),
            _config_loader=FakeConfigLoader(),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=FakeIncidentStore(),
            _escalation_state_store=MagicMock(),
            _escalation_engine=FakeEscalationEngine(),
            _on_incident=_on_incident,
        )
        pipeline = DetectionPipeline(handler)
        pipeline.handle_alarm({"source": "aws.cloudwatch"})
    finally:
        handler_mod.DualStreamDispatcher = original  # type: ignore[assignment]

    # Sink must have been called exactly once.
    assert len(on_incident_calls) == 1, (
        f"on_incident sink called {len(on_incident_calls)} times, expected 1"
    )
    assert on_incident_calls[0].correlation_id == incident.correlation_id


def test_detection_pipeline_on_incident_sink_failure_does_not_fail_alarm():
    """A raising on_incident sink must not propagate — the Node still succeeds."""
    incident = _make_incident()

    class FakeAlarmSource:
        def parse_event(self, event):
            return incident

        def bind_config(self, **kwargs):
            pass

    class FakeEscalationEngine:
        class _T:
            old_phase = "IDLE"
            new_phase = "NOTIFIED"
            contact_ids_to_page: list = []
            note = "ok"

        def start(self, incident, policy):
            return self._T()

    class FakeDispatchResult:
        team_stream_ok = True
        central_stream_ok = True

    class FakeDispatcher:
        def __init__(self, notifier, transport, contact_ids):
            pass

        def dispatch(self, incident):
            return FakeDispatchResult()

    def _raising_sink(inc: Incident) -> None:
        raise RuntimeError("sink intentionally explodes")

    import textwrap

    import yaml

    import relay.node.handler as handler_mod
    from relay.config.schema import EscalationConfig, RelayConfig, RoutingConfig

    ESCALATION_YAML = textwrap.dedent("""\
        policies:
          - policy_id: pol-default
            name: default
            team: team-platform
            steps:
              - step_index: 0
                contact_ids: []
                timeout_minutes: 5
                notify_streams: [TEAM]
    """)
    ROUTING_YAML = textwrap.dedent("""\
        rules: []
        default_escalation_policy_id: pol-default
        default_streams: [TEAM]
    """)

    class FakeConfigLoader:
        def get(self):
            return RelayConfig(
                escalation=EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML)),
                routing=RoutingConfig.model_validate(yaml.safe_load(ROUTING_YAML)),
                loaded_at=datetime.now(UTC),
            )

        def refresh(self):
            return self.get()

    original = handler_mod.DualStreamDispatcher
    handler_mod.DualStreamDispatcher = FakeDispatcher  # type: ignore[assignment]
    try:
        from relay.node.handler import NodeHandler

        handler = NodeHandler(
            _alarm_source=FakeAlarmSource(),
            _config_loader=FakeConfigLoader(),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=FakeIncidentStore(),
            _escalation_state_store=MagicMock(),
            _escalation_engine=FakeEscalationEngine(),
            _on_incident=_raising_sink,
        )
        pipeline = DetectionPipeline(handler)
        # Must NOT raise even though the sink raises.
        result = pipeline.handle_alarm({"source": "aws.cloudwatch"})
    finally:
        handler_mod.DualStreamDispatcher = original  # type: ignore[assignment]

    assert result.get("statusCode") == 200


# ---------------------------------------------------------------------------
# NoOpTimerPort — escalation must start (page step 0) without a Scheduler
# ---------------------------------------------------------------------------


def test_noop_timer_lets_escalation_start_without_scheduler():
    """EscalationEngine.start() must not raise when wired with NoOpTimerPort.

    The collapsed container has no EventBridge Scheduler until the DynamoDB
    deadline sweep lands (Step 2); the no-op timer keeps `start()` working so
    step 0 still pages.
    """
    from relay.core.escalation import EscalationEngine, NoOpTimerPort
    from relay.core.model import EscalationPolicy, EscalationStep

    class FakeStateStore:
        def __init__(self) -> None:
            self.saved = []

        def load(self, incident_id):
            return None

        def save(self, ctx):
            self.saved.append(ctx)

    policy = EscalationPolicy(
        policy_id="pol-default",
        name="default",
        team="team-platform",
        steps=[EscalationStep(step_index=0, contact_ids=["cnt1"], timeout_minutes=5)],
    )
    engine = EscalationEngine(timer=NoOpTimerPort(), state_store=FakeStateStore())
    transition = engine.start(_make_incident(), policy)

    assert transition.contact_ids_to_page == ["cnt1"]


# ---------------------------------------------------------------------------
# Step 3 — SQS/handle_event routes raw CloudWatch alarms to the pipeline
# ---------------------------------------------------------------------------


def _make_processor_with_pipeline(pipeline):
    processor = HubProcessor(
        incident_store=FakeIncidentStore(),
        notifier=MagicMock(),
        hub_state=_make_hub_state(),
        sse_publisher=FakeSSEPublisher(),
        forwarder=FakeNoOpForwarder(),
        listeners=[],
    )
    if pipeline is not None:
        processor.set_pipeline(pipeline)
    return processor


def test_handle_event_routes_cloudwatch_alarm_to_pipeline():
    """A raw 'CloudWatch Alarm State Change' event goes to pipeline.handle_alarm."""
    calls: list = []

    class FakePipeline:
        def handle_alarm(self, event):
            calls.append(event)
            return {"statusCode": 200}

    processor = _make_processor_with_pipeline(FakePipeline())
    event = {"detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch",
             "detail": {"alarmName": "x"}}
    processor.handle_event(event)
    assert len(calls) == 1
    assert calls[0] is event


def test_handle_event_alarm_without_pipeline_is_dropped_not_raised():
    """No pipeline wired → alarm is logged + dropped, not crashed (federated Hub)."""
    processor = _make_processor_with_pipeline(None)
    # Must not raise even though no pipeline is set.
    processor.handle_event(
        {"detail-type": "CloudWatch Alarm State Change", "detail": {"alarmName": "x"}}
    )


def test_sqs_consumer_stops_on_shutdown_event():
    """run_forever exits promptly when its shutdown event is set (SIGTERM path)."""
    from relay.hub.app import SQSConsumer

    shutdown = threading.Event()
    shutdown.set()  # already signalled → loop must not even poll once

    consumer = SQSConsumer.__new__(SQSConsumer)
    consumer._queue_url = "https://sqs.us-east-1.amazonaws.com/123/q"
    consumer._handler = MagicMock()
    consumer._shutdown = shutdown
    consumer._sqs = MagicMock()

    consumer.run_forever()  # returns immediately
    consumer._sqs.receive_message.assert_not_called()


def test_handle_event_incident_event_still_uses_handle_incident():
    """A forwarded Incident event (not an alarm) must NOT go to the pipeline."""
    alarm_calls: list = []

    class FakePipeline:
        def handle_alarm(self, event):
            alarm_calls.append(event)

    processor = _make_processor_with_pipeline(FakePipeline())
    incident = _make_incident()
    # An incident event has no CloudWatch detail-type; it should persist via
    # _handle_incident, not the pipeline.
    processor.handle_event(incident.model_dump(mode="json"))
    assert alarm_calls == []
    assert processor._incident_store.get_incident(incident.correlation_id) is not None


# ---------------------------------------------------------------------------
# Test 3 — POST /ingest/alarm FastAPI route
# ---------------------------------------------------------------------------


def _hub_app_with_pipeline(runtime: str, pipeline) -> TestClient:
    """Build a minimal HubApp-like object and TestClient for route testing."""
    app_obj = HubApp.__new__(HubApp)

    hs = _make_hub_state()
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    app_obj._incident_store = FakeIncidentStore()
    app_obj._contact_store = MagicMock()
    app_obj._notifier = MagicMock()
    app_obj._paging_topic_arn = None
    app_obj._settings_store = None
    app_obj._schedule_store = None
    app_obj._pipeline = pipeline
    app_obj._runtime = runtime

    return TestClient(app_obj.build_fastapi_app())


def test_ingest_alarm_returns_403_in_fargate_runtime():
    """POST /ingest/alarm must return 403 when runtime=fargate and RELAY_ALLOW_INGEST unset."""
    client = _hub_app_with_pipeline("fargate", MagicMock())
    r = client.post("/ingest/alarm", json={"source": "aws.cloudwatch"})
    assert r.status_code == 403
    assert "ingest disabled" in r.json()["detail"]


def test_ingest_alarm_routes_to_pipeline_when_runtime_local_mock():
    """POST /ingest/alarm routes to the pipeline when runtime=local-mock."""

    class FakePipeline:
        def handle_alarm(self, payload: dict) -> dict:
            return {"statusCode": 200, "correlation_id": "test-123", "from_fake": True}

    client = _hub_app_with_pipeline("local-mock", FakePipeline())
    r = client.post("/ingest/alarm", json={"source": "aws.cloudwatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["from_fake"] is True


def test_ingest_alarm_returns_503_when_pipeline_is_none(monkeypatch):
    """POST /ingest/alarm returns 503 when the pipeline is None (build failed)."""
    monkeypatch.setenv("RELAY_ALLOW_INGEST", "true")
    client = _hub_app_with_pipeline("fargate", None)
    r = client.post("/ingest/alarm", json={"source": "aws.cloudwatch"})
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]


def test_ingest_alarm_allowed_via_env_override(monkeypatch):
    """RELAY_ALLOW_INGEST=true must open the endpoint regardless of runtime."""
    monkeypatch.setenv("RELAY_ALLOW_INGEST", "true")

    class FakePipeline:
        def handle_alarm(self, payload: dict) -> dict:
            return {"statusCode": 200, "correlation_id": "env-override"}

    client = _hub_app_with_pipeline("fargate", FakePipeline())
    r = client.post("/ingest/alarm", json={"source": "aws.cloudwatch"})
    assert r.status_code == 200
    assert r.json()["correlation_id"] == "env-override"


def test_ingest_alarm_returns_400_on_value_error(monkeypatch):
    """POST /ingest/alarm returns 400 when the pipeline raises ValueError."""
    monkeypatch.setenv("RELAY_ALLOW_INGEST", "true")

    class FailingPipeline:
        def handle_alarm(self, payload: dict) -> dict:
            raise ValueError("bad event shape")

    client = _hub_app_with_pipeline("fargate", FailingPipeline())
    r = client.post("/ingest/alarm", json={})
    assert r.status_code == 400
    assert "bad event shape" in r.json()["detail"]
