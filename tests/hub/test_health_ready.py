"""Tests for GET /health/ready — deep readiness probe.

Covers:
  1. All checks pass → status "ok"
  2. DynamoDB describe_table fails → dynamodb check fails, status "degraded"
  3. SQS not configured → sqs_ingest check ok with a note
  4. SQS configured but GetQueueAttributes fails → sqs_ingest check fails
  5. SNS paging topic not configured → sns_paging_topic check ok with note
  6. SNS paging topic GetTopicAttributes fails → sns_paging_topic check fails
  7. SNS direct-SMS probe fails (AuthorizationError) → sns_direct_sms fails
  8. config_loaded reflects loaded/not-loaded state
  9. ignore_rules_seeded reflects rule count
  10. routing_rules_seeded reflects rule count
  11. ignore/routing rule store unavailable → respective checks fail
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402

# ---------------------------------------------------------------------------
# Fake stores
# ---------------------------------------------------------------------------


class _FakeIgnoreRuleStore:
    def __init__(self, rules: list[Any] | None = None) -> None:
        self._rules = rules or []

    def list_rules(self) -> list[Any]:
        return list(self._rules)

    def put_rule(self, rule: Any) -> None:
        self._rules.append(rule)


class _FakeRoutingRuleStore:
    def __init__(self, rules: list[Any] | None = None) -> None:
        self._rules = rules or []

    def list_rules(self) -> list[tuple[str, Any, int, bool]]:
        return [(f"id-{i}", r, 0, True) for i, r in enumerate(self._rules)]

    def put_rule(self, rule: Any, rule_id: str | None = None, *, enabled: bool = True) -> str:
        self._rules.append(rule)
        return rule_id or "generated-id"


class _FakeConfig:
    """Minimal hub config object (stands in for RelayConfig)."""
    pass


# ---------------------------------------------------------------------------
# Test client builder
# ---------------------------------------------------------------------------


def _client(
    *,
    ignore_rule_store: Any = None,
    routing_rule_store: Any = None,
    hub_config: Any | None = None,
    ignore_rule_store_none: bool = False,
    routing_rule_store_none: bool = False,
) -> TestClient:
    """Build a minimal HubApp TestClient with injected fakes."""
    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = None
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._settings_store = None
    app_obj._schedule_store = None
    app_obj._ignore_rule_store = (
        None
        if ignore_rule_store_none
        else (ignore_rule_store if ignore_rule_store is not None else _FakeIgnoreRuleStore())
    )
    app_obj._ignore_baseline = []
    app_obj._routing_rule_store = (
        None
        if routing_rule_store_none
        else (routing_rule_store if routing_rule_store is not None else _FakeRoutingRuleStore())
    )
    app_obj._routing_baseline = []
    app_obj._config = hub_config
    app_obj._pipeline = None
    app_obj._runtime = "local-mock"

    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    hs._org_paths = {}
    hs._org_tree = None
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    return TestClient(app_obj.build_fastapi_app())


# ---------------------------------------------------------------------------
# AWS client mock helpers
# ---------------------------------------------------------------------------


def _good_boto3() -> MagicMock:
    """A boto3.client mock whose describe/get calls succeed."""
    mock = MagicMock()
    mock.describe_table.return_value = {"Table": {"TableName": "relay-hub-fleet"}}
    mock.get_queue_attributes.return_value = {"Attributes": {"ApproximateNumberOfMessages": "0"}}
    mock.get_topic_attributes.return_value = {"Attributes": {"TopicArn": "arn:aws:sns:us-east-1:123:test"}}
    mock.list_phone_numbers_opted_out.return_value = {"phoneNumbers": []}
    return mock


def _boto3_factory(success: dict[str, Any], fail_on: set[str] | None = None) -> Any:
    """Return a context-manager-friendly patch for boto3.client.

    ``success`` maps service -> mock client that succeeds.
    ``fail_on`` is a set of service names whose clients raise ClientError.
    """
    from botocore.exceptions import ClientError

    fail_on = fail_on or set()

    def _side_effect(service: str, **kwargs: Any) -> Any:
        if service in fail_on:
            m = MagicMock()
            def _raise(*a: Any, **k: Any) -> None:
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                    "TestOperation",
                )
            m.describe_table.side_effect = _raise
            m.get_queue_attributes.side_effect = _raise
            m.get_topic_attributes.side_effect = _raise
            m.list_phone_numbers_opted_out.side_effect = _raise
            return m
        return success.get(service, _good_boto3())

    return _side_effect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear Hub-relevant env vars so tests are hermetic."""
    for var in [
        "RELAY_FLEET_TABLE_NAME",
        "RELAY_DYNAMO_INCIDENTS_TABLE",
        "RELAY_SQS_QUEUE_URL",
        "RELAY_SNS_TOPIC_ARN",
        "RELAY_PAGING_TOPIC_ARN",
        "RELAY_CENTRAL_PAGING_TOPIC_ARN",
        "RELAY_CONFIG_SOURCE",
        "RELAY_CONFIG_DIR",
        "RELAY_AUTH_MODE",
        "RELAY_ENABLE_DIRECT_SMS",
        "RELAY_SKIP_SMS_OPTOUT_PROBE",
    ]:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthReady:
    def test_all_pass_returns_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When every probe succeeds, status is 'ok'."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)
        monkeypatch.setenv("RELAY_ENABLE_DIRECT_SMS", "true")

        client = _client(
            ignore_rule_store=_FakeIgnoreRuleStore(["rule1", "rule2"]),
            routing_rule_store=_FakeRoutingRuleStore(["rt1"]),
            hub_config=_FakeConfig(),
        )
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        checks = body["checks"]
        assert checks["dynamodb"]["ok"] is True
        assert checks["sqs_ingest"]["ok"] is True
        assert checks["sns_paging_topic"]["ok"] is True
        assert checks["sns_direct_sms"]["ok"] is True
        assert checks["config_loaded"]["ok"] is True
        assert checks["ignore_rules_seeded"]["ok"] is True
        assert checks["ignore_rules_seeded"]["count"] == 2
        assert checks["routing_rules_seeded"]["ok"] is True
        assert checks["routing_rules_seeded"]["count"] == 1

    def test_dynamo_fail_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DynamoDB describe_table failure → dynamodb ok=false, status degraded."""
        from botocore.exceptions import ClientError

        def _raise(*a: Any, **k: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
                "DescribeTable",
            )

        mock = _good_boto3()
        mock.describe_table.side_effect = _raise
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["dynamodb"]["ok"] is False
        assert "ResourceNotFoundException" in body["checks"]["dynamodb"]["error"]

    def test_sqs_not_configured_is_ok_with_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No RELAY_SQS_QUEUE_URL → sqs_ingest ok=true with a note."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        sqs = resp.json()["checks"]["sqs_ingest"]
        assert sqs["ok"] is True
        assert "not configured" in sqs.get("note", "")

    def test_sqs_configured_and_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured SQS URL + get_queue_attributes failure → ok=false."""
        from botocore.exceptions import ClientError

        monkeypatch.setenv("RELAY_SQS_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test")

        def _raise(*a: Any, **k: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetQueueAttributes",
            )

        mock = _good_boto3()
        mock.get_queue_attributes.side_effect = _raise
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["sqs_ingest"]["ok"] is False

    def test_sns_topic_not_configured_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No SNS topic ARN → sns_paging_topic ok=true with a note."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        sns = resp.json()["checks"]["sns_paging_topic"]
        assert sns["ok"] is True
        assert "not configured" in sns.get("note", "")

    def test_sns_topic_configured_and_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured SNS topic ARN + get_topic_attributes failure → ok=false."""
        from botocore.exceptions import ClientError

        monkeypatch.setenv("RELAY_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:test-topic")

        def _raise(*a: Any, **k: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "AuthorizationError", "Message": "Access Denied"}},
                "GetTopicAttributes",
            )

        mock = _good_boto3()
        mock.get_topic_attributes.side_effect = _raise
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["sns_paging_topic"]["ok"] is False

    def test_sns_direct_sms_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SNS direct-SMS probe raises AuthorizationError → sns_direct_sms fails.

        Only runs when direct SMS is enabled (RELAY_ENABLE_DIRECT_SMS=true).
        """
        from botocore.exceptions import ClientError

        monkeypatch.setenv("RELAY_ENABLE_DIRECT_SMS", "true")

        def _raise(*a: Any, **k: Any) -> None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AuthorizationError",
                        "Message": "SNS:Publish denied on phone resources",
                    }
                },
                "ListPhoneNumbersOptedOut",
            )

        mock = _good_boto3()
        mock.list_phone_numbers_opted_out.side_effect = _raise
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        sms = body["checks"]["sns_direct_sms"]
        assert sms["ok"] is False
        assert "AuthorizationError" in sms["error"]
        # Because only the direct-SMS check fails and other checks pass,
        # this deployment is degraded.
        assert body["status"] == "degraded"

    def test_sns_direct_sms_pinpoint_scp_deny_is_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ISSUE-5: a Pinpoint SMS Voice v2 SCP denial is a warn, not a failure.

        The opt-out probe routes internally to sms-voice:DescribeOptedOutNumbers.
        An SCP that denies that service blocks the probe even though the runtime
        sns:Publish SMS path is unaffected, so the check stays ok=true (with a
        warn) and the deployment is not degraded.
        """
        from botocore.exceptions import ClientError

        monkeypatch.setenv("RELAY_ENABLE_DIRECT_SMS", "true")

        def _raise(*a: Any, **k: Any) -> None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "AuthorizationError",
                        "Message": (
                            "User is not authorized to perform: "
                            "sms-voice:DescribeOptedOutNumbers on resource: "
                            "arn:aws:sms-voice:us-east-1:123:opt-out-list/Default "
                            "with an explicit deny in an identity-based policy "
                            "(Service: PinpointSmsVoiceV2, Status Code: 400)"
                        ),
                    }
                },
                "ListPhoneNumbersOptedOut",
            )

        mock = _good_boto3()
        mock.list_phone_numbers_opted_out.side_effect = _raise
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client(
            ignore_rule_store=_FakeIgnoreRuleStore(["r1"]),
            routing_rule_store=_FakeRoutingRuleStore(["rt1"]),
            hub_config=_FakeConfig(),
        )
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        sms = body["checks"]["sns_direct_sms"]
        assert sms["ok"] is True
        assert "SCP" in sms.get("warn", "")
        assert "error" not in sms
        assert body["status"] == "ok"

    def test_sns_direct_sms_skipped_by_escape_hatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ISSUE-5: RELAY_SKIP_SMS_OPTOUT_PROBE=true skips the probe entirely.

        Operators who know their account denies Pinpoint SMS Voice v2 can opt out
        of the probe for a clean health check; it must not run boto3 at all.
        """
        monkeypatch.setenv("RELAY_ENABLE_DIRECT_SMS", "true")
        monkeypatch.setenv("RELAY_SKIP_SMS_OPTOUT_PROBE", "true")

        mock = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client(
            ignore_rule_store=_FakeIgnoreRuleStore(["r1"]),
            routing_rule_store=_FakeRoutingRuleStore(["rt1"]),
            hub_config=_FakeConfig(),
        )
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        sms = body["checks"]["sns_direct_sms"]
        assert sms["ok"] is True
        assert "skipped" in sms.get("note", "")
        assert body["status"] == "ok"
        mock.list_phone_numbers_opted_out.assert_not_called()

    def test_sns_direct_sms_skipped_when_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without RELAY_ENABLE_DIRECT_SMS the probe is skipped (ok=true, note).

        A deployment that never opted into direct SMS must not run the probe at
        all — the RelayHubDirectSms grant is absent by design, so probing would
        surface a spurious denial.
        """
        mock = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)

        client = _client(
            ignore_rule_store=_FakeIgnoreRuleStore(["r1"]),
            routing_rule_store=_FakeRoutingRuleStore(["rt1"]),
            hub_config=_FakeConfig(),
        )
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        sms = body["checks"]["sns_direct_sms"]
        assert sms["ok"] is True
        assert "skipped" in sms.get("note", "")
        assert body["status"] == "ok"
        mock.list_phone_numbers_opted_out.assert_not_called()

    def test_sns_direct_sms_never_routes_to_pinpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (ISSUE-5): the direct-SMS probe must never call an SNS API
        that AWS routes internally to Pinpoint SMS Voice.

        CheckIfPhoneNumberIsOptedOut routes to sms-voice:DescribeOptedOutNumbers,
        which strict-SCP accounts deny outright even when direct SMS works at
        runtime — a false degraded. This test fails closed if the probe is ever
        switched back to a Pinpoint-routing call, a class of bug that a mocked
        boto3 client cannot otherwise surface (the mock has no real routing).
        """
        mock = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: mock)
        monkeypatch.setenv("RELAY_ENABLE_DIRECT_SMS", "true")

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        assert resp.json()["checks"]["sns_direct_sms"]["ok"] is True
        # The probe must use the SNS-local opt-out list call...
        mock.list_phone_numbers_opted_out.assert_called_once()
        # ...and never the phone-number check that AWS routes to Pinpoint.
        mock.check_if_phone_number_is_opted_out.assert_not_called()

    def test_config_loaded_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When hub_config is not None, config_loaded ok=true with source."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)
        monkeypatch.setenv("RELAY_CONFIG_SOURCE", "local")
        monkeypatch.setenv("RELAY_CONFIG_DIR", "/app/config")

        client = _client(hub_config=_FakeConfig())
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        cfg = resp.json()["checks"]["config_loaded"]
        assert cfg["ok"] is True
        assert cfg["source"] == "local"
        assert cfg["path"] == "/app/config"

    def test_config_not_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When hub_config is None, config_loaded ok=true but loaded=false."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client(hub_config=None)
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        cfg = resp.json()["checks"]["config_loaded"]
        assert cfg["ok"] is True
        assert cfg.get("loaded") is False

    def test_ignore_rule_store_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ignore_rule_store=None → ignore_rules_seeded fails."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client(ignore_rule_store_none=True)
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["checks"]["ignore_rules_seeded"]["ok"] is False
        assert body["status"] == "degraded"

    def test_routing_rule_store_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """routing_rule_store=None → routing_rules_seeded fails."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client(routing_rule_store_none=True)
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["checks"]["routing_rules_seeded"]["ok"] is False
        assert body["status"] == "degraded"

    def test_rule_counts_in_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Seeded rule counts appear in the response."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        ignore_store = _FakeIgnoreRuleStore(["r1", "r2", "r3"])
        routing_store = _FakeRoutingRuleStore(["rt1", "rt2"])
        client = _client(ignore_rule_store=ignore_store, routing_rule_store=routing_store)
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        checks = resp.json()["checks"]
        assert checks["ignore_rules_seeded"]["count"] == 3
        assert checks["routing_rules_seeded"]["count"] == 2

    def test_response_shape_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Response always has 'status' and all 7 expected check keys."""
        good = _good_boto3()
        monkeypatch.setattr("relay.hub.app.boto3.client", lambda svc, **kw: good)

        client = _client()
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "checks" in body
        expected_keys = {
            "dynamodb",
            "sqs_ingest",
            "sns_paging_topic",
            "sns_direct_sms",
            "config_loaded",
            "ignore_rules_seeded",
            "routing_rules_seeded",
        }
        assert set(body["checks"].keys()) == expected_keys
