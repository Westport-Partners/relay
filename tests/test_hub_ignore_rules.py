"""Tests for the Hub ignore-rules API endpoints (Phase 3).

Covers:
- POST /incidents/{id}/ignore → creates a rule + auto-resolves the incident
- POST /rules validation (no matcher → 422)
- POST /rules + GET /rules round-trip; PUT updates; DELETE removes
- GET /rules/deviation reports deviation after a UI-created rule
- GET /rules/download returns YAML with ignore block + attachment header
- Seeding: empty store → seeds from config; non-empty → DB wins
- Writer gating (POST/PUT/DELETE → 403 without auth)
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from relay.core.model import (
    Incident,
    IncidentState,
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
    _seed_ignore_rules,
)

# ---------------------------------------------------------------------------
# Fake stores
# ---------------------------------------------------------------------------


class _FakeIncidentStore:
    def __init__(self, incidents: list[Incident]) -> None:
        self._incidents = list(incidents)

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        if account_id is None:
            return list(self._incidents)
        return [i for i in self._incidents if i.account_id == account_id]

    def get_incident(self, correlation_id: str) -> Incident | None:
        return next(
            (i for i in self._incidents if i.correlation_id == correlation_id), None
        )

    def put_incident(self, incident: Incident) -> None:
        self._incidents = [
            incident if i.correlation_id == incident.correlation_id else i
            for i in self._incidents
        ]
        if incident.correlation_id not in {i.correlation_id for i in self._incidents}:
            self._incidents.append(incident)


class _FakeIgnoreRuleStore:
    """In-memory implementation of DynamoIgnoreRuleStore's public interface."""

    def __init__(self) -> None:
        self._rules: dict[str, tuple] = {}  # rule_id -> (IgnoreRule, trigger_count)
        self._counter: int = 0

    def put_rule(self, rule, rule_id: str | None = None) -> str:
        import uuid
        if rule_id is None:
            rule_id = rule.name or str(uuid.uuid4())
        existing_count = self._rules.get(rule_id, (None, 0))[1]
        self._rules[rule_id] = (rule, existing_count)
        return rule_id

    def get_rule(self, rule_id: str):
        entry = self._rules.get(rule_id)
        return entry[0] if entry else None

    def list_rules(self) -> list[tuple]:
        return [
            (rid, rule, count)
            for rid, (rule, count) in sorted(self._rules.items())
        ]

    def delete_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)

    def record_trigger(self, rule_id: str) -> int:
        if rule_id in self._rules:
            rule, count = self._rules[rule_id]
            self._rules[rule_id] = (rule, count + 1)
            return count + 1
        return 0


# ---------------------------------------------------------------------------
# Helper: build a test incident
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


# ---------------------------------------------------------------------------
# Helper: build a TestClient with only the stores we need
# ---------------------------------------------------------------------------


def _client(
    incident_store=None,
    ignore_rule_store=None,
    ignore_baseline=None,
) -> TestClient:
    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = incident_store or _FakeIncidentStore([_incident()])
    app_obj._ignore_rule_store = ignore_rule_store if ignore_rule_store is not None else _FakeIgnoreRuleStore()
    app_obj._ignore_baseline = ignore_baseline if ignore_baseline is not None else []
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._settings_store = None
    app_obj._schedule_store = None
    app_obj._config = None
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
# Auth fixture (mirrors test_hub_ui_endpoints.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


# ---------------------------------------------------------------------------
# Tests: writer gating
# ---------------------------------------------------------------------------


def test_create_rule_requires_auth():
    c = _client()
    r = c.post("/rules", json={"app_name": "my-svc"})
    assert r.status_code == 403


def test_update_rule_requires_auth():
    c = _client()
    assert c.put("/rules/some-id", json={"note": "x"}).status_code == 403


def test_delete_rule_requires_auth():
    c = _client()
    assert c.delete("/rules/some-id").status_code == 403


def test_ignore_incident_requires_auth():
    c = _client()
    assert c.post("/incidents/c-123/ignore", json={}).status_code == 403


# ---------------------------------------------------------------------------
# Tests: POST /rules validation
# ---------------------------------------------------------------------------


def test_create_rule_no_matcher_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    # Only non-matcher fields → 422
    r = c.post("/rules", json={"name": "bad-rule", "note": "no matcher"})
    assert r.status_code == 422


def test_create_rule_empty_body_422(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    r = c.post("/rules", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Tests: CRUD round-trip
# ---------------------------------------------------------------------------


def test_create_and_list_rules(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_DEV_USER", "alice")
    c = _client()
    # Start empty
    assert c.get("/rules").json()["rules"] == []
    # Create
    r = c.post("/rules", json={"app_name": "my-svc", "note": "test rule"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    rule_id = body["rule_id"]
    # List — should appear
    rules = c.get("/rules").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == rule_id
    assert rules[0]["app_name"] == "my-svc"
    assert rules[0]["created_by"] == "alice"


def test_update_rule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    # Create first
    rid = c.post("/rules", json={"app_name": "svc-a"}).json()["rule_id"]
    # Update note
    r = c.put(f"/rules/{rid}", json={"note": "updated note"})
    assert r.status_code == 200
    rules = c.get("/rules").json()["rules"]
    assert rules[0]["note"] == "updated note"
    assert rules[0]["app_name"] == "svc-a"  # unchanged


def test_update_missing_rule_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.put("/rules/no-such-id", json={"note": "x"}).status_code == 404


def test_delete_rule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    rid = c.post("/rules", json={"app_name": "svc-b"}).json()["rule_id"]
    r = c.delete(f"/rules/{rid}")
    assert r.status_code == 200
    assert r.json()["deleted"] == rid
    assert c.get("/rules").json()["rules"] == []


def test_delete_missing_rule_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.delete("/rules/ghost").status_code == 404


# ---------------------------------------------------------------------------
# Tests: POST /incidents/{id}/ignore
# ---------------------------------------------------------------------------


def test_ignore_incident_creates_rule_and_resolves(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_DEV_USER", "ops-user")
    inc_store = _FakeIncidentStore([_incident()])
    rule_store = _FakeIgnoreRuleStore()
    c = _client(incident_store=inc_store, ignore_rule_store=rule_store)

    r = c.post("/incidents/c-123/ignore", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "RESOLVED"
    rule_id = body["rule_id"]

    # Rule must exist in the store
    rules = c.get("/rules").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == rule_id
    assert rules[0]["app_name"] == "checkout-api"
    assert rules[0]["alarm_name"] == "prod-checkout-5xx"

    # Incident must be RESOLVED with an "ignored" timeline event
    stored_inc = inc_store.get_incident("c-123")
    assert stored_inc.state == IncidentState.RESOLVED
    ignored_events = [e for e in stored_inc.timeline if e.event_type == "ignored"]
    assert len(ignored_events) == 1
    assert ignored_events[0].detail["via"] == "hub-ui"
    assert ignored_events[0].detail["ignore_rule_id"] == rule_id


def test_ignore_incident_with_prefix_override(monkeypatch):
    """Caller can broaden the rule with alarm_name_prefix; exact alarm_name is dropped."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    inc_store = _FakeIncidentStore([_incident()])
    rule_store = _FakeIgnoreRuleStore()
    c = _client(incident_store=inc_store, ignore_rule_store=rule_store)

    r = c.post("/incidents/c-123/ignore", json={"alarm_name_prefix": "prod-checkout-"})
    assert r.status_code == 200

    rules = c.get("/rules").json()["rules"]
    assert len(rules) == 1
    assert rules[0]["alarm_name_prefix"] == "prod-checkout-"
    # Exact alarm_name should NOT be set (prefix is the broader match)
    assert rules[0].get("alarm_name") is None


def test_ignore_missing_incident_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client()
    assert c.post("/incidents/no-such/ignore", json={}).status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /rules/deviation
# ---------------------------------------------------------------------------


def test_deviation_empty_baseline_no_rules():
    c = _client(ignore_baseline=[])
    body = c.get("/rules/deviation").json()
    assert body["deviates"] is False
    assert body["db_count"] == 0
    assert body["baseline_count"] == 0
    assert body["added"] == []
    assert body["removed"] == []


def test_deviation_added_rule(monkeypatch):
    """A UI-created rule that isn't in the (empty) baseline → deviates=True."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    rule_store = _FakeIgnoreRuleStore()
    c = _client(ignore_rule_store=rule_store, ignore_baseline=[])
    # Add a rule via the API
    c.post("/rules", json={"app_name": "new-svc"})
    body = c.get("/rules/deviation").json()
    assert body["deviates"] is True
    assert body["db_count"] == 1
    assert body["baseline_count"] == 0
    assert len(body["added"]) == 1
    assert body["added"][0]["app_name"] == "new-svc"
    assert body["removed"] == []


def test_deviation_baseline_matches_db(monkeypatch):
    """When DB and baseline have the same rule, no deviation."""
    from relay.config.schema import IgnoreRule

    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    baseline_rule = IgnoreRule(app_name="svc-x", note="baseline")
    rule_store = _FakeIgnoreRuleStore()
    rule_store.put_rule(baseline_rule)
    c = _client(ignore_rule_store=rule_store, ignore_baseline=[baseline_rule])
    body = c.get("/rules/deviation").json()
    assert body["deviates"] is False


def test_deviation_removed_rule(monkeypatch):
    """A rule in baseline but not in DB → deviates=True with a removed entry."""
    from relay.config.schema import IgnoreRule

    baseline_rule = IgnoreRule(app_name="svc-y", note="gone")
    # Empty store (rule was deleted) but baseline has the rule.
    c = _client(ignore_rule_store=_FakeIgnoreRuleStore(), ignore_baseline=[baseline_rule])
    body = c.get("/rules/deviation").json()
    assert body["deviates"] is True
    assert len(body["removed"]) == 1
    assert body["removed"][0]["app_name"] == "svc-y"


# ---------------------------------------------------------------------------
# Tests: GET /rules/download
# ---------------------------------------------------------------------------


def test_download_rules_empty(monkeypatch):
    c = _client(ignore_rule_store=_FakeIgnoreRuleStore())
    r = c.get("/rules/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "routing.yaml" in r.headers.get("content-disposition", "")
    text = r.text
    assert "ignore:" in text
    assert "rules:" in text


def test_download_rules_with_content(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    rule_store = _FakeIgnoreRuleStore()
    c = _client(ignore_rule_store=rule_store)
    c.post("/rules", json={"app_name": "svc-download", "note": "export me"})
    r = c.get("/rules/download")
    assert r.status_code == 200
    text = r.text
    assert "svc-download" in text
    assert "export me" in text
    # File header comment should be present
    assert "Relay ignore rules" in text


# ---------------------------------------------------------------------------
# Tests: seeding logic (_seed_ignore_rules helper)
# ---------------------------------------------------------------------------


def test_seed_empty_store_populates_rules():
    """Empty store + config rules → store gets seeded; seeded=True returned."""
    from relay.config.schema import IgnoreConfig, IgnoreRule

    class _FakeConfig:
        class routing:
            ignore = IgnoreConfig(
                rules=[
                    IgnoreRule(app_name="seeded-app", note="from config"),
                    IgnoreRule(alarm_name="noisy-alarm"),
                ]
            )

    store = _FakeIgnoreRuleStore()
    baseline, seeded = _seed_ignore_rules(store, _FakeConfig())
    assert seeded is True
    assert len(baseline) == 2
    stored = store.list_rules()
    assert len(stored) == 2


def test_seed_nonempty_store_does_not_overwrite():
    """Store already has rules → DB wins; seeded=False; baseline still from config."""
    from relay.config.schema import IgnoreConfig, IgnoreRule

    class _FakeConfig:
        class routing:
            ignore = IgnoreConfig(
                rules=[IgnoreRule(app_name="config-app")]
            )

    store = _FakeIgnoreRuleStore()
    # Pre-populate the store
    from relay.config.schema import IgnoreRule
    store.put_rule(IgnoreRule(app_name="existing-app"))

    baseline, seeded = _seed_ignore_rules(store, _FakeConfig())
    assert seeded is False
    # Baseline still reflects config (for deviation detection)
    assert len(baseline) == 1
    assert baseline[0].app_name == "config-app"
    # Store still has only the original rule (not overwritten)
    stored = store.list_rules()
    assert len(stored) == 1
    assert stored[0][1].app_name == "existing-app"


def test_seed_none_config():
    """None config → empty baseline, seeded=False."""
    store = _FakeIgnoreRuleStore()
    baseline, seeded = _seed_ignore_rules(store, None)
    assert baseline == []
    assert seeded is False


def test_seed_config_no_ignore_block():
    """Config with no ignore block → empty baseline, seeded=False."""

    class _FakeConfig:
        class routing:
            ignore = None

    store = _FakeIgnoreRuleStore()
    baseline, seeded = _seed_ignore_rules(store, _FakeConfig())
    assert baseline == []
    assert seeded is False
