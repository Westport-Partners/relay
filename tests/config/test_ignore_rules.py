"""Tests for the ignore-rules feature: IgnoreRule / IgnoreConfig schema models
and DynamoIgnoreRuleStore.

Covers:
  1. IgnoreRule.matches — exact alarm_name, prefix, account_id, app_name,
     environment (string + list), tags AND-logic, omitted-field catch-all.
  2. IgnoreConfig.first_match — ordering, disabled config, disabled rule skipping.
  3. RoutingConfig.ignore field parses from YAML / is optional.
  4. DynamoIgnoreRuleStore — put/get round-trip, list joins counters, record_trigger
     atomically increments, delete cascades both items.

Uses moto to mock DynamoDB — no real AWS calls are made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
import yaml

import relay.node.handler as handler_mod
from relay.config.schema import (
    EscalationConfig,
    IgnoreConfig,
    IgnoreRule,
    RelayConfig,
    RoutingConfig,
)
from relay.core.model import Incident, Severity, SignalSource
from tests.node.test_node_handler import (
    ESCALATION_YAML,
    FakeAlarmSource,
    FakeDispatcher,
    FakeEscalationEngine,
    FakeEscalationStateStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TABLE_NAME = "relay-ignore-test"


def _incident(
    *,
    account_id: str = "123456789012",
    app_name: str = "my-app",
    alarm_name: str = "my-app-5xx",
    environment: str = "production",
    tags: dict[str, str] | None = None,
) -> Incident:
    return Incident(
        account_id=account_id,
        region="us-east-1",
        app_name=app_name,
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name=alarm_name,
        environment=environment,
        tags=tags or {},
    )


# ---------------------------------------------------------------------------
# 1. IgnoreRule.matches — individual field checks
# ---------------------------------------------------------------------------


def test_empty_rule_matches_anything():
    """A rule with no match fields set matches every incident."""
    rule = IgnoreRule()
    assert rule.matches(_incident()) is True


@pytest.mark.parametrize(
    "rule, incident_kwargs, expected",
    [
        # alarm_name exact — positive
        pytest.param(
            IgnoreRule(alarm_name="my-app-5xx"),
            {"alarm_name": "my-app-5xx"},
            True,
            id="alarm_name_exact_match",
        ),
        # alarm_name exact — negative
        pytest.param(
            IgnoreRule(alarm_name="my-app-5xx"),
            {"alarm_name": "my-app-4xx"},
            False,
            id="alarm_name_exact_no_match",
        ),
        # alarm_name exact does NOT prefix-match
        pytest.param(
            IgnoreRule(alarm_name="my-app"),
            {"alarm_name": "my-app-5xx"},
            False,
            id="alarm_name_exact_not_prefix",
        ),
        # alarm_name_prefix — positive (two sub-cases kept as separate ids)
        pytest.param(
            IgnoreRule(alarm_name_prefix="my-app-"),
            {"alarm_name": "my-app-5xx"},
            True,
            id="alarm_name_prefix_match_5xx",
        ),
        pytest.param(
            IgnoreRule(alarm_name_prefix="my-app-"),
            {"alarm_name": "my-app-latency"},
            True,
            id="alarm_name_prefix_match_latency",
        ),
        # alarm_name_prefix — negative
        pytest.param(
            IgnoreRule(alarm_name_prefix="my-app-"),
            {"alarm_name": "other-app-5xx"},
            False,
            id="alarm_name_prefix_no_match",
        ),
        # empty prefix matches any alarm name
        pytest.param(
            IgnoreRule(alarm_name_prefix=""),
            {},
            True,
            id="alarm_name_prefix_empty_matches_all",
        ),
        # account_id — positive
        pytest.param(
            IgnoreRule(account_id="123456789012"),
            {"account_id": "123456789012"},
            True,
            id="account_id_match",
        ),
        # account_id — negative
        pytest.param(
            IgnoreRule(account_id="123456789012"),
            {"account_id": "999999999999"},
            False,
            id="account_id_no_match",
        ),
        # app_name — positive
        pytest.param(
            IgnoreRule(app_name="my-app"),
            {"app_name": "my-app"},
            True,
            id="app_name_match",
        ),
        # app_name — negative
        pytest.param(
            IgnoreRule(app_name="my-app"),
            {"app_name": "other-app"},
            False,
            id="app_name_no_match",
        ),
        # environment string — positive
        pytest.param(
            IgnoreRule(environment="production"),
            {"environment": "production"},
            True,
            id="environment_string_match",
        ),
        # environment string — negative
        pytest.param(
            IgnoreRule(environment="production"),
            {"environment": "staging"},
            False,
            id="environment_string_no_match",
        ),
        # environment list — positive (two values)
        pytest.param(
            IgnoreRule(environment=["dev", "test", "preprod"]),
            {"environment": "dev"},
            True,
            id="environment_list_match_dev",
        ),
        pytest.param(
            IgnoreRule(environment=["dev", "test", "preprod"]),
            {"environment": "preprod"},
            True,
            id="environment_list_match_preprod",
        ),
        # environment list — negative
        pytest.param(
            IgnoreRule(environment=["dev", "test", "preprod"]),
            {"environment": "production"},
            False,
            id="environment_list_no_match",
        ),
        # environment None matches any
        pytest.param(
            IgnoreRule(environment=None),
            {"environment": "production"},
            True,
            id="environment_none_matches_production",
        ),
        pytest.param(
            IgnoreRule(environment=None),
            {"environment": "dev"},
            True,
            id="environment_none_matches_dev",
        ),
        # tags — all present + correct
        pytest.param(
            IgnoreRule(tags={"team": "core", "tier": "1"}),
            {"tags": {"team": "core", "tier": "1"}},
            True,
            id="tags_all_present",
        ),
        # tags — missing one tag
        pytest.param(
            IgnoreRule(tags={"team": "core", "tier": "1"}),
            {"tags": {"team": "core"}},
            False,
            id="tags_missing_tier",
        ),
        # tags — wrong value
        pytest.param(
            IgnoreRule(tags={"team": "core", "tier": "1"}),
            {"tags": {"team": "core", "tier": "2"}},
            False,
            id="tags_wrong_value",
        ),
        # tags empty matches anything
        pytest.param(
            IgnoreRule(tags={}),
            {},
            True,
            id="tags_empty_matches_default",
        ),
        pytest.param(
            IgnoreRule(tags={}),
            {"tags": {"anything": "goes"}},
            True,
            id="tags_empty_matches_any_tags",
        ),
        # omitted fields are wildcards
        pytest.param(
            IgnoreRule(app_name="my-app"),
            {"app_name": "my-app", "alarm_name": "anything", "environment": "dev"},
            True,
            id="omitted_fields_wildcard_match",
        ),
        pytest.param(
            IgnoreRule(app_name="my-app"),
            {"app_name": "other-app"},
            False,
            id="omitted_fields_wildcard_no_match",
        ),
    ],
)
def test_single_field_match(
    rule: IgnoreRule, incident_kwargs: dict[str, Any], expected: bool
):
    assert rule.matches(_incident(**incident_kwargs)) is expected


def test_and_logic_all_fields_must_match():
    """All specified fields must match; one mismatch rejects the rule."""
    rule = IgnoreRule(
        app_name="my-app",
        alarm_name_prefix="my-app-",
        environment="production",
        tags={"tier": "1"},
    )
    # All match
    assert rule.matches(_incident(
        app_name="my-app",
        alarm_name="my-app-5xx",
        environment="production",
        tags={"tier": "1"},
    )) is True
    # Wrong app_name
    assert rule.matches(_incident(
        app_name="other-app",
        alarm_name="my-app-5xx",
        environment="production",
        tags={"tier": "1"},
    )) is False
    # Wrong environment
    assert rule.matches(_incident(
        app_name="my-app",
        alarm_name="my-app-5xx",
        environment="staging",
        tags={"tier": "1"},
    )) is False


def test_alarm_name_and_prefix_both_set_and_logic():
    """When both alarm_name and alarm_name_prefix are set, both must pass."""
    rule = IgnoreRule(alarm_name="my-app-5xx", alarm_name_prefix="my-app-")
    # alarm_name exact matches AND prefix matches
    assert rule.matches(_incident(alarm_name="my-app-5xx")) is True
    # prefix matches but alarm_name doesn't
    assert rule.matches(_incident(alarm_name="my-app-latency")) is False


# ---------------------------------------------------------------------------
# 2. IgnoreConfig.first_match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg, incident_kwargs, expected_name",
    [
        pytest.param(
            IgnoreConfig(
                enabled=True,
                rules=[
                    IgnoreRule(name="rule-a", alarm_name="alarm-a"),
                    IgnoreRule(name="rule-b", alarm_name="alarm-b"),
                ],
            ),
            {"alarm_name": "alarm-b"},
            "rule-b",
            id="returns_first_matching_rule",
        ),
        pytest.param(
            IgnoreConfig(
                enabled=True,
                rules=[
                    IgnoreRule(name="first", app_name="my-app"),
                    IgnoreRule(name="second", app_name="my-app"),
                ],
            ),
            {"app_name": "my-app"},
            "first",
            id="ordering_first_wins",
        ),
        pytest.param(
            IgnoreConfig(
                enabled=True,
                rules=[IgnoreRule(alarm_name="specific-alarm")],
            ),
            {"alarm_name": "different-alarm"},
            None,
            id="no_match_returns_none",
        ),
        pytest.param(
            IgnoreConfig(
                enabled=False,
                rules=[IgnoreRule()],  # catch-all rule — would match if config were enabled
            ),
            {},
            None,
            id="disabled_config_returns_none",
        ),
        pytest.param(
            IgnoreConfig(
                enabled=True,
                rules=[
                    IgnoreRule(name="off", alarm_name="my-app-5xx", enabled=False),
                    IgnoreRule(name="on", alarm_name="my-app-5xx", enabled=True),
                ],
            ),
            {"alarm_name": "my-app-5xx"},
            "on",
            id="skips_disabled_rules",
        ),
        pytest.param(
            IgnoreConfig(
                enabled=True,
                rules=[
                    IgnoreRule(enabled=False),
                    IgnoreRule(enabled=False),
                ],
            ),
            {},
            None,
            id="all_disabled_returns_none",
        ),
        pytest.param(
            IgnoreConfig(enabled=True, rules=[]),
            {},
            None,
            id="empty_rules_returns_none",
        ),
    ],
)
def test_first_match(
    cfg: IgnoreConfig, incident_kwargs: dict[str, Any], expected_name: str | None
):
    result = cfg.first_match(_incident(**incident_kwargs))
    if expected_name is None:
        assert result is None
    else:
        assert result is not None
        assert result.name == expected_name


# ---------------------------------------------------------------------------
# 3. RoutingConfig.ignore field
# ---------------------------------------------------------------------------


def test_routing_config_ignore_is_optional():
    cfg = RoutingConfig.model_validate(
        {
            "rules": [],
            "default_escalation_policy_id": "pol-standard",
            "default_streams": ["TEAM"],
        }
    )
    assert cfg.ignore is None


def test_routing_config_ignore_parses_from_yaml():
    raw = """
rules: []
default_escalation_policy_id: pol-standard
default_streams: [TEAM]
ignore:
  enabled: true
  rules:
    - name: drop-health-checks
      alarm_name_prefix: hc-
      environment: production
      note: "Health check alarms are never actionable"
"""
    cfg = RoutingConfig.model_validate(yaml.safe_load(raw))
    assert cfg.ignore is not None
    assert cfg.ignore.enabled is True
    assert len(cfg.ignore.rules) == 1
    rule = cfg.ignore.rules[0]
    assert rule.name == "drop-health-checks"
    assert rule.alarm_name_prefix == "hc-"
    assert rule.environment == "production"
    assert rule.note == "Health check alarms are never actionable"


def test_routing_config_ignore_disabled():
    raw = """
rules: []
default_escalation_policy_id: pol-standard
default_streams: [TEAM]
ignore:
  enabled: false
  rules:
    - alarm_name_prefix: hc-
"""
    cfg = RoutingConfig.model_validate(yaml.safe_load(raw))
    assert cfg.ignore is not None
    assert cfg.ignore.enabled is False


# ---------------------------------------------------------------------------
# 3b. Shipped default ignore rules (the real config files)
# ---------------------------------------------------------------------------


def test_shipped_config_drops_aws_autoscaling_alarms():
    """The shipped routing template default-ignores AWS autoscaling alarms so a
    fresh install never pages on its own (or the monitored account's) scaling
    activity — including Relay's own hub service scaling down when idle.

    The alarm name is the real shape AWS generates for an ECS target-tracking
    policy, which also contains a "/" (the tile-detail route must handle that).

    Asserts on ``routing.example.yaml`` (the version-controlled artifact); the
    live ``routing.yaml`` is gitignored and absent in a fresh checkout.
    """
    with open("config/routing.example.yaml") as fh:
        cfg = RoutingConfig.model_validate(yaml.safe_load(fh))
    assert cfg.ignore is not None and cfg.ignore.enabled is True

    real_alarm = (
        "TargetTracking-service/relay-hub/"
        "relay-hub-AlarmLow-9b855cda-34a1-4b5b-ba84-5896b06c9fff"
    )
    matched = cfg.ignore.first_match(_incident(alarm_name=real_alarm))
    assert matched is not None
    assert matched.name == "aws-autoscaling-target-tracking"

    # A genuine app alarm must still pass through (not over-broad).
    assert cfg.ignore.first_match(_incident(alarm_name="prod-checkout-5xx")) is None


# ---------------------------------------------------------------------------
# 4. DynamoIgnoreRuleStore (moto)
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
    from relay.adapters.aws.dynamo_stores import DynamoIgnoreRuleStore

    return DynamoIgnoreRuleStore(
        table_name=TABLE_NAME, boto3_session=session, clock=clock
    )


class TestDynamoIgnoreRuleStore:
    """Round-trip and counter tests for DynamoIgnoreRuleStore."""

    def test_put_rule_returns_id(self, dynamo_session):
        store = _store(dynamo_session)
        rule = IgnoreRule(name="test-rule", alarm_name_prefix="hc-")
        rule_id = store.put_rule(rule)
        assert rule_id == "test-rule"  # defaults to rule.name when no explicit id

    def test_put_rule_explicit_id(self, dynamo_session):
        store = _store(dynamo_session)
        rule = IgnoreRule(alarm_name="specific-alarm")
        rule_id = store.put_rule(rule, rule_id="my-explicit-id")
        assert rule_id == "my-explicit-id"

    def test_put_and_get_round_trip(self, dynamo_session):
        store = _store(dynamo_session)
        rule = IgnoreRule(
            name="hc-drop",
            alarm_name_prefix="hc-",
            environment="production",
            note="drop health check alarms",
            enabled=True,
            created_by="alice",
        )
        rule_id = store.put_rule(rule, rule_id="hc-drop-rule")
        loaded = store.get_rule(rule_id)

        assert loaded is not None
        assert loaded.alarm_name_prefix == "hc-"
        assert loaded.environment == "production"
        assert loaded.note == "drop health check alarms"
        assert loaded.enabled is True
        assert loaded.created_by == "alice"

    def test_get_rule_missing_returns_none(self, dynamo_session):
        store = _store(dynamo_session)
        assert store.get_rule("nonexistent-rule-id") is None

    def test_put_rule_overwrites_existing(self, dynamo_session):
        store = _store(dynamo_session)
        rule = IgnoreRule(alarm_name_prefix="old-prefix")
        store.put_rule(rule, rule_id="overwrite-me")

        updated = IgnoreRule(alarm_name_prefix="new-prefix", note="updated")
        store.put_rule(updated, rule_id="overwrite-me")

        loaded = store.get_rule("overwrite-me")
        assert loaded is not None
        assert loaded.alarm_name_prefix == "new-prefix"
        assert loaded.note == "updated"

    def test_list_rules_returns_all(self, dynamo_session):
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(alarm_name="alarm-a"), rule_id="list-rule-a")
        store.put_rule(IgnoreRule(alarm_name="alarm-b"), rule_id="list-rule-b")

        results = store.list_rules()
        ids = [r[0] for r in results]
        assert "list-rule-a" in ids
        assert "list-rule-b" in ids

    def test_list_rules_trigger_count_zero_when_no_triggers(self, dynamo_session):
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(alarm_name="no-trigger"), rule_id="no-trigger-rule")

        results = store.list_rules()
        rule_map = {r[0]: r[2] for r in results}
        assert rule_map.get("no-trigger-rule", 0) == 0

    def test_list_rules_joins_trigger_counts(self, dynamo_session):
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(alarm_name="counted"), rule_id="counted-rule")
        store.record_trigger("counted-rule")
        store.record_trigger("counted-rule")
        store.record_trigger("counted-rule")

        results = store.list_rules()
        rule_map = {r[0]: r[2] for r in results}
        assert rule_map["counted-rule"] == 3

    def test_list_rules_sorted_by_rule_id(self, dynamo_session):
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(), rule_id="zzz-last")
        store.put_rule(IgnoreRule(), rule_id="aaa-first")
        store.put_rule(IgnoreRule(), rule_id="mmm-middle")

        results = store.list_rules()
        ids = [r[0] for r in results if r[0] in ("zzz-last", "aaa-first", "mmm-middle")]
        assert ids == sorted(ids)

    def test_record_trigger_increments_atomically(self, dynamo_session):
        fixed_time = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        store = _store(dynamo_session, clock=lambda: fixed_time)
        store.put_rule(IgnoreRule(), rule_id="counter-rule")

        assert store.record_trigger("counter-rule") == 1
        assert store.record_trigger("counter-rule") == 2
        assert store.record_trigger("counter-rule") == 3

    def test_record_trigger_sets_last_triggered_at(self, dynamo_session):
        fixed_time = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        store = _store(dynamo_session, clock=lambda: fixed_time)
        store.put_rule(IgnoreRule(), rule_id="ts-rule")
        store.record_trigger("ts-rule")

        # Read the raw COUNTER item to check last_triggered_at
        table = dynamo_session.resource("dynamodb").Table(TABLE_NAME)
        item = table.get_item(
            Key={"pk": "IGNORE#ts-rule", "sk": "COUNTER"}
        ).get("Item")
        assert item is not None
        assert item["last_triggered_at"] == fixed_time.isoformat()
        assert int(item["trigger_count"]) == 1

    def test_delete_rule_removes_meta_item(self, dynamo_session):
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(alarm_name="to-delete"), rule_id="delete-me")
        assert store.get_rule("delete-me") is not None

        store.delete_rule("delete-me")
        assert store.get_rule("delete-me") is None

    def test_delete_rule_cascades_counter(self, dynamo_session):
        """delete_rule removes both META and COUNTER items."""
        store = _store(dynamo_session)
        store.put_rule(IgnoreRule(), rule_id="cascade-delete")
        store.record_trigger("cascade-delete")

        # Verify counter exists before delete
        table = dynamo_session.resource("dynamodb").Table(TABLE_NAME)
        counter_before = table.get_item(
            Key={"pk": "IGNORE#cascade-delete", "sk": "COUNTER"}
        ).get("Item")
        assert counter_before is not None

        store.delete_rule("cascade-delete")

        # Both rows gone
        assert store.get_rule("cascade-delete") is None
        counter_after = table.get_item(
            Key={"pk": "IGNORE#cascade-delete", "sk": "COUNTER"}
        ).get("Item")
        assert counter_after is None

    def test_delete_rule_idempotent(self, dynamo_session):
        """Deleting a non-existent rule is a no-op (does not raise)."""
        store = _store(dynamo_session)
        # Should not raise
        store.delete_rule("nonexistent-rule")

    def test_put_rule_uuid_when_no_name(self, dynamo_session):
        """put_rule generates a UUID when rule.name is None and rule_id is None."""
        store = _store(dynamo_session)
        rule = IgnoreRule(alarm_name="anon-alarm")  # name=None
        rule_id = store.put_rule(rule)
        # Should be a non-empty string (UUID4)
        assert rule_id
        assert len(rule_id) == 36  # UUID4 format: 8-4-4-4-12
        assert store.get_rule(rule_id) is not None

    def test_round_trip_preserves_tags_and_environment_list(self, dynamo_session):
        """Complex fields (tags dict, environment list) survive JSON round-trip."""
        store = _store(dynamo_session)
        rule = IgnoreRule(
            tags={"team": "core", "tier": "1"},
            environment=["dev", "test"],
        )
        store.put_rule(rule, rule_id="complex-rule")
        loaded = store.get_rule("complex-rule")

        assert loaded is not None
        assert loaded.tags == {"team": "core", "tier": "1"}
        assert loaded.environment == ["dev", "test"]


# ---------------------------------------------------------------------------
# 5. NodeHandler gate (end-to-end through _handle_alarm)
# ---------------------------------------------------------------------------

# Required env vars — the autouse fixture in test_node_handler only applies
# within that module; we set them explicitly here via a module-level fixture.
_HANDLER_ENV = {
    "RELAY_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:t",
    "RELAY_HUB_EVENT_BUS_ARN": "arn:aws:events:us-east-1:123456789012:event-bus/hub",
    "RELAY_GITLAB_REPO": "12345",
    "RELAY_GITLAB_SECRET_NAME": "relay/gitlab-token",
    "RELAY_TABLE_NAME": "relay-test-table",
    "RELAY_ACCOUNT_ID": "123456789012",
    "RELAY_TIMEOUT_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:relay-node",
    "RELAY_SCHEDULER_ROLE_ARN": "arn:aws:iam::123456789012:role/relay-scheduler-role",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
}


class FakeIgnoreRuleStore:
    """Minimal fake DynamoIgnoreRuleStore for NodeHandler injection."""

    def __init__(self, rules: list[tuple[str, IgnoreRule]] | None = None) -> None:
        # rules is a list of (rule_id, IgnoreRule); trigger count always 0.
        self._rules: list[tuple[str, IgnoreRule, int]] = [
            (rid, rule, 0) for rid, rule in (rules or [])
        ]
        self.triggered: list[str] = []
        self._list_raises: Exception | None = None
        self._record_raises: Exception | None = None

    def list_rules(self) -> list[tuple[str, IgnoreRule, int]]:
        if self._list_raises is not None:
            raise self._list_raises
        return list(self._rules)

    def record_trigger(self, rule_id: str) -> int:
        if self._record_raises is not None:
            raise self._record_raises
        self.triggered.append(rule_id)
        return len(self.triggered)


def _build_ignore_handler(
    monkeypatch,
    *,
    ignore_rule_store: FakeIgnoreRuleStore,
    incident: Incident,
    suppression_store: MagicMock | None = None,
) -> tuple[handler_mod.NodeHandler, MagicMock, MagicMock]:
    """Build a NodeHandler with all AWS collaborators faked plus injected stores.

    Returns (handler, incident_store_mock, dispatch_spy).
    """
    for k, v in _HANDLER_ENV.items():
        monkeypatch.setenv(k, v)

    escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
    routing = RoutingConfig(
        rules=[],
        default_escalation_policy_id="pol-default",
        default_streams=["TEAM"],
    )
    cfg = RelayConfig(escalation=escalation, routing=routing, loaded_at=datetime.now(UTC))

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
        _suppression_store=suppression_store or MagicMock(),
        _ignore_rule_store=ignore_rule_store,
        _routing_rule_store=MagicMock(),
        _escalation_engine=FakeEscalationEngine(),
        _clock=lambda: 0.0,
    )
    return h, incident_store, dispatch_spy


class TestNodeHandlerIgnoreGate:

    def _incident(
        self,
        alarm_name: str = "test-alarm-high-error",
        app_name: str = "test-app",
        environment: str = "prod",
    ) -> Incident:
        return Incident(
            account_id="123456789012",
            region="us-east-1",
            app_name=app_name,
            severity=Severity.SEV2,
            signal_source=SignalSource.CLOUDWATCH_ALARM,
            alarm_name=alarm_name,
            environment=environment,
        )

    def test_matching_rule_ignored(self, monkeypatch):
        """An alarm matching an enabled ignore rule: ignored=True, not persisted, not paged, trigger recorded."""
        rule = IgnoreRule(name="drop-test", alarm_name="test-alarm-high-error")
        store = FakeIgnoreRuleStore(rules=[("rule-001", rule)])
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        assert result["ignored"] is True
        assert result["ignore_rule_id"] == "rule-001"
        assert result["statusCode"] == 200
        assert result["team_ok"] is False
        assert result["central_ok"] is False
        incident_store.put_incident.assert_not_called()
        assert not dispatch_spy.called
        assert store.triggered == ["rule-001"]

    def test_non_matching_rule_proceeds_normally(self, monkeypatch):
        """An alarm that does NOT match any ignore rule must be dispatched."""
        rule = IgnoreRule(alarm_name="other-alarm")  # does not match
        store = FakeIgnoreRuleStore(rules=[("rule-005", rule)])
        inc = self._incident(alarm_name="test-alarm-high-error")

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        assert result.get("ignored") is not True
        assert dispatch_spy.called
        # put_incident is called at least once (step 5 initial persist + step 6
        # timeline persist after escalation start).
        assert incident_store.put_incident.call_count >= 1

    def test_empty_rule_list_proceeds_normally(self, monkeypatch):
        """With no ignore rules the alarm proceeds through the full pipeline."""
        store = FakeIgnoreRuleStore(rules=[])
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        assert result.get("ignored") is not True
        assert dispatch_spy.called

    def test_list_rules_failure_fails_open(self, monkeypatch):
        """If list_rules raises, the alarm is NOT ignored — fail-open."""
        store = FakeIgnoreRuleStore()
        store._list_raises = RuntimeError("DynamoDB unavailable")
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        # Alarm proceeds — no ignore
        assert result.get("ignored") is not True
        assert dispatch_spy.called

    def test_record_trigger_failure_does_not_abort(self, monkeypatch):
        """A record_trigger failure must be swallowed; the ignore still completes."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        store = FakeIgnoreRuleStore(rules=[("rule-006", rule)])
        store._record_raises = RuntimeError("counter table throttled")
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        # Should still be ignored even though record_trigger raised
        assert result["ignored"] is True
        incident_store.put_incident.assert_not_called()

    def test_ignore_takes_precedence_over_suppression(self, monkeypatch):
        """An alarm matching both an ignore rule and suppression returns ignored, not suppressed."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        ignore_store = FakeIgnoreRuleStore(rules=[("rule-007", rule)])

        # Suppression store that would suppress if reached
        suppression_store = MagicMock()
        suppression_store.increment_and_count.return_value = 999  # would suppress

        inc = self._incident()

        # Build config with suppression enabled
        for k, v in _HANDLER_ENV.items():
            monkeypatch.setenv(k, v)

        from relay.config.schema import SuppressionConfig

        escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
        routing = RoutingConfig(
            rules=[],
            default_escalation_policy_id="pol-default",
            default_streams=["TEAM"],
            suppression=SuppressionConfig(enabled=True, max_per_window=1),
        )
        cfg = RelayConfig(escalation=escalation, routing=routing, loaded_at=datetime.now(UTC))

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
            _alarm_source=FakeAlarmSource(inc),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=incident_store,
            _escalation_state_store=FakeEscalationStateStore(),
            _suppression_store=suppression_store,
            _ignore_rule_store=ignore_store,
            _routing_rule_store=MagicMock(),
            _escalation_engine=FakeEscalationEngine(),
            _clock=lambda: 0.0,
        )
        result = h._handle_alarm({})

        # Ignore wins — returns ignored not suppressed
        assert result["ignored"] is True
        assert result.get("suppressed") is not True
        incident_store.put_incident.assert_not_called()

    def test_disabled_rule_is_skipped(self, monkeypatch):
        """A rule with enabled=False must NOT match even if all other fields match."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error", enabled=False)
        store = FakeIgnoreRuleStore(rules=[("rule-008", rule)])
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        assert result.get("ignored") is not True
        assert dispatch_spy.called

    def test_ttl_cache_avoids_repeated_list_calls(self, monkeypatch):
        """Within the TTL window, list_rules must only be called once."""
        for k, v in _HANDLER_ENV.items():
            monkeypatch.setenv(k, v)
        # Short TTL of 30s; clock always returns 0.0 so cache never expires
        monkeypatch.setenv("RELAY_IGNORE_RULES_TTL_SECONDS", "30")

        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        store = MagicMock()
        store.list_rules.return_value = [("rule-cache", rule, 0)]
        store.record_trigger.return_value = 1

        escalation = EscalationConfig.model_validate(yaml.safe_load(ESCALATION_YAML))
        routing = RoutingConfig(
            rules=[],
            default_escalation_policy_id="pol-default",
            default_streams=["TEAM"],
        )
        cfg = RelayConfig(escalation=escalation, routing=routing, loaded_at=datetime.now(UTC))

        class _Loader:
            def get(self):
                return cfg

            def refresh(self):
                return cfg

        dispatch_spy = MagicMock(wraps=FakeDispatcher)
        monkeypatch.setattr(handler_mod, "DualStreamDispatcher", dispatch_spy)

        inc = self._incident()
        h = handler_mod.NodeHandler(
            _config_loader=_Loader(),
            _alarm_source=FakeAlarmSource(inc),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=MagicMock(),
            _escalation_state_store=FakeEscalationStateStore(),
            _suppression_store=MagicMock(),
            _ignore_rule_store=store,
            _routing_rule_store=MagicMock(),
            _escalation_engine=FakeEscalationEngine(),
            _clock=lambda: 0.0,  # clock frozen: TTL never expires after first load
        )

        # Call _handle_alarm twice — list_rules should only be called once
        # (first alarm loads the cache; second reuses it)
        h._handle_alarm({})
        h._handle_alarm({})

        assert store.list_rules.call_count == 1, (
            "list_rules must only be called once within the TTL window"
        )
