"""Tests for Node-side noise suppression (routing.yaml `suppression:` block).

Covers:
  1. SuppressionConfig parsing (present, absent/optional, defaults).
  2. Decision logic: dedup (max=1), rate-limit (max=N), exempt severities,
     disabled passthrough, per-app/tag rule overrides of window/max.
  3. DynamoSuppressionStore: windowed atomic increment + TTL (moto).
  4. NodeHandler gate: suppressed re-fire skips dispatch/persist; SEV1 bypasses;
     disabled config is passthrough; store failure fails open.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
import pytest
import yaml

import relay.node.handler as handler_mod
from relay.config.schema import (
    EscalationConfig,
    RelayConfig,
    RoutingConfig,
    SuppressionConfig,
    SuppressionRule,
)
from relay.core.model import Incident, Severity, SignalSource
from tests.node.test_node_handler import (
    ESCALATION_YAML,
    FakeAlarmSource,
    FakeDispatcher,
    FakeEscalationEngine,
    FakeEscalationStateStore,
)

TABLE_NAME = "relay-suppression-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _incident(
    *,
    severity: Severity = Severity.SEV2,
    app_name: str = "test-app",
    alarm_name: str = "test-alarm",
    environment: str = "production",
    tags: dict[str, str] | None = None,
) -> Incident:
    return Incident(
        account_id="123456789012",
        region="us-east-1",
        app_name=app_name,
        severity=severity,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name=alarm_name,
        environment=environment,
        tags=tags or {},
    )


# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------


def test_routing_config_suppression_is_optional():
    cfg = RoutingConfig.model_validate(
        {
            "rules": [],
            "default_escalation_policy_id": "pol-standard",
            "default_streams": ["TEAM"],
        }
    )
    assert cfg.suppression is None


def test_suppression_block_parses_from_yaml():
    raw = """
rules: []
default_escalation_policy_id: pol-standard
default_streams: [TEAM]
suppression:
  enabled: true
  window_seconds: 600
  max_per_window: 3
  exempt_severities: [SEV1, SEV2]
  rules:
    - name: chatty-healthcheck
      alarm_name_prefix: hc-
      window_seconds: 60
      max_per_window: 1
"""
    cfg = RoutingConfig.model_validate(yaml.safe_load(raw))
    assert cfg.suppression is not None
    sup = cfg.suppression
    assert sup.enabled is True
    assert sup.window_seconds == 600
    assert sup.max_per_window == 3
    assert sup.exempt_severities == [Severity.SEV1, Severity.SEV2]
    assert len(sup.rules) == 1
    assert sup.rules[0].alarm_name_prefix == "hc-"
    assert sup.rules[0].max_per_window == 1


def test_suppression_defaults():
    """An empty block is disabled, dedup-tuned (max 1), 5-min window, SEV1 exempt."""
    sup = SuppressionConfig()
    assert sup.enabled is False
    assert sup.window_seconds == 300
    assert sup.max_per_window == 1
    assert sup.exempt_severities == [Severity.SEV1]
    assert sup.rules == []


# ---------------------------------------------------------------------------
# 2. Decision logic
# ---------------------------------------------------------------------------


def test_disabled_never_suppresses():
    sup = SuppressionConfig(enabled=False, max_per_window=1)
    assert sup.is_suppressed(_incident(), current_count=99) is False


def test_dedup_suppresses_after_first():
    """max_per_window=1: first fire passes, subsequent fires in window suppress."""
    sup = SuppressionConfig(enabled=True, max_per_window=1)
    assert sup.is_suppressed(_incident(), current_count=1) is False
    assert sup.is_suppressed(_incident(), current_count=2) is True


def test_rate_limit_suppresses_past_n():
    """max_per_window=3: first three pass, fourth+ suppress."""
    sup = SuppressionConfig(enabled=True, max_per_window=3)
    assert sup.is_suppressed(_incident(), current_count=1) is False
    assert sup.is_suppressed(_incident(), current_count=3) is False
    assert sup.is_suppressed(_incident(), current_count=4) is True


def test_exempt_severity_never_suppressed():
    sup = SuppressionConfig(enabled=True, max_per_window=1, exempt_severities=[Severity.SEV1])
    # SEV1 exempt even with a huge count
    assert sup.is_suppressed(_incident(severity=Severity.SEV1), current_count=100) is False
    # SEV2 not exempt
    assert sup.is_suppressed(_incident(severity=Severity.SEV2), current_count=2) is True


def test_limits_for_global_default():
    sup = SuppressionConfig(enabled=True, window_seconds=300, max_per_window=2)
    assert sup.limits_for(_incident()) == (300, 2)


def test_rule_override_window_and_max():
    sup = SuppressionConfig(
        enabled=True,
        window_seconds=300,
        max_per_window=5,
        rules=[
            SuppressionRule(alarm_name_prefix="hc-", window_seconds=60, max_per_window=1)
        ],
    )
    # matching alarm → override applies
    assert sup.limits_for(_incident(alarm_name="hc-ping")) == (60, 1)
    # non-matching alarm → global
    assert sup.limits_for(_incident(alarm_name="api-5xx")) == (300, 5)


def test_rule_override_partial_keeps_global_for_unset_field():
    """A rule that sets only max_per_window keeps the global window_seconds."""
    sup = SuppressionConfig(
        enabled=True,
        window_seconds=300,
        max_per_window=5,
        rules=[SuppressionRule(app_name="noisy", max_per_window=1)],
    )
    assert sup.limits_for(_incident(app_name="noisy")) == (300, 1)


def test_first_matching_rule_wins():
    sup = SuppressionConfig(
        enabled=True,
        rules=[
            SuppressionRule(app_name="x", max_per_window=1),
            SuppressionRule(app_name="x", max_per_window=9),
        ],
    )
    _, maximum = sup.limits_for(_incident(app_name="x"))
    assert maximum == 1


def test_rule_matches_tags_anded():
    rule = SuppressionRule(tags={"team": "core", "tier": "1"})
    assert rule.matches(_incident(tags={"team": "core", "tier": "1"})) is True
    assert rule.matches(_incident(tags={"team": "core"})) is False


# ---------------------------------------------------------------------------
# 3. DynamoSuppressionStore (moto)
# ---------------------------------------------------------------------------


@pytest.fixture
def dynamo_session():
    from moto import mock_aws

    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        table = ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield session


def _store(session, clock=None):
    from relay.adapters.aws.dynamo_stores import DynamoSuppressionStore

    return DynamoSuppressionStore(
        table_name=TABLE_NAME, boto3_session=session, clock=clock
    )


def test_store_increments_within_window(dynamo_session):
    """Repeated increments in the same window return a monotonically rising count."""
    fixed = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
    store = _store(dynamo_session, clock=lambda: fixed)
    assert store.increment_and_count("acct#app#alarm", 300) == 1
    assert store.increment_and_count("acct#app#alarm", 300) == 2
    assert store.increment_and_count("acct#app#alarm", 300) == 3


def test_store_resets_in_next_window(dynamo_session):
    """A fire in a later window starts a fresh counter (different bucket row)."""
    now = [datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)]
    store = _store(dynamo_session, clock=lambda: now[0])
    assert store.increment_and_count("k", 300) == 1
    assert store.increment_and_count("k", 300) == 2
    # jump past the window boundary → new bucket
    now[0] = datetime(2026, 6, 24, 12, 10, 0, tzinfo=UTC)
    assert store.increment_and_count("k", 300) == 1


def test_store_separates_distinct_keys(dynamo_session):
    fixed = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
    store = _store(dynamo_session, clock=lambda: fixed)
    assert store.increment_and_count("acct#app-a#alarm", 300) == 1
    assert store.increment_and_count("acct#app-b#alarm", 300) == 1


def test_store_writes_ttl(dynamo_session):
    fixed = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
    store = _store(dynamo_session, clock=lambda: fixed)
    store.increment_and_count("k", 300)
    epoch = int(fixed.timestamp())
    bucket = epoch // 300
    item = (
        dynamo_session.resource("dynamodb")
        .Table(TABLE_NAME)
        .get_item(Key={"pk": f"SUPP#k#{bucket}", "sk": "STATE"})["Item"]
    )
    assert int(item["ttl"]) == (bucket + 2) * 300
    assert int(item["count"]) == 1


# ---------------------------------------------------------------------------
# 4. NodeHandler gate (end-to-end through _handle_alarm)
# ---------------------------------------------------------------------------


def _routing_with_suppression(**kwargs) -> RoutingConfig:
    sup = SuppressionConfig(**kwargs)
    return RoutingConfig(
        rules=[],
        default_escalation_policy_id="pol-default",
        default_streams=["TEAM"],
        suppression=sup,
    )


def _build_handler(monkeypatch, *, routing, suppression_store, incident):
    """Build a NodeHandler with all AWS collaborators faked, plus injected
    suppression store and a pre-built incident from the alarm source.

    Reuses the env-var + fake patterns from test_node_handler.
    """
    for k, v in {
        "RELAY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:t",
        "RELAY_HUB_EVENT_BUS_ARN": "arn:aws:events:us-east-1:123456789012:event-bus/hub",
        "RELAY_GITLAB_REPO": "12345",
        "RELAY_TABLE_NAME": "relay-test-table",
        "RELAY_ACCOUNT_ID": "123456789012",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)

    escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
    cfg = RelayConfig(
        escalation=escalation, routing=routing, loaded_at=datetime.now(UTC)
    )

    class _Loader:
        def get(self):
            return cfg

        def refresh(self):
            return cfg

    dispatch_spy = MagicMock(wraps=FakeDispatcher)
    monkeypatch.setattr(handler_mod, "DualStreamDispatcher", dispatch_spy)

    incident_store = MagicMock()
    h = handler_mod.NodeHandler(
        _config_loader=_Loader(),
        _alarm_source=FakeAlarmSource(incident),
        _notifier=MagicMock(),
        _transport=MagicMock(),
        _contact_store=MagicMock(),
        _incident_store=incident_store,
        _escalation_state_store=FakeEscalationStateStore(),
        _suppression_store=suppression_store,
        _escalation_engine=FakeEscalationEngine(),
        _clock=lambda: 0.0,
    )
    return h, incident_store, dispatch_spy


def test_handler_suppresses_second_fire(monkeypatch):
    """dedup: first fire dispatches; second (count=2) is suppressed."""
    counts = MagicMock()
    counts.increment_and_count.side_effect = [1, 2]
    routing = _routing_with_suppression(enabled=True, max_per_window=1)
    h, incident_store, dispatch_spy = _build_handler(
        monkeypatch,
        routing=routing,
        suppression_store=counts,
        incident=_incident(severity=Severity.SEV2),
    )

    first = h._handle_alarm({})
    assert first.get("suppressed") is not True
    assert dispatch_spy.called

    dispatch_spy.reset_mock()
    second = h._handle_alarm({})
    assert second["suppressed"] is True
    assert not dispatch_spy.called  # no page on the suppressed fire


def test_handler_sev1_bypasses_suppression(monkeypatch):
    """SEV1 is exempt: even a high count never suppresses, store not consulted."""
    counts = MagicMock()
    counts.increment_and_count.side_effect = AssertionError("should not be called")
    routing = _routing_with_suppression(enabled=True, max_per_window=1)
    h, incident_store, dispatch_spy = _build_handler(
        monkeypatch,
        routing=routing,
        suppression_store=counts,
        incident=_incident(severity=Severity.SEV1),
    )

    result = h._handle_alarm({})
    assert result.get("suppressed") is not True
    assert dispatch_spy.called


def test_handler_disabled_is_passthrough(monkeypatch):
    """Disabled suppression never touches the store and always dispatches."""
    counts = MagicMock()
    counts.increment_and_count.side_effect = AssertionError("should not be called")
    routing = _routing_with_suppression(enabled=False)
    h, incident_store, dispatch_spy = _build_handler(
        monkeypatch,
        routing=routing,
        suppression_store=counts,
        incident=_incident(severity=Severity.SEV3),
    )

    result = h._handle_alarm({})
    assert result.get("suppressed") is not True
    assert dispatch_spy.called


def test_handler_store_failure_fails_open(monkeypatch):
    """A store error must not suppress (fail-open) — the page still goes out."""
    counts = MagicMock()
    counts.increment_and_count.side_effect = RuntimeError("dynamo down")
    routing = _routing_with_suppression(enabled=True, max_per_window=1)
    h, incident_store, dispatch_spy = _build_handler(
        monkeypatch,
        routing=routing,
        suppression_store=counts,
        incident=_incident(severity=Severity.SEV2),
    )

    result = h._handle_alarm({})
    assert result.get("suppressed") is not True
    assert dispatch_spy.called
