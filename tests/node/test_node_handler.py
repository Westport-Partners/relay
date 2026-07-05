"""Tests for NodeHandler — TTL-based config refresh and OrgTree-aware routing.

Strategy
--------
NodeHandler.__init__ reads many environment variables and wires real boto3
clients.  Rather than mocking boto3 at the module level, we:

  1. Set the required env vars with ``monkeypatch.setenv`` so __init__ doesn't
     KeyError on missing vars.
  2. Inject *all* heavyweight collaborators via the keyword-only ``_*``
     injection parameters added for testing.

This keeps the production code path intact (injection params all default to
None, so real adapters are created in production) while allowing unit tests to
run with no AWS credentials.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

import relay.node.handler as handler_mod
from relay.config.schema import (
    EscalationConfig,
    RelayConfig,
    RoutingConfig,
)
from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
)

# ---------------------------------------------------------------------------
# Environment variable fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def handler_env_vars(monkeypatch):
    """Set every env var that NodeHandler.__init__ reads.

    Without these, NodeHandler() would raise KeyError on the very first line
    of __init__.  Values are dummy strings — the real AWS clients are never
    created in tests because we inject fakes instead.
    """
    vars_to_set = {
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
    }
    for key, value in vars_to_set.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------

ESCALATION_YAML = textwrap.dedent("""\
    policies:
      - policy_id: pol-default
        name: default
        team: team-platform
        steps:
          - step_index: 0
            contact_ids: [cnt_primary]
            timeout_minutes: 5
            notify_streams: [TEAM]
""")

ROUTING_YAML = textwrap.dedent("""\
    rules: []
    default_escalation_policy_id: pol-default
    default_streams: [TEAM]
""")


def make_relay_config() -> RelayConfig:
    """Build a minimal RelayConfig."""
    import yaml
    escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
    routing = RoutingConfig.model_validate(yaml.safe_load(ROUTING_YAML))
    return RelayConfig(
        escalation=escalation,
        routing=routing,
        loaded_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------

class FakeConfigLoader:
    """Tracks get() and refresh() calls; returns whatever config is set."""

    def __init__(self, initial_config: RelayConfig) -> None:
        self._config = initial_config
        self.get_calls: int = 0
        self.refresh_calls: int = 0
        self._refresh_raises: Exception | None = None

    def get(self) -> RelayConfig:
        self.get_calls += 1
        return self._config

    def refresh(self) -> RelayConfig:
        self.refresh_calls += 1
        if self._refresh_raises is not None:
            raise self._refresh_raises
        return self._config

    def make_refresh_raise(self, exc: Exception) -> None:
        self._refresh_raises = exc


def _make_incident(
    *,
    deployment_id: str = "unknown",
    environment: str = "prod",
) -> Incident:
    return Incident(
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="test-alarm-high-error",
        deployment_id=deployment_id,
        environment=environment,
    )


class FakeAlarmSource:
    """Returns a pre-built Incident for any event."""

    def __init__(self, incident: Incident) -> None:
        self._incident = incident

    def parse_event(self, event: dict[str, Any]) -> Incident:
        return self._incident


class FakeIncidentStore:
    def put_incident(self, incident: Incident) -> None:
        pass

    def get_incident(self, correlation_id: str) -> Incident | None:
        return None


class FakeEscalationStateStore:
    def load(self, incident_id: str):
        return None

    def save(self, ctx) -> None:
        pass


class FakeDispatchResult:
    team_stream_ok = True
    central_stream_ok = True


class FakeDispatcher:
    """Replaces DualStreamDispatcher.dispatch; always succeeds."""

    def __init__(self, notifier, transport, contact_ids):
        pass

    def dispatch(self, incident: Incident) -> FakeDispatchResult:
        return FakeDispatchResult()


class FakeEscalationEngine:
    """Fake EscalationEngine — start() returns a transition with no contacts."""

    class _Transition:
        old_phase = "IDLE"
        new_phase = "NOTIFIED"
        contact_ids_to_page: list[str] = []
        note = "test"

    def start(self, incident, policy):
        return self._Transition()

    def on_timeout(self, incident_id, step_index, policy):
        return self._Transition()

    def acknowledge(self, incident_id, contact_id, policy):
        return self._Transition()


# ---------------------------------------------------------------------------
# Helper: build a fully-faked NodeHandler
# ---------------------------------------------------------------------------

def _make_handler(
    config: RelayConfig | None = None,
    clock_values: list[float] | None = None,
    config_loader: FakeConfigLoader | None = None,
    incident: Incident | None = None,
    ttl: float | None = None,
    monkeypatch=None,
) -> tuple[handler_mod.NodeHandler, FakeConfigLoader]:
    """Construct a NodeHandler with all AWS collaborators faked out.

    Returns (handler, fake_config_loader) for inspection.
    """
    cfg = config or make_relay_config()
    loader = config_loader or FakeConfigLoader(cfg)

    # Controllable monotonic clock
    if clock_values is not None:
        _clock_iter = iter(clock_values)

        def _clock() -> float:
            return next(_clock_iter)
    else:
        _val = [0.0]

        def _clock() -> float:
            return _val[0]

    if ttl is not None and monkeypatch is not None:
        monkeypatch.setenv("RELAY_CONFIG_TTL_SECONDS", str(ttl))

    inc = incident or _make_incident()
    alarm_source = FakeAlarmSource(inc)
    incident_store = FakeIncidentStore()
    esc_state_store = FakeEscalationStateStore()
    esc_engine = FakeEscalationEngine()

    # Patch DualStreamDispatcher so no real AWS calls happen
    import relay.node.handler as handler_mod
    original_dispatcher = handler_mod.DualStreamDispatcher
    handler_mod.DualStreamDispatcher = FakeDispatcher

    try:
        h = handler_mod.NodeHandler(
            _config_loader=loader,
            _alarm_source=alarm_source,
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=incident_store,
            _escalation_state_store=esc_state_store,
            _escalation_engine=esc_engine,
            _clock=_clock,
        )
    finally:
        handler_mod.DualStreamDispatcher = original_dispatcher

    # Keep the patched dispatcher in place for the duration of the test by
    # re-patching it on the module after construction.  We store the revert
    # for callers that want to call process().
    handler_mod.DualStreamDispatcher = FakeDispatcher

    return h, loader


# ---------------------------------------------------------------------------
# TTL refresh tests
# ---------------------------------------------------------------------------

class TestConfigTTLRefresh:

    def test_refresh_not_called_before_ttl(self, monkeypatch):
        """process() must NOT call refresh() if the TTL has not yet elapsed."""
        import relay.node.handler as handler_mod

        original_dispatcher = handler_mod.DualStreamDispatcher
        try:
            # Clock: __init__ gets 0.0, process() gets 50.0 — less than 300s TTL.
            values = [0.0, 50.0]
            idx = [0]

            def controlled_clock() -> float:
                v = values[idx[0]]
                idx[0] = min(idx[0] + 1, len(values) - 1)
                return v

            cfg = make_relay_config()
            loader = FakeConfigLoader(cfg)
            inc = _make_incident()
            alarm_source = FakeAlarmSource(inc)
            handler_mod.DualStreamDispatcher = FakeDispatcher

            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=alarm_source,
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                _clock=controlled_clock,
            )
            # Reset after construction (init consumed one value)
            loader.refresh_calls = 0

            # Simulate a process() call at t=50 (TTL=300, not yet elapsed)
            h.process({})  # falls through to _handle_alarm via ValueError handled gracefully

            assert loader.refresh_calls == 0, (
                "refresh() should NOT be called before TTL elapses"
            )
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher

    def test_refresh_called_once_ttl_elapses(self, monkeypatch):
        """process() MUST call refresh() exactly once after the TTL elapses."""
        import relay.node.handler as handler_mod

        monkeypatch.setenv("RELAY_CONFIG_TTL_SECONDS", "100")
        original_dispatcher = handler_mod.DualStreamDispatcher
        try:
            # Clock values: init gets 0, process() gets 200 (>100s TTL), then 200 again for update.
            values = [0.0, 200.0, 200.0]
            idx = [0]

            def controlled_clock() -> float:
                v = values[idx[0]]
                idx[0] = min(idx[0] + 1, len(values) - 1)
                return v

            cfg = make_relay_config()
            loader = FakeConfigLoader(cfg)
            inc = _make_incident()
            alarm_source = FakeAlarmSource(inc)
            handler_mod.DualStreamDispatcher = FakeDispatcher

            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=alarm_source,
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                _clock=controlled_clock,
            )
            loader.refresh_calls = 0  # reset after init

            h.process({})

            assert loader.refresh_calls == 1, (
                "refresh() should be called exactly once when TTL elapses"
            )
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher

    def test_refresh_failure_is_swallowed_and_old_config_retained(self, monkeypatch):
        """A refresh() that raises must NOT propagate and must keep the old config."""
        import relay.node.handler as handler_mod

        monkeypatch.setenv("RELAY_CONFIG_TTL_SECONDS", "100")
        original_dispatcher = handler_mod.DualStreamDispatcher
        try:
            values = [0.0, 999.0, 999.0]
            idx = [0]

            def controlled_clock() -> float:
                v = values[idx[0]]
                idx[0] = min(idx[0] + 1, len(values) - 1)
                return v

            cfg = make_relay_config()
            loader = FakeConfigLoader(cfg)
            loader.make_refresh_raise(RuntimeError("GitLab unreachable"))
            original_config_id = id(cfg)

            inc = _make_incident()
            alarm_source = FakeAlarmSource(inc)
            handler_mod.DualStreamDispatcher = FakeDispatcher

            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=alarm_source,
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                _clock=controlled_clock,
            )
            loader.refresh_calls = 0

            # Must not raise even though refresh() raises
            result = h.process({})

            assert loader.refresh_calls == 1, "refresh() should have been attempted"
            # Old config is still in place
            assert id(h.config) == original_config_id, (
                "config must remain the original object after a failed refresh"
            )
            # Event handling still produced a result
            assert "statusCode" in result
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher

    def test_event_handling_succeeds_after_refresh_failure(self, monkeypatch):
        """Even when refresh() raises, the alarm event must be processed successfully."""
        import relay.node.handler as handler_mod

        monkeypatch.setenv("RELAY_CONFIG_TTL_SECONDS", "1")
        original_dispatcher = handler_mod.DualStreamDispatcher
        try:
            values = [0.0, 500.0, 500.0]
            idx = [0]

            def controlled_clock() -> float:
                v = values[idx[0]]
                idx[0] = min(idx[0] + 1, len(values) - 1)
                return v

            cfg = make_relay_config()
            loader = FakeConfigLoader(cfg)
            loader.make_refresh_raise(RuntimeError("network down"))

            inc = _make_incident()
            alarm_source = FakeAlarmSource(inc)
            handler_mod.DualStreamDispatcher = FakeDispatcher

            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=alarm_source,
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                _clock=controlled_clock,
            )

            result = h.process({})
            # Should succeed with statusCode 200 from _handle_alarm
            assert result.get("statusCode") == 200
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher


# ---------------------------------------------------------------------------
# datetime.utcnow fix — smoke test that the handler runs without DeprecationWarning
# ---------------------------------------------------------------------------

class TestDatetimeFix:

    def test_no_utcnow_in_handler_source(self):
        """handler.py must not contain datetime.utcnow() calls."""
        import inspect

        import relay.node.handler as handler_mod

        source = inspect.getsource(handler_mod)
        assert "utcnow()" not in source, (
            "handler.py still contains datetime.utcnow(); "
            "it should use datetime.now(timezone.utc) instead"
        )


# ---------------------------------------------------------------------------
# Role resolver wiring — the Node must build a schedule-backed resolver from the
# shared team table so escalation roles (primary/secondary/manager) resolve to
# real people. The team Hub may be scaled to zero, so the Node pages itself.
# ---------------------------------------------------------------------------

class TestRoleResolverWiring:

    def _build(self, **inject):
        import relay.node.handler as handler_mod

        cfg = make_relay_config()
        loader = FakeConfigLoader(cfg)
        original_dispatcher = handler_mod.DualStreamDispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        try:
            return handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=FakeAlarmSource(_make_incident()),
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                **inject,
            )
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher

    def test_default_construction_wires_schedule_backed_resolver(self):
        """With no injection, the Node builds a real ScheduleRoleResolver backed
        by the shared RELAY_TABLE_NAME — NOT None (the old deferred state)."""
        from relay.core.role_resolver import ScheduleRoleResolver

        h = self._build()
        assert isinstance(h.role_resolver, ScheduleRoleResolver), (
            "default Node must wire a schedule-backed role resolver so role-based "
            "escalation can resolve people without the Hub"
        )

    def test_injected_resolver_is_respected(self):
        """An injected resolver (tests) overrides the default wiring."""
        sentinel = MagicMock()
        h = self._build(_role_resolver=sentinel)
        assert h.role_resolver is sentinel


# ---------------------------------------------------------------------------
# Heartbeat tests
# ---------------------------------------------------------------------------

class TestHeartbeat:

    def _make_heartbeat_handler(self, monkeypatch) -> handler_mod.NodeHandler:
        """Build a NodeHandler with all AWS collaborators faked, env set explicitly."""
        import relay.node.handler as handler_mod

        cfg = make_relay_config()
        loader = FakeConfigLoader(cfg)
        original_dispatcher = handler_mod.DualStreamDispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        try:
            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=FakeAlarmSource(_make_incident()),
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
            )
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher
        # Keep dispatcher patched so process() calls work cleanly
        handler_mod.DualStreamDispatcher = FakeDispatcher
        return h

    def test_heartbeat_event_emits_via_transport(self, monkeypatch):
        """process({"relay_event": "heartbeat"}) emits via transport and returns heartbeat_ok."""
        monkeypatch.setenv("RELAY_NODE_APP_NAME", "billing-api")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "billing-api-prod")
        monkeypatch.setenv("RELAY_NODE_ENVIRONMENT", "prod")
        monkeypatch.setenv("RELAY_ACCOUNT_ID", "123456789012")

        h = self._make_heartbeat_handler(monkeypatch)

        result = h.process({"relay_event": "heartbeat"})

        assert result["statusCode"] == 200
        assert result["note"] == "heartbeat_ok"
        assert h.transport.emit_heartbeat.call_count == 1

        call_kwargs = h.transport.emit_heartbeat.call_args.kwargs
        assert call_kwargs["account_id"] == "123456789012"
        assert call_kwargs["app_name"] == "billing-api"
        assert call_kwargs["deployment_id"] == "billing-api-prod"
        assert call_kwargs["environment"] == "prod"
        assert call_kwargs["timestamp"] != ""

    def test_heartbeat_failure_is_swallowed(self, monkeypatch):
        """A heartbeat transport failure must not raise — returns heartbeat_failed."""
        monkeypatch.setenv("RELAY_NODE_APP_NAME", "billing-api")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "billing-api-prod")
        monkeypatch.setenv("RELAY_NODE_ENVIRONMENT", "prod")

        h = self._make_heartbeat_handler(monkeypatch)
        h.transport.emit_heartbeat.side_effect = RuntimeError("boom")

        result = h.process({"relay_event": "heartbeat"})

        assert result["statusCode"] == 200
        assert result["note"] == "heartbeat_failed"

    def test_heartbeat_defaults_app_name_to_team(self, monkeypatch):
        """When RELAY_NODE_APP_NAME is unset, app_name falls back to RELAY_TEAM_NAME."""
        monkeypatch.delenv("RELAY_NODE_APP_NAME", raising=False)
        monkeypatch.setenv("RELAY_TEAM_NAME", "acme")

        h = self._make_heartbeat_handler(monkeypatch)

        h.process({"relay_event": "heartbeat"})

        call_kwargs = h.transport.emit_heartbeat.call_args.kwargs
        assert call_kwargs["app_name"] == "acme"

    def test_heartbeat_carries_metadata_and_oncall_snapshot(self, monkeypatch):
        """Enriched metadata + the on-call snapshot ride the heartbeat kwargs.

        Both are best-effort enrichments resolved from injected fakes — the
        snapshot lets a federated Hub show who owns the app without reaching
        this team's schedule.
        """
        from datetime import UTC, datetime

        import relay.node.handler as handler_mod

        monkeypatch.setenv("RELAY_NODE_APP_NAME", "billing-api")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "billing-api")
        monkeypatch.setenv("RELAY_ACCOUNT_ID", "123456789012")

        # Fake enricher returns fixed metadata.
        enricher = MagicMock()
        enricher.build_metadata.return_value = {"owner": "team-bill", "aws_tags": {"env": "prod"}}

        # Fake schedule store with a schedule covering "now" so a snapshot resolves.
        from relay.core.scheduling import Availability, auto_schedule, monday_of

        now = datetime.now(UTC)
        ws = monday_of(now.date())
        av = Availability(
            contact_id="cnt-bill",
            available=True,
            slots={d: ["night", "day", "evening"] for d in
                   ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            ooo=None,
        )
        sched = auto_schedule(ws, [av])
        stored = {
            "week_start": sched.week_start.isoformat(),
            "slots": [{"date": s.date.isoformat(), "shift": s.shift.value,
                       "role": s.role.value, "contact_id": s.contact_id} for s in sched.slots],
            "roles": [r.value for r in sched.roles],
        }
        schedule_store = MagicMock()
        schedule_store.get_schedule.return_value = stored
        schedule_store.get_overrides.return_value = []

        contact_store = MagicMock()
        contact = MagicMock()
        contact.contact_id = "cnt-bill"
        contact.name = "Biller"
        contact_store.list_contacts.return_value = [contact]

        cfg = make_relay_config()
        loader = FakeConfigLoader(cfg)
        original = handler_mod.DualStreamDispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        try:
            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=FakeAlarmSource(_make_incident()),
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=contact_store,
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
                _schedule_store=schedule_store,
                _tag_enricher=enricher,
            )
        finally:
            handler_mod.DualStreamDispatcher = original

        h.process({"relay_event": "heartbeat"})

        kwargs = h.transport.emit_heartbeat.call_args.kwargs
        assert kwargs["metadata"] == {"owner": "team-bill", "aws_tags": {"env": "prod"}}
        assert kwargs["on_call"] is not None
        assert kwargs["on_call"]["source"] == "team_snapshot"
        assert kwargs["on_call"]["roles"]["primary"]["name"] == "Biller"


# ---------------------------------------------------------------------------
# Org path resolution
# ---------------------------------------------------------------------------


class TestHeartbeatOrgPath:
    """Tests for _resolve_org_path() and the org_path kwarg passed to emit_heartbeat."""

    def _make_handler(self, monkeypatch) -> handler_mod.NodeHandler:
        """Build a NodeHandler with all AWS collaborators faked."""
        import relay.node.handler as handler_mod

        cfg = make_relay_config()
        loader = FakeConfigLoader(cfg)
        original_dispatcher = handler_mod.DualStreamDispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        try:
            h = handler_mod.NodeHandler(
                _config_loader=loader,
                _alarm_source=FakeAlarmSource(_make_incident()),
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=FakeIncidentStore(),
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
            )
        finally:
            handler_mod.DualStreamDispatcher = original_dispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        return h

    def test_heartbeat_emits_org_path_override(self, monkeypatch):
        """RELAY_NODE_ORG_PATH override wins; passed as org_path kwarg."""
        override = [{"id": "dep-x", "name": "X", "level": "deployment", "parent": None}]
        monkeypatch.setenv("RELAY_NODE_ORG_PATH", json.dumps(override))
        monkeypatch.setenv("RELAY_NODE_APP_NAME", "svc-x")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "dep-x")

        h = self._make_handler(monkeypatch)
        h.process({"relay_event": "heartbeat"})

        call_kwargs = h.transport.emit_heartbeat.call_args.kwargs
        assert call_kwargs["org_path"] == override

    def test_heartbeat_org_path_falls_back_to_synthetic(self, monkeypatch):
        """When RELAY_NODE_ORG_PATH is unset and config has no org_tree, use synthetic node."""
        monkeypatch.delenv("RELAY_NODE_ORG_PATH", raising=False)
        monkeypatch.setenv("RELAY_NODE_APP_NAME", "svc")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "svc-prod")

        h = self._make_handler(monkeypatch)
        # make_relay_config() does not set org_tree, so it defaults to None
        assert getattr(h.config, "org_tree", None) is None

        h.process({"relay_event": "heartbeat"})

        call_kwargs = h.transport.emit_heartbeat.call_args.kwargs
        org_path = call_kwargs["org_path"]
        assert isinstance(org_path, list)
        assert len(org_path) == 1
        node = org_path[0]
        assert node["id"] == "svc-prod"
        assert node["name"] == "svc"
        assert node["level"] == "deployment"
        assert node["parent"] is None

    def test_heartbeat_org_path_invalid_json_is_ignored(self, monkeypatch):
        """Invalid JSON in RELAY_NODE_ORG_PATH is silently ignored; falls back to synthetic."""
        monkeypatch.setenv("RELAY_NODE_ORG_PATH", "{not json")
        monkeypatch.setenv("RELAY_NODE_APP_NAME", "fallback-svc")
        monkeypatch.setenv("RELAY_NODE_DEPLOYMENT_ID", "fallback-dep")

        h = self._make_handler(monkeypatch)
        # Should not raise
        result = h.process({"relay_event": "heartbeat"})
        assert result["statusCode"] == 200

        call_kwargs = h.transport.emit_heartbeat.call_args.kwargs
        org_path = call_kwargs["org_path"]
        # Override was ignored → synthetic fallback
        assert isinstance(org_path, list)
        assert len(org_path) == 1
        assert org_path[0]["id"] == "fallback-dep"
        assert org_path[0]["level"] == "deployment"


# ---------------------------------------------------------------------------
# Escalation timeout → incident.state = ESCALATED (so the Hub sees a transition)
# ---------------------------------------------------------------------------


class TestTimeoutMarksEscalated:
    """A timeout that pages the next step transitions the incident to ESCALATED."""

    class _StoringIncidentStore:
        def __init__(self, incident: Incident) -> None:
            self._inc = incident
            self.puts: list[Incident] = []

        def get_incident(self, correlation_id: str) -> Incident | None:
            return self._inc

        def put_incident(self, incident: Incident) -> None:
            self.puts.append(incident)

    class _EscalatingEngine:
        """on_timeout returns a transition that pages a contact."""

        class _Transition:
            old_phase = "WAITING_ACK"
            new_phase = "ESCALATING"
            contact_ids_to_page = ["cnt-secondary"]
            roles_to_page: list[str] = []
            streams: list[str] = []
            note = "advanced"

        def on_timeout(self, incident_id, step_index, policy):
            return self._Transition()

    def _build(self, monkeypatch, incident: Incident):
        import relay.node.handler as handler_mod

        store = self._StoringIncidentStore(incident)
        monkeypatch.setattr(handler_mod, "DualStreamDispatcher", FakeDispatcher)
        h = handler_mod.NodeHandler(
            _config_loader=FakeConfigLoader(make_relay_config()),
            _alarm_source=FakeAlarmSource(incident),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=store,
            _escalation_state_store=FakeEscalationStateStore(),
            _escalation_engine=self._EscalatingEngine(),
            _clock=lambda: 0.0,
        )
        # Resolve contacts directly to the paged ids (skip schedule resolution).
        h._contacts_for_transition = lambda transition: list(
            transition.contact_ids_to_page
        )
        return h, store

    def test_timeout_sets_escalated_state(self, monkeypatch):
        inc = _make_incident()
        assert inc.state == IncidentState.TRIGGERED
        h, store = self._build(monkeypatch, inc)

        result = h.process(
            {"relay_event": "escalation_timeout", "incident_id": inc.correlation_id,
             "step_index": 0}
        )

        assert result["statusCode"] == 200
        assert inc.state == IncidentState.ESCALATED
        # The escalated incident was persisted before dispatch.
        assert any(p.state == IncidentState.ESCALATED for p in store.puts)

    def test_timeout_does_not_escalate_acknowledged(self, monkeypatch):
        """An already-acknowledged incident is not flipped to ESCALATED."""
        inc = _make_incident()
        inc.state = IncidentState.ACKNOWLEDGED
        h, _ = self._build(monkeypatch, inc)

        h.process(
            {"relay_event": "escalation_timeout", "incident_id": inc.correlation_id,
             "step_index": 0}
        )

        assert inc.state == IncidentState.ACKNOWLEDGED


# ---------------------------------------------------------------------------
# Routing provenance — routing_rule_id / routing_reason stamped on Incident
# ---------------------------------------------------------------------------


class TestRoutingProvenance:
    """_handle_alarm must stamp routing_rule_id and routing_reason on the incident."""

    class _CapturingIncidentStore:
        """Captures every put_incident call so tests can inspect the saved incident."""

        def __init__(self) -> None:
            self.puts: list[Incident] = []

        def put_incident(self, incident: Incident) -> None:
            self.puts.append(incident)

        def get_incident(self, correlation_id: str) -> Incident | None:
            return None

    def _build_handler(self, incident: Incident, config: RelayConfig, store):  # noqa: F821
        monkeypatch_dispatcher = handler_mod.DualStreamDispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        try:
            h = handler_mod.NodeHandler(
                _config_loader=FakeConfigLoader(config),
                _alarm_source=FakeAlarmSource(incident),
                _notifier=MagicMock(),
                _transport=MagicMock(),
                _contact_store=MagicMock(),
                _incident_store=store,
                _escalation_state_store=FakeEscalationStateStore(),
                _escalation_engine=FakeEscalationEngine(),
            )
        finally:
            handler_mod.DualStreamDispatcher = monkeypatch_dispatcher
        handler_mod.DualStreamDispatcher = FakeDispatcher
        return h

    def _make_config_with_rule(self, rule_id: str, alarm_name_prefix: str) -> RelayConfig:  # noqa: F821
        """Build a RelayConfig with one routing rule matching alarm_name_prefix."""
        import yaml

        from relay.core.model import RoutingRule

        escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
        routing = RoutingConfig(
            rules=[
                RoutingRule(
                    rule_id=rule_id,
                    priority=10,
                    alarm_name_prefix=alarm_name_prefix,
                    escalation_policy_id="pol-default",
                )
            ],
            default_escalation_policy_id="pol-default",
            default_streams=["TEAM"],
        )
        return RelayConfig(
            escalation=escalation,
            routing=routing,
            loaded_at=datetime.now(UTC),
        )

    def test_matched_rule_stamps_routing_rule_id(self):
        """When a routing rule matches, incident.routing_rule_id == that rule's id."""
        inc = _make_incident()
        # The alarm name starts with "test-alarm" → rule should match.
        cfg = self._make_config_with_rule("rule-test-alarms", "test-alarm")
        store = self._CapturingIncidentStore()

        h = self._build_handler(inc, cfg, store)
        result = h.process({})

        assert result.get("statusCode") == 200
        assert inc.routing_rule_id == "rule-test-alarms"
        assert "rule-test-alarms" in inc.routing_reason

    def test_no_matched_rule_leaves_routing_rule_id_none(self):
        """When no routing rule matches, routing_rule_id is None and routing_reason is non-empty."""
        inc = _make_incident()
        # Use the default config which has rules=[] → no rule will match.
        cfg = make_relay_config()
        store = self._CapturingIncidentStore()

        h = self._build_handler(inc, cfg, store)
        result = h.process({})

        assert result.get("statusCode") == 200
        assert inc.routing_rule_id is None
        assert inc.routing_reason != ""

    def test_routing_provenance_survives_model_dump(self):
        """routing_rule_id and routing_reason must appear in model_dump output."""
        inc = _make_incident()
        cfg = self._make_config_with_rule("rule-dump-check", "test-alarm")
        store = self._CapturingIncidentStore()

        h = self._build_handler(inc, cfg, store)
        h.process({})

        dumped = inc.model_dump(mode="json")
        assert dumped["routing_rule_id"] == "rule-dump-check"
        assert "rule-dump-check" in dumped["routing_reason"]
