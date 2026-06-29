"""Tests for the Hub routing-rules API endpoints (Phase R3).

Covers:
- POST /routing-rules requires escalation_policy_id (422 without)
- POST /routing-rules creates rule; appears in GET /routing-rules with match_count + enabled
- Bad regex → 422
- PUT updates (e.g. change severity_override); DELETE removes; 404s
- POST /incidents/{id}/route creates rule prefilled from incident, requires escalation_policy_id,
  does NOT resolve the incident
- GET /escalation-policies returns configured policies
- GET /routing-rules/deviation reports deviates=True after a UI-created rule absent from baseline
- GET /routing-rules/download returns yaml with a `rules:` block + attachment header
- Seeding: empty store seeds from config routing rules; non-empty store does NOT overwrite
- Writer gating: POST/PUT/DELETE return 403 without auth
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from relay.core.model import (
    EscalationPolicy,
    EscalationStep,
    Incident,
    IncidentState,
    RoutingRule,
    Severity,
    SignalSource,
)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.hub.app import (  # noqa: E402
    HubApp,
    HubState,
    SSEPublisher,
    _seed_routing_rules,
)

# ---------------------------------------------------------------------------
# Fake stores
# ---------------------------------------------------------------------------


class _FakeRoutingRuleStore:
    """In-memory implementation of DynamoRoutingRuleStore's public interface."""

    def __init__(self) -> None:
        self._rules: dict[str, tuple[Any, ...]] = {}  # rule_id -> (RoutingRule, match_count, enabled)

    def put_rule(self, rule, rule_id=None, *, enabled=True) -> str:
        import uuid
        if rule_id is None:
            rule_id = rule.rule_id or uuid.uuid4().hex
        existing_count = self._rules.get(rule_id, (None, 0, True))[1]
        self._rules[rule_id] = (rule, existing_count, enabled)
        return cast(str, rule_id)

    def get_rule(self, rule_id: str):
        entry = self._rules.get(rule_id)
        return entry[0] if entry else None

    def list_rules(self) -> list[tuple[Any, ...]]:
        rows = [
            (rid, rule, count, en)
            for rid, (rule, count, en) in self._rules.items()
        ]
        rows.sort(key=lambda t: (t[1].priority, t[0]))
        return rows

    def delete_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)

    def record_match(self, rule_id: str) -> int:
        if rule_id in self._rules:
            rule, count, en = self._rules[rule_id]
            self._rules[rule_id] = (rule, count + 1, en)
            return cast(int, count + 1)
        return 0

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        if rule_id in self._rules:
            rule, count, _ = self._rules[rule_id]
            self._rules[rule_id] = (rule, count, enabled)


class _FakeIncidentStore:
    def __init__(self, incidents) -> None:
        self._incidents = list(incidents)

    def list_open_incidents(self, account_id=None):
        if account_id is None:
            return list(self._incidents)
        return [i for i in self._incidents if i.account_id == account_id]

    def get_incident(self, correlation_id):
        return next(
            (i for i in self._incidents if i.correlation_id == correlation_id), None
        )

    def put_incident(self, incident) -> None:
        self._incidents = [
            incident if i.correlation_id == incident.correlation_id else i
            for i in self._incidents
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _incident(
    cid: str = "c-123",
    state: IncidentState = IncidentState.TRIGGERED,
) -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id=cid,
        account_id="123456789012",
        region="us-east-1",
        app_name="checkout-api",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        state=state,
        alarm_name="prod-checkout-5xx",
        environment="prod",
        deployment_id="dep-1",
        created_at=now,
        updated_at=now,
    )


def _policy(policy_id: str = "p-default") -> EscalationPolicy:
    return EscalationPolicy(
        policy_id=policy_id,
        name=f"Policy {policy_id}",
        team="platform",
        steps=[
            EscalationStep(
                step_index=0,
                roles=["oncall-eng"],
                timeout_minutes=15,
            )
        ],
    )


def _client(
    incident_store=None,
    routing_rule_store=None,
    routing_baseline=None,
    hub_config=None,
) -> TestClient:

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = incident_store or _FakeIncidentStore([_incident()])
    app_obj._routing_rule_store = (
        routing_rule_store if routing_rule_store is not None else _FakeRoutingRuleStore()
    )
    app_obj._routing_baseline = routing_baseline if routing_baseline is not None else []
    app_obj._ignore_rule_store = None
    app_obj._ignore_baseline = []
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._settings_store = None
    app_obj._schedule_store = None
    app_obj._config = hub_config
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


# ---------------------------------------------------------------------------
# Auth fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


# ---------------------------------------------------------------------------
# Tests: writer gating
# ---------------------------------------------------------------------------


def test_create_routing_rule_requires_auth():
    c = _client()
    r = c.post("/routing-rules", json={"escalation_policy_id": "p1", "priority": 10})
    assert r.status_code == 403


def test_update_routing_rule_requires_auth():
    c = _client()
    assert c.put("/routing-rules/some-id", json={"priority": 5}).status_code == 403


def test_delete_routing_rule_requires_auth():
    c = _client()
    assert c.delete("/routing-rules/some-id").status_code == 403


def test_route_incident_requires_auth():
    c = _client()
    assert c.post("/incidents/c-123/route", json={"escalation_policy_id": "p1"}).status_code == 403


# ---------------------------------------------------------------------------
# Tests: POST /routing-rules validation
# ---------------------------------------------------------------------------


def test_create_routing_rule_missing_policy_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    r = c.post("/routing-rules", json={"priority": 10})
    assert r.status_code == 422
    assert "escalation_policy_id" in r.json()["detail"]


def test_create_routing_rule_missing_priority_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    r = c.post("/routing-rules", json={"escalation_policy_id": "p1"})
    assert r.status_code == 422
    assert "priority" in r.json()["detail"]


def test_create_routing_rule_bad_regex_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    r = c.post(
        "/routing-rules",
        json={
            "escalation_policy_id": "p1",
            "priority": 10,
            "alarm_name_regex": "[invalid-regex(",
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Tests: CRUD round-trip
# ---------------------------------------------------------------------------


def test_create_and_list_routing_rules(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_DEV_USER", "alice")
    c = _client()
    # Start empty
    assert c.get("/routing-rules").json()["rules"] == []
    # Create
    r = c.post(
        "/routing-rules",
        json={
            "escalation_policy_id": "p-platform",
            "priority": 10,
            "alarm_name_prefix": "prod-",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    rule_id = body["rule_id"]
    # List — should appear with match_count and enabled
    rules = c.get("/routing-rules").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == rule_id
    assert rules[0]["alarm_name_prefix"] == "prod-"
    assert rules[0]["escalation_policy_id"] == "p-platform"
    assert rules[0]["match_count"] == 0
    assert rules[0]["enabled"] is True


def test_update_routing_rule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    rid = c.post(
        "/routing-rules",
        json={"escalation_policy_id": "p1", "priority": 20, "alarm_name_prefix": "test-"},
    ).json()["rule_id"]
    # Update severity_override
    r = c.put(f"/routing-rules/{rid}", json={"severity_override": "SEV1"})
    assert r.status_code == 200
    rules = c.get("/routing-rules").json()["rules"]
    assert rules[0]["severity_override"] == "SEV1"
    assert rules[0]["alarm_name_prefix"] == "test-"  # unchanged


def test_update_routing_rule_bad_data_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    rid = c.post(
        "/routing-rules",
        json={"escalation_policy_id": "p1", "priority": 5},
    ).json()["rule_id"]
    r = c.put(f"/routing-rules/{rid}", json={"alarm_name_regex": "[(bad"})
    assert r.status_code == 422


def test_update_missing_routing_rule_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.put("/routing-rules/no-such-id", json={"priority": 5}).status_code == 404


def test_delete_routing_rule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    rid = c.post(
        "/routing-rules",
        json={"escalation_policy_id": "p1", "priority": 30},
    ).json()["rule_id"]
    r = c.delete(f"/routing-rules/{rid}")
    assert r.status_code == 200
    assert r.json()["deleted"] == rid
    assert c.get("/routing-rules").json()["rules"] == []


def test_delete_missing_routing_rule_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.delete("/routing-rules/ghost").status_code == 404


# ---------------------------------------------------------------------------
# Tests: POST /incidents/{id}/route
# ---------------------------------------------------------------------------


def test_route_incident_creates_rule_prefilled_from_incident(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_DEV_USER", "ops-user")
    inc_store = _FakeIncidentStore([_incident()])
    rule_store = _FakeRoutingRuleStore()
    c = _client(incident_store=inc_store, routing_rule_store=rule_store)

    r = c.post(
        "/incidents/c-123/route",
        json={"escalation_policy_id": "p-sre"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    rule_id = body["rule_id"]

    rules = c.get("/routing-rules").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == rule_id
    assert rules[0]["escalation_policy_id"] == "p-sre"
    # Default matcher is alarm_name_prefix from incident.alarm_name
    assert rules[0]["alarm_name_prefix"] == "prod-checkout-5xx"


def test_route_incident_does_not_resolve_incident(monkeypatch):
    """Routing rules affect FUTURE alarms only; the current incident stays as-is."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    inc_store = _FakeIncidentStore([_incident()])
    c = _client(incident_store=inc_store)

    c.post("/incidents/c-123/route", json={"escalation_policy_id": "p-sre"})
    # Incident state must NOT be changed
    stored_inc = inc_store.get_incident("c-123")
    assert stored_inc.state == IncidentState.TRIGGERED


def test_route_incident_missing_policy_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    r = c.post("/incidents/c-123/route", json={})
    assert r.status_code == 422
    assert "escalation_policy_id" in r.json()["detail"]


def test_route_missing_incident_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.post("/incidents/no-such/route", json={"escalation_policy_id": "p1"}).status_code == 404


def test_route_incident_custom_priority_and_severity(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    inc_store = _FakeIncidentStore([_incident()])
    c = _client(incident_store=inc_store)

    r = c.post(
        "/incidents/c-123/route",
        json={
            "escalation_policy_id": "p-sre",
            "priority": 5,
            "severity_override": "SEV1",
        },
    )
    assert r.status_code == 200
    rules = c.get("/routing-rules").json()["rules"]
    assert rules[0]["priority"] == 5
    assert rules[0]["severity_override"] == "SEV1"


# ---------------------------------------------------------------------------
# Tests: GET /escalation-policies
# ---------------------------------------------------------------------------


def test_escalation_policies_no_config():
    c = _client(hub_config=None)
    body = c.get("/escalation-policies").json()
    assert body["policies"] == []


def test_escalation_policies_returns_configured_policies():
    from relay.config.schema import EscalationConfig

    class _FakeConfig:
        escalation = EscalationConfig(policies=[_policy("p-alpha"), _policy("p-beta")])

    c = _client(hub_config=_FakeConfig())
    body = c.get("/escalation-policies").json()
    assert len(body["policies"]) == 2
    policy_ids = {p["policy_id"] for p in body["policies"]}
    assert "p-alpha" in policy_ids
    assert "p-beta" in policy_ids
    # Each policy has a name
    for p in body["policies"]:
        assert p["name"]


# ---------------------------------------------------------------------------
# Tests: GET /routing-rules/deviation
# ---------------------------------------------------------------------------


def test_routing_deviation_empty_baseline_no_rules():
    c = _client(routing_baseline=[])
    body = c.get("/routing-rules/deviation").json()
    assert body["deviates"] is False
    assert body["db_count"] == 0
    assert body["baseline_count"] == 0
    assert body["added"] == []
    assert body["removed"] == []


def test_routing_deviation_added_rule(monkeypatch):
    """A UI-created rule absent from baseline → deviates=True."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    rule_store = _FakeRoutingRuleStore()
    c = _client(routing_rule_store=rule_store, routing_baseline=[])
    c.post(
        "/routing-rules",
        json={"escalation_policy_id": "p-new", "priority": 10, "alarm_name_prefix": "x-"},
    )
    body = c.get("/routing-rules/deviation").json()
    assert body["deviates"] is True
    assert body["db_count"] == 1
    assert body["baseline_count"] == 0
    assert len(body["added"]) == 1
    assert body["removed"] == []


def test_routing_deviation_removed_rule():
    """A rule in baseline but not in DB → deviates=True with a removed entry."""
    baseline_rule = RoutingRule(
        rule_id="r-old",
        priority=5,
        escalation_policy_id="p-gone",
    )
    c = _client(routing_rule_store=_FakeRoutingRuleStore(), routing_baseline=[baseline_rule])
    body = c.get("/routing-rules/deviation").json()
    assert body["deviates"] is True
    assert len(body["removed"]) == 1
    assert body["removed"][0]["rule_id"] == "r-old"


# ---------------------------------------------------------------------------
# Tests: GET /routing-rules/download
# ---------------------------------------------------------------------------


def test_download_routing_rules_empty():
    c = _client(routing_rule_store=_FakeRoutingRuleStore())
    r = c.get("/routing-rules/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "routing-rules.yaml" in r.headers.get("content-disposition", "")
    text = r.text
    assert "rules:" in text


def test_download_routing_rules_with_content(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    rule_store = _FakeRoutingRuleStore()
    c = _client(routing_rule_store=rule_store)
    c.post(
        "/routing-rules",
        json={"escalation_policy_id": "p-download", "priority": 15, "alarm_name_prefix": "dl-"},
    )
    r = c.get("/routing-rules/download")
    assert r.status_code == 200
    text = r.text
    assert "p-download" in text
    assert "Relay routing rules" in text


# ---------------------------------------------------------------------------
# Tests: seeding logic (_seed_routing_rules helper)
# ---------------------------------------------------------------------------


def test_seed_routing_empty_store_populates_rules():
    """Empty store + config rules → store gets seeded; seeded=True returned."""

    class _FakeConfig:
        class routing:
            rules = [
                RoutingRule(
                    rule_id="r-1", priority=10, escalation_policy_id="p-a"
                ),
                RoutingRule(
                    rule_id="r-2", priority=20, escalation_policy_id="p-b"
                ),
            ]

    store = _FakeRoutingRuleStore()
    baseline, seeded = _seed_routing_rules(store, _FakeConfig())
    assert seeded is True
    assert len(baseline) == 2
    stored = store.list_rules()
    assert len(stored) == 2


def test_seed_routing_nonempty_store_does_not_overwrite():
    """Store already has rules → DB wins; seeded=False; baseline still from config."""

    class _FakeConfig:
        class routing:
            rules = [
                RoutingRule(rule_id="cfg-r", priority=5, escalation_policy_id="p-cfg")
            ]

    store = _FakeRoutingRuleStore()
    store.put_rule(
        RoutingRule(rule_id="existing-r", priority=1, escalation_policy_id="p-existing"),
        rule_id="existing-r",
    )

    baseline, seeded = _seed_routing_rules(store, _FakeConfig())
    assert seeded is False
    assert len(baseline) == 1
    assert baseline[0].rule_id == "cfg-r"
    # Store still has only the original rule
    stored = store.list_rules()
    assert len(stored) == 1
    assert stored[0][1].rule_id == "existing-r"


def test_seed_routing_none_config():
    """None config → empty baseline, seeded=False."""
    store = _FakeRoutingRuleStore()
    baseline, seeded = _seed_routing_rules(store, None)
    assert baseline == []
    assert seeded is False


def test_seed_routing_config_no_routing_block():
    """Config with no routing block → empty baseline, seeded=False."""

    class _FakeConfig:
        routing = None

    store = _FakeRoutingRuleStore()
    baseline, seeded = _seed_routing_rules(store, _FakeConfig())
    assert baseline == []
    assert seeded is False
