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
from tests.test_node_handler import (
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


def test_alarm_name_exact_match():
    rule = IgnoreRule(alarm_name="my-app-5xx")
    assert rule.matches(_incident(alarm_name="my-app-5xx")) is True
    assert rule.matches(_incident(alarm_name="my-app-4xx")) is False


def test_alarm_name_exact_does_not_prefix_match():
    """alarm_name is an EXACT match; 'my-app' should not match 'my-app-5xx'."""
    rule = IgnoreRule(alarm_name="my-app")
    assert rule.matches(_incident(alarm_name="my-app-5xx")) is False


def test_alarm_name_prefix_match():
    rule = IgnoreRule(alarm_name_prefix="my-app-")
    assert rule.matches(_incident(alarm_name="my-app-5xx")) is True
    assert rule.matches(_incident(alarm_name="my-app-latency")) is True
    assert rule.matches(_incident(alarm_name="other-app-5xx")) is False


def test_alarm_name_prefix_empty_matches_all():
    """Empty prefix matches any alarm name (startswith('') is always True)."""
    rule = IgnoreRule(alarm_name_prefix="")
    assert rule.matches(_incident()) is True


def test_account_id_exact_match():
    rule = IgnoreRule(account_id="123456789012")
    assert rule.matches(_incident(account_id="123456789012")) is True
    assert rule.matches(_incident(account_id="999999999999")) is False


def test_app_name_exact_match():
    rule = IgnoreRule(app_name="my-app")
    assert rule.matches(_incident(app_name="my-app")) is True
    assert rule.matches(_incident(app_name="other-app")) is False


def test_environment_string_match():
    rule = IgnoreRule(environment="production")
    assert rule.matches(_incident(environment="production")) is True
    assert rule.matches(_incident(environment="staging")) is False


def test_environment_list_match():
    rule = IgnoreRule(environment=["dev", "test", "preprod"])
    assert rule.matches(_incident(environment="dev")) is True
    assert rule.matches(_incident(environment="preprod")) is True
    assert rule.matches(_incident(environment="production")) is False


def test_environment_none_matches_any():
    rule = IgnoreRule(environment=None)
    assert rule.matches(_incident(environment="production")) is True
    assert rule.matches(_incident(environment="dev")) is True


def test_tags_all_must_be_present():
    rule = IgnoreRule(tags={"team": "core", "tier": "1"})
    assert rule.matches(_incident(tags={"team": "core", "tier": "1"})) is True
    assert rule.matches(_incident(tags={"team": "core"})) is False  # missing tier
    assert rule.matches(_incident(tags={"team": "core", "tier": "2"})) is False  # wrong value


def test_tags_empty_matches_anything():
    rule = IgnoreRule(tags={})
    assert rule.matches(_incident()) is True
    assert rule.matches(_incident(tags={"anything": "goes"})) is True


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


def test_omitted_fields_are_wildcards():
    """Fields not set on the rule do not constrain matching."""
    # Rule only constrains app_name; other incident fields can be anything
    rule = IgnoreRule(app_name="my-app")
    assert rule.matches(_incident(app_name="my-app", alarm_name="anything", environment="dev")) is True
    assert rule.matches(_incident(app_name="other-app")) is False


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


def test_first_match_returns_first_matching_rule():
    cfg = IgnoreConfig(
        enabled=True,
        rules=[
            IgnoreRule(name="rule-a", alarm_name="alarm-a"),
            IgnoreRule(name="rule-b", alarm_name="alarm-b"),
        ],
    )
    result = cfg.first_match(_incident(alarm_name="alarm-b"))
    assert result is not None
    assert result.name == "rule-b"


def test_first_match_ordering_first_wins():
    """When two rules both match, the first one in list order wins."""
    cfg = IgnoreConfig(
        enabled=True,
        rules=[
            IgnoreRule(name="first", app_name="my-app"),
            IgnoreRule(name="second", app_name="my-app"),
        ],
    )
    result = cfg.first_match(_incident(app_name="my-app"))
    assert result is not None
    assert result.name == "first"


def test_first_match_no_match_returns_none():
    cfg = IgnoreConfig(
        enabled=True,
        rules=[IgnoreRule(alarm_name="specific-alarm")],
    )
    result = cfg.first_match(_incident(alarm_name="different-alarm"))
    assert result is None


def test_first_match_disabled_config_returns_none():
    """When IgnoreConfig.enabled is False, first_match always returns None."""
    cfg = IgnoreConfig(
        enabled=False,
        rules=[IgnoreRule()],  # catch-all rule — would match if config were enabled
    )
    result = cfg.first_match(_incident())
    assert result is None


def test_first_match_skips_disabled_rules():
    """Rules with enabled=False are skipped even if they would otherwise match."""
    cfg = IgnoreConfig(
        enabled=True,
        rules=[
            IgnoreRule(name="off", alarm_name="my-app-5xx", enabled=False),
            IgnoreRule(name="on", alarm_name="my-app-5xx", enabled=True),
        ],
    )
    result = cfg.first_match(_incident(alarm_name="my-app-5xx"))
    assert result is not None
    assert result.name == "on"


def test_first_match_all_disabled_returns_none():
    cfg = IgnoreConfig(
        enabled=True,
        rules=[
            IgnoreRule(enabled=False),
            IgnoreRule(enabled=False),
        ],
    )
    assert cfg.first_match(_incident()) is None


def test_ignore_config_empty_rules_returns_none():
    cfg = IgnoreConfig(enabled=True, rules=[])
    assert cfg.first_match(_incident()) is None


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

    def test_matching_rule_returns_ignored_true(self, monkeypatch):
        """An alarm matching an enabled ignore rule must return ignored=True."""
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

    def test_matching_rule_does_not_persist(self, monkeypatch):
        """An ignored alarm must NOT be saved to the incident store."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        store = FakeIgnoreRuleStore(rules=[("rule-002", rule)])
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        h._handle_alarm({})

        incident_store.put_incident.assert_not_called()

    def test_matching_rule_does_not_page(self, monkeypatch):
        """An ignored alarm must NOT dispatch a page."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        store = FakeIgnoreRuleStore(rules=[("rule-003", rule)])
        inc = self._incident()

        h, incident_store, dispatch_spy = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        h._handle_alarm({})

        assert not dispatch_spy.called

    def test_record_trigger_called_with_rule_id(self, monkeypatch):
        """record_trigger must be called with the matched rule_id."""
        rule = IgnoreRule(alarm_name="test-alarm-high-error")
        store = FakeIgnoreRuleStore(rules=[("rule-004", rule)])
        inc = self._incident()

        h, _, _ = _build_ignore_handler(
            monkeypatch, ignore_rule_store=store, incident=inc
        )
        h._handle_alarm({})

        assert store.triggered == ["rule-004"]

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


# ---------------------------------------------------------------------------
# 6. DynamoRoutingRuleStore (moto)
# ---------------------------------------------------------------------------

ROUTING_TABLE_NAME = "relay-routing-test"


@pytest.fixture
def routing_dynamo_session():
    from moto import mock_aws

    with mock_aws():
        session = boto3.session.Session(region_name="us-east-1")
        ddb = session.resource("dynamodb")
        table = ddb.create_table(
            TableName=ROUTING_TABLE_NAME,
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


def _routing_store(session, clock=None):
    from relay.adapters.aws.dynamo_stores import DynamoRoutingRuleStore

    return DynamoRoutingRuleStore(
        table_name=ROUTING_TABLE_NAME, boto3_session=session, clock=clock
    )


def _routing_rule(
    rule_id: str = "rule-001",
    priority: int = 10,
    escalation_policy_id: str = "pol-default",
    **kwargs,
):
    from relay.core.model import RoutingRule

    return RoutingRule(
        rule_id=rule_id,
        priority=priority,
        escalation_policy_id=escalation_policy_id,
        **kwargs,
    )


class TestDynamoRoutingRuleStore:
    """Round-trip, priority-sort, counter, and toggle tests for DynamoRoutingRuleStore."""

    def test_put_rule_returns_id(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        rule = _routing_rule(rule_id="my-rule", priority=5)
        rule_id = store.put_rule(rule)
        assert rule_id == "my-rule"

    def test_put_rule_explicit_id_overrides_rule_id(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        rule = _routing_rule(rule_id="embedded-id", priority=5)
        stored_id = store.put_rule(rule, rule_id="explicit-id")
        assert stored_id == "explicit-id"
        # retrievable under the explicit id
        assert store.get_rule("explicit-id") is not None

    def test_put_and_get_round_trip_all_fields(self, routing_dynamo_session):
        """All RoutingRule fields survive JSON round-trip."""
        from relay.core.model import RoutingRule, Severity, Stream

        store = _routing_store(routing_dynamo_session)
        rule = RoutingRule(
            rule_id="full-rule",
            priority=20,
            alarm_name_prefix="api-",
            alarm_name_regex=r"^api-5\d\d$",
            tag_filters={"team": "platform", "tier": "1"},
            namespace_prefix="AWS/Lambda",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-critical",
            streams=[Stream.TEAM, Stream.CENTRAL],
        )
        rule_id = store.put_rule(rule)
        loaded = store.get_rule(rule_id)

        assert loaded is not None
        assert loaded.rule_id == "full-rule"
        assert loaded.priority == 20
        assert loaded.alarm_name_prefix == "api-"
        assert loaded.alarm_name_regex == r"^api-5\d\d$"
        assert loaded.tag_filters == {"team": "platform", "tier": "1"}
        assert loaded.namespace_prefix == "AWS/Lambda"
        assert loaded.severity_override == Severity.SEV1
        assert loaded.escalation_policy_id == "pol-critical"
        assert Stream.TEAM in loaded.streams
        assert Stream.CENTRAL in loaded.streams

    def test_get_rule_missing_returns_none(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        assert store.get_rule("nonexistent-routing-rule") is None

    def test_put_rule_overwrites_existing(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        rule = _routing_rule(rule_id="overwrite-me", priority=5, alarm_name_prefix="old-")
        store.put_rule(rule)

        updated = _routing_rule(rule_id="overwrite-me", priority=15, alarm_name_prefix="new-")
        store.put_rule(updated, rule_id="overwrite-me")

        loaded = store.get_rule("overwrite-me")
        assert loaded is not None
        assert loaded.alarm_name_prefix == "new-"
        assert loaded.priority == 15

    def test_list_rules_sorted_by_priority_ascending(self, routing_dynamo_session):
        """list_rules must return rules ordered by priority (ascending)."""
        store = _routing_store(routing_dynamo_session)
        # Add out of priority order intentionally
        store.put_rule(_routing_rule(rule_id="low-pri",  priority=100))
        store.put_rule(_routing_rule(rule_id="high-pri", priority=1))
        store.put_rule(_routing_rule(rule_id="mid-pri",  priority=50))

        results = store.list_rules()
        priorities = [r[1].priority for r in results
                      if r[0] in ("low-pri", "high-pri", "mid-pri")]
        assert priorities == sorted(priorities), (
            f"Expected ascending priority order but got {priorities}"
        )

    def test_list_rules_priority_tiebreak_by_rule_id(self, routing_dynamo_session):
        """When two rules share a priority they are sub-sorted by rule_id."""
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="zzz-rule", priority=10))
        store.put_rule(_routing_rule(rule_id="aaa-rule", priority=10))

        results = store.list_rules()
        ids = [r[0] for r in results if r[0] in ("zzz-rule", "aaa-rule")]
        assert ids == ["aaa-rule", "zzz-rule"]

    def test_list_rules_match_count_zero_when_no_matches(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="no-match-rule", priority=5))

        results = store.list_rules()
        counts = {r[0]: r[2] for r in results}
        assert counts.get("no-match-rule", 0) == 0

    def test_list_rules_joins_match_counts(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="counted-routing", priority=5))
        store.record_match("counted-routing")
        store.record_match("counted-routing")

        results = store.list_rules()
        counts = {r[0]: r[2] for r in results}
        assert counts["counted-routing"] == 2

    def test_list_rules_includes_enabled_flag(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="enabled-rule",  priority=1), enabled=True)
        store.put_rule(_routing_rule(rule_id="disabled-rule", priority=2), enabled=False)

        result_map = {r[0]: r[3] for r in store.list_rules()
                      if r[0] in ("enabled-rule", "disabled-rule")}
        assert result_map["enabled-rule"] is True
        assert result_map["disabled-rule"] is False

    def test_list_rules_enabled_defaults_true(self, routing_dynamo_session):
        """put_rule with no explicit enabled= should default to True in list_rules."""
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="default-enabled", priority=1))

        result_map = {r[0]: r[3] for r in store.list_rules()}
        assert result_map.get("default-enabled") is True

    def test_record_match_increments_atomically(self, routing_dynamo_session):
        fixed_time = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        store = _routing_store(routing_dynamo_session, clock=lambda: fixed_time)
        store.put_rule(_routing_rule(rule_id="counter-routing", priority=5))

        assert store.record_match("counter-routing") == 1
        assert store.record_match("counter-routing") == 2
        assert store.record_match("counter-routing") == 3

    def test_record_match_sets_last_matched_at(self, routing_dynamo_session):
        fixed_time = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        store = _routing_store(routing_dynamo_session, clock=lambda: fixed_time)
        store.put_rule(_routing_rule(rule_id="ts-routing", priority=5))
        store.record_match("ts-routing")

        table = routing_dynamo_session.resource("dynamodb").Table(ROUTING_TABLE_NAME)
        item = table.get_item(
            Key={"pk": "ROUTING#ts-routing", "sk": "COUNTER"}
        ).get("Item")
        assert item is not None
        assert item["last_matched_at"] == fixed_time.isoformat()
        assert int(item["match_count"]) == 1

    def test_delete_rule_removes_meta_item(self, routing_dynamo_session):
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="delete-routing", priority=5))
        assert store.get_rule("delete-routing") is not None

        store.delete_rule("delete-routing")
        assert store.get_rule("delete-routing") is None

    def test_delete_rule_cascades_counter(self, routing_dynamo_session):
        """delete_rule removes both META and COUNTER items."""
        store = _routing_store(routing_dynamo_session)
        store.put_rule(_routing_rule(rule_id="cascade-routing", priority=5))
        store.record_match("cascade-routing")

        table = routing_dynamo_session.resource("dynamodb").Table(ROUTING_TABLE_NAME)
        counter_before = table.get_item(
            Key={"pk": "ROUTING#cascade-routing", "sk": "COUNTER"}
        ).get("Item")
        assert counter_before is not None

        store.delete_rule("cascade-routing")

        assert store.get_rule("cascade-routing") is None
        counter_after = table.get_item(
            Key={"pk": "ROUTING#cascade-routing", "sk": "COUNTER"}
        ).get("Item")
        assert counter_after is None

    def test_delete_rule_idempotent(self, routing_dynamo_session):
        """Deleting a non-existent routing rule is a no-op."""
        store = _routing_store(routing_dynamo_session)
        store.delete_rule("nonexistent-routing-rule")  # should not raise

    def test_set_enabled_flips_flag(self, routing_dynamo_session):
        """set_enabled toggles the enabled flag without losing rule fields."""

        store = _routing_store(routing_dynamo_session)
        rule = _routing_rule(rule_id="toggle-rule", priority=5, alarm_name_prefix="api-")
        store.put_rule(rule, enabled=True)

        # Disable it
        store.set_enabled("toggle-rule", False)
        result_map = {r[0]: r[3] for r in store.list_rules()}
        assert result_map["toggle-rule"] is False

        # Re-enable it
        store.set_enabled("toggle-rule", True)
        result_map = {r[0]: r[3] for r in store.list_rules()}
        assert result_map["toggle-rule"] is True

    def test_set_enabled_preserves_rule_fields(self, routing_dynamo_session):
        """set_enabled must not overwrite rule_json or other META attributes."""
        store = _routing_store(routing_dynamo_session)
        rule = _routing_rule(rule_id="preserve-rule", priority=7, alarm_name_prefix="keep-me-")
        store.put_rule(rule, enabled=True)

        store.set_enabled("preserve-rule", False)

        loaded = store.get_rule("preserve-rule")
        assert loaded is not None
        assert loaded.alarm_name_prefix == "keep-me-"
        assert loaded.priority == 7


# ---------------------------------------------------------------------------
# 7. NodeHandler routing-rule gate (DB-backed routing via _effective_routing_config)
# ---------------------------------------------------------------------------


class FakeRoutingRuleStore:
    """Minimal fake DynamoRoutingRuleStore for NodeHandler injection.

    list_rules() returns (rule_id, RoutingRule, match_count, enabled) tuples.
    record_match() records calls for assertion.
    """

    def __init__(
        self,
        rules: list[tuple[str, Any, int, bool]] | None = None,
    ) -> None:
        self._rules: list[tuple[str, Any, int, bool]] = rules or []
        self.matched: list[str] = []
        self._list_raises: Exception | None = None
        self._record_raises: Exception | None = None

    def list_rules(self) -> list[tuple[str, Any, int, bool]]:
        if self._list_raises is not None:
            raise self._list_raises
        return list(self._rules)

    def record_match(self, rule_id: str) -> int:
        if self._record_raises is not None:
            raise self._record_raises
        self.matched.append(rule_id)
        return len(self.matched)


def _build_routing_handler(
    monkeypatch,
    *,
    routing_rule_store: FakeRoutingRuleStore,
    incident: Incident,
    config_rules: list[Any] | None = None,
    suppression_store: Any = None,
) -> tuple[Any, Any, Any]:
    """Build a NodeHandler with injected routing_rule_store + all AWS fakes.

    Returns (handler, incident_store_mock, dispatch_spy).
    config_rules: list of RoutingRule objects for self.config.routing.rules
                  (empty list by default).
    """
    import yaml as _yaml


    for k, v in _HANDLER_ENV.items():
        monkeypatch.setenv(k, v)

    escalation = EscalationConfig.model_validate(_yaml.safe_load(ESCALATION_YAML))
    routing = RoutingConfig(
        rules=sorted(config_rules or [], key=lambda r: r.priority),
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
        _ignore_rule_store=FakeIgnoreRuleStore(rules=[]),
        _routing_rule_store=routing_rule_store,
        _escalation_engine=FakeEscalationEngine(),
        _clock=lambda: 0.0,
    )
    return h, incident_store, dispatch_spy


def _make_incident(alarm_name: str = "test-alarm-sev2") -> Incident:
    return Incident(
        account_id="123456789012",
        region="us-east-1",
        app_name="test-app",
        severity=Severity.SEV3,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name=alarm_name,
        environment="prod",
    )


class TestNodeHandlerRoutingRuleGate:
    """Tests for the DB-backed routing-rule path in NodeHandler._handle_alarm."""

    def test_db_rule_overrides_config_severity(self, monkeypatch):
        """A DB rule with severity_override=SEV1 should classify the incident as SEV1."""
        from relay.core.model import RoutingRule, Stream

        db_rule = RoutingRule(
            rule_id="db-sev1-rule",
            priority=10,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[("db-sev1-rule", db_rule, 0, True)])
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, incident_store, dispatch_spy = _build_routing_handler(
            monkeypatch, routing_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        assert result["statusCode"] == 200
        # Incident should have been classified as SEV1 by the DB rule
        assert result["severity"] == Severity.SEV1

    def test_empty_db_falls_back_to_config(self, monkeypatch):
        """With no DB routing rules, classification uses self.config.routing (config rules)."""
        from relay.core.model import RoutingRule, Stream

        # A config rule (priority=5) sets SEV2 for alarms prefixed "test-alarm-"
        config_rule = RoutingRule(
            rule_id="config-sev2-rule",
            priority=5,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV2,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[])  # empty DB
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, incident_store, dispatch_spy = _build_routing_handler(
            monkeypatch,
            routing_rule_store=store,
            incident=inc,
            config_rules=[config_rule],
        )
        result = h._handle_alarm({})

        # Config rule should fire: SEV2
        assert result["severity"] == Severity.SEV2

    def test_fail_open_on_list_rules_error(self, monkeypatch):
        """If list_rules raises, classification still succeeds using config — no exception propagates."""
        from relay.core.model import RoutingRule, Stream

        # Config rule sets SEV2
        config_rule = RoutingRule(
            rule_id="config-fallback-rule",
            priority=5,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV2,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[])
        store._list_raises = RuntimeError("DynamoDB unavailable")
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, incident_store, dispatch_spy = _build_routing_handler(
            monkeypatch,
            routing_rule_store=store,
            incident=inc,
            config_rules=[config_rule],
        )
        # Must not raise
        result = h._handle_alarm({})

        assert result["statusCode"] == 200
        # Config fallback: SEV2
        assert result["severity"] == Severity.SEV2

    def test_record_match_called_with_db_rule_id(self, monkeypatch):
        """record_match must be called with the matched rule_id when a DB rule matches."""
        from relay.core.model import RoutingRule, Stream

        db_rule = RoutingRule(
            rule_id="track-me-rule",
            priority=10,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[("track-me-rule", db_rule, 0, True)])
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, _, _ = _build_routing_handler(
            monkeypatch, routing_rule_store=store, incident=inc
        )
        h._handle_alarm({})

        assert "track-me-rule" in store.matched

    def test_record_match_not_called_when_db_empty(self, monkeypatch):
        """record_match must NOT be called when DB is empty (config rules used)."""
        from relay.core.model import RoutingRule, Stream

        config_rule = RoutingRule(
            rule_id="config-rule",
            priority=5,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV2,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[])
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, _, _ = _build_routing_handler(
            monkeypatch,
            routing_rule_store=store,
            incident=inc,
            config_rules=[config_rule],
        )
        h._handle_alarm({})

        assert store.matched == [], "record_match must not be called when using config rules"

    def test_record_match_failure_does_not_abort(self, monkeypatch):
        """A record_match failure must be swallowed; the incident still proceeds."""
        from relay.core.model import RoutingRule, Stream

        db_rule = RoutingRule(
            rule_id="count-fail-rule",
            priority=10,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[("count-fail-rule", db_rule, 0, True)])
        store._record_raises = RuntimeError("counter table throttled")
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, incident_store, dispatch_spy = _build_routing_handler(
            monkeypatch, routing_rule_store=store, incident=inc
        )
        # Must not raise
        result = h._handle_alarm({})

        assert result["statusCode"] == 200
        assert result["severity"] == Severity.SEV1  # classification still succeeded

    def test_priority_ordering_lower_number_wins(self, monkeypatch):
        """The DB rule with the lower priority number should win over a higher one."""
        from relay.core.model import RoutingRule, Stream

        # priority=5 (wins) sets SEV1
        high_pri_rule = RoutingRule(
            rule_id="rule-priority-5",
            priority=5,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        # priority=20 (loses) would set SEV4
        low_pri_rule = RoutingRule(
            rule_id="rule-priority-20",
            priority=20,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV4,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        # Return them in the "wrong" order so we verify sorting
        store = FakeRoutingRuleStore(rules=[
            ("rule-priority-20", low_pri_rule, 0, True),
            ("rule-priority-5", high_pri_rule, 0, True),
        ])
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, _, _ = _build_routing_handler(
            monkeypatch, routing_rule_store=store, incident=inc
        )
        result = h._handle_alarm({})

        # Lower priority number (5) wins → SEV1
        assert result["severity"] == Severity.SEV1

    def test_disabled_db_rule_is_skipped(self, monkeypatch):
        """A DB rule with enabled=False must be skipped; config rule fires instead."""
        from relay.core.model import RoutingRule, Stream

        disabled_db_rule = RoutingRule(
            rule_id="disabled-db-rule",
            priority=5,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,  # would win if enabled
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        config_rule = RoutingRule(
            rule_id="config-sev2-rule",
            priority=10,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV2,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        store = FakeRoutingRuleStore(rules=[("disabled-db-rule", disabled_db_rule, 0, False)])
        inc = _make_incident(alarm_name="test-alarm-sev2")

        h, _, _ = _build_routing_handler(
            monkeypatch,
            routing_rule_store=store,
            incident=inc,
            config_rules=[config_rule],
        )
        result = h._handle_alarm({})

        # Disabled DB rule skipped → config rule fires → SEV2
        assert result["severity"] == Severity.SEV2

    def test_ttl_cache_avoids_repeated_db_calls(self, monkeypatch):
        """Within the TTL window, list_rules must only be called once."""
        from relay.core.model import RoutingRule, Stream

        for k, v in _HANDLER_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("RELAY_ROUTING_RULES_TTL_SECONDS", "30")

        db_rule = RoutingRule(
            rule_id="cache-rule",
            priority=10,
            alarm_name_prefix="test-alarm-",
            severity_override=Severity.SEV1,
            escalation_policy_id="pol-default",
            streams=[Stream.TEAM],
        )
        mock_store = MagicMock()
        mock_store.list_rules.return_value = [("cache-rule", db_rule, 0, True)]
        mock_store.record_match.return_value = 1

        inc = _make_incident(alarm_name="test-alarm-sev2")

        import yaml as _yaml

        escalation = EscalationConfig.model_validate(_yaml.safe_load(ESCALATION_YAML))
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

        h = handler_mod.NodeHandler(
            _config_loader=_Loader(),
            _alarm_source=FakeAlarmSource(inc),
            _notifier=MagicMock(),
            _transport=MagicMock(),
            _contact_store=MagicMock(),
            _incident_store=MagicMock(),
            _escalation_state_store=FakeEscalationStateStore(),
            _suppression_store=MagicMock(),
            _ignore_rule_store=FakeIgnoreRuleStore(rules=[]),
            _routing_rule_store=mock_store,
            _escalation_engine=FakeEscalationEngine(),
            _clock=lambda: 0.0,  # clock frozen: TTL never expires
        )

        # Call _handle_alarm twice — list_rules should only be called once
        h._handle_alarm({})
        h._handle_alarm({})

        assert mock_store.list_rules.call_count == 1, (
            "list_rules must only be called once within the TTL window"
        )
