"""Integration test: NodeHandler._handle_alarm stamps deployment_metadata on the incident.

Mirrors the construction pattern in tests/test_node_handler.py (FakeAlarmSource,
FakeConfigLoader, FakeIncidentStore, FakeEscalationEngine, FakeDispatcher) so
the existing test harness conventions are consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from relay.config.schema import (
    DeploymentDefaults,
    EscalationConfig,
    HierarchyConfig,
    RelayConfig,
    RoutingConfig,
)
from relay.core.model import (
    Incident,
    OrgNode,
    OrgTree,
    Severity,
    SignalSource,
)
from relay.node.handler import NodeHandler

# ---------------------------------------------------------------------------
# Environment variables required by NodeHandler.__init__
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def handler_env_vars(monkeypatch):
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
# Fakes (mirror test_node_handler.py)
# ---------------------------------------------------------------------------


class FakeConfigLoader:
    def __init__(self, config: RelayConfig) -> None:
        self._config = config

    def get(self) -> RelayConfig:
        return self._config

    def refresh(self) -> RelayConfig:
        return self._config


class FakeAlarmSource:
    def __init__(self, incident: Incident) -> None:
        self._incident = incident

    def parse_event(self, event: dict[str, Any]) -> Incident:
        return self._incident


class FakeIncidentStore:
    def __init__(self) -> None:
        self.stored: list[Incident] = []

    def put_incident(self, incident: Incident) -> None:
        self.stored.append(incident)

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
    def __init__(self, notifier, transport, contact_ids):
        pass

    def dispatch(self, incident: Incident) -> FakeDispatchResult:
        return FakeDispatchResult()


class FakeEscalationEngine:
    class _Transition:
        old_phase = "IDLE"
        new_phase = "NOTIFIED"
        contact_ids_to_page: list[str] = []
        note = "test"

    def start(self, incident, policy):
        return self._Transition()


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _make_base_config(
    hierarchy: HierarchyConfig | None = None,
    org_tree: OrgTree | None = None,
) -> RelayConfig:
    import yaml

    escalation_yaml = """
policies:
  - policy_id: pol-default
    name: default
    team: team-platform
    steps:
      - step_index: 0
        contact_ids: [cnt_primary]
        timeout_minutes: 5
        notify_streams: [TEAM]
"""
    routing_yaml = """
rules: []
default_escalation_policy_id: pol-default
default_streams: [TEAM]
"""
    escalation = EscalationConfig.model_validate(yaml.safe_load(escalation_yaml))
    routing = RoutingConfig.model_validate(yaml.safe_load(routing_yaml))
    return RelayConfig(
        escalation=escalation,
        routing=routing,
        loaded_at=datetime.now(UTC),
        hierarchy=hierarchy,
        org_tree=org_tree,
    )


def _build_handler(config: RelayConfig, incident: Incident) -> tuple[NodeHandler, FakeIncidentStore]:
    import relay.node.handler as handler_mod

    store = FakeIncidentStore()
    original = handler_mod.DualStreamDispatcher
    handler_mod.DualStreamDispatcher = FakeDispatcher
    try:
        h = NodeHandler(
            _config_loader=FakeConfigLoader(config),
            _alarm_source=FakeAlarmSource(incident),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=store,
            _escalation_state_store=FakeEscalationStateStore(),
            _escalation_engine=FakeEscalationEngine(),
            _clock=lambda: 0.0,
        )
    finally:
        handler_mod.DualStreamDispatcher = original
    return h, store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeploymentMetadataResolution:

    def _make_incident_with_tags(self, deployment_id: str = "dep-auth-prod") -> Incident:
        return Incident(
            account_id="123456789012",
            region="us-east-1",
            app_name="auth-api",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="auth-api-errors",
            deployment_id=deployment_id,
            environment="prod",
            tags={
                "COMPONENT_ID": "auth-api",
                "GIT_SHA": "abc123",
                "GITLAB_PROJECT_ID": "platform/auth",
            },
        )

    def test_handler_stamps_deployment_metadata_from_tag_map(self) -> None:
        """tag_map in hierarchy.deployment_defaults → metadata keys resolved from incident tags."""
        hierarchy = HierarchyConfig(
            levels=["product_line", "product", "component", "deployment"],
            leaf_level="deployment",
            deployment_defaults=DeploymentDefaults(
                tag_map={
                    "component_id": "COMPONENT_ID",
                    "git_sha": "GIT_SHA",
                }
            ),
        )
        config = _make_base_config(hierarchy=hierarchy)
        incident = self._make_incident_with_tags()

        h, store = _build_handler(config, incident)
        h.process({})

        assert incident.deployment_metadata["component_id"] == "auth-api"
        assert incident.deployment_metadata["git_sha"] == "abc123"

    def test_handler_stamps_deployment_metadata_from_node_template(self) -> None:
        """Node metadata with ${tag:X} template → resolved from incident tags."""
        node = OrgNode(
            id="dep-auth-prod",
            name="auth-api-prod",
            level="deployment",
            parent=None,
            metadata={"gitlab_project": "${tag:GITLAB_PROJECT_ID}"},
        )
        org_tree = OrgTree([node], leaf_level="deployment")
        config = _make_base_config(org_tree=org_tree)
        incident = self._make_incident_with_tags(deployment_id="dep-auth-prod")

        h, store = _build_handler(config, incident)
        h.process({})

        assert incident.deployment_metadata["gitlab_project"] == "platform/auth"

    def test_handler_stamps_both_tag_map_and_node_template(self) -> None:
        """tag_map base layer + node template overlay both contribute."""
        hierarchy = HierarchyConfig(
            levels=["product_line", "product", "component", "deployment"],
            leaf_level="deployment",
            deployment_defaults=DeploymentDefaults(
                tag_map={"component_id": "COMPONENT_ID"}
            ),
        )
        node = OrgNode(
            id="dep-auth-prod",
            name="auth-api-prod",
            level="deployment",
            parent=None,
            metadata={"gitlab_project": "${tag:GITLAB_PROJECT_ID}"},
        )
        org_tree = OrgTree([node], leaf_level="deployment")
        config = _make_base_config(hierarchy=hierarchy, org_tree=org_tree)
        incident = self._make_incident_with_tags(deployment_id="dep-auth-prod")

        h, store = _build_handler(config, incident)
        h.process({})

        assert incident.deployment_metadata["component_id"] == "auth-api"
        assert incident.deployment_metadata["gitlab_project"] == "platform/auth"

    def test_handler_metadata_empty_when_no_tags(self) -> None:
        """With no incident tags, metadata is not set (nothing resolves)."""
        hierarchy = HierarchyConfig(
            levels=["deployment"],
            leaf_level="deployment",
            deployment_defaults=DeploymentDefaults(
                tag_map={"component_id": "COMPONENT_ID"}
            ),
        )
        config = _make_base_config(hierarchy=hierarchy)
        incident = Incident(
            account_id="123456789012",
            region="us-east-1",
            app_name="auth-api",
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name="auth-api-errors",
            tags={},  # empty tags → nothing resolves
        )

        h, store = _build_handler(config, incident)
        h.process({})

        # deployment_metadata stays empty when nothing resolves
        assert incident.deployment_metadata == {}

    def test_handler_metadata_resolution_failure_does_not_raise(self, monkeypatch) -> None:
        """If resolution raises internally, the handler continues (best-effort)."""
        import relay.config.tag_mapping as tm

        config = _make_base_config()
        incident = self._make_incident_with_tags()

        def _boom(*a, **kw):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(tm, "resolve_deployment_metadata", _boom)

        h, store = _build_handler(config, incident)
        result = h.process({})

        # Handler must succeed despite the internal failure
        assert result["statusCode"] == 200
        # deployment_metadata was not set (exception swallowed)
        assert incident.deployment_metadata == {}
