"""Tests for the Hub UI incident list/detail/history/resolve/acknowledge/auth/config endpoints."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from relay.core.model import (
    Incident,
    IncidentState,
    Severity,
    SignalSource,
    Stream,
    TimelineEvent,
)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from relay.core.model import Contact  # noqa: E402
from relay.hub.app import HubApp, HubState, SSEPublisher  # noqa: E402


def _incident(
    correlation_id: str = "c-123",
    state: IncidentState = IncidentState.TRIGGERED,
) -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id=correlation_id,
        account_id="123456789012",
        region="us-east-1",
        app_name="checkout-api",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        state=state,
        alarm_name="prod-checkout-5xx",
        environment="prod",
        deployment_id="dep-1",
        service_path=["Payments", "Checkout", "API", "checkout-api"],
        created_at=now,
        updated_at=now,
        timeline=[
            TimelineEvent(
                event_id="e1",
                incident_id="c-123",
                stream=Stream.TEAM,
                occurred_at=now,
                actor="relay",
                event_type="triggered",
                detail={"reason": "alarm ALARM"},
            )
        ],
    )


class _FakeIncidentStore:
    def __init__(self, incidents: list[Incident]) -> None:
        self._incidents = incidents

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        open_states = {
            IncidentState.TRIGGERED,
            IncidentState.ACKNOWLEDGED,
            IncidentState.ESCALATED,
        }
        incidents = [i for i in self._incidents if i.state in open_states]
        if account_id is None:
            return incidents
        return [i for i in incidents if i.account_id == account_id]

    def list_incidents(self) -> list[Incident]:
        return list(self._incidents)

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


def _client(incident_store) -> TestClient:
    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = incident_store
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


def test_list_incidents_returns_summaries():
    c = _client(_FakeIncidentStore([_incident()]))
    r = c.get("/incidents")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["app_name"] == "checkout-api"
    assert body[0]["severity"] == "SEV2"
    assert body[0]["state"] == "TRIGGERED"


def test_history_returns_only_terminal_state_incidents():
    store = _FakeIncidentStore(
        [
            _incident("open-1", IncidentState.TRIGGERED),
            _incident("open-2", IncidentState.ACKNOWLEDGED),
            _incident("open-3", IncidentState.ESCALATED),
            _incident("done-1", IncidentState.RESOLVED),
            _incident("done-2", IncidentState.CLOSED),
        ]
    )
    c = _client(store)

    hist = c.get("/incidents/history")
    assert hist.status_code == 200
    hist_ids = {i["correlation_id"] for i in hist.json()}
    assert hist_ids == {"done-1", "done-2"}
    assert all(i["state"] in {"RESOLVED", "CLOSED"} for i in hist.json())

    # Open tab still returns only open incidents.
    open_ids = {i["correlation_id"] for i in c.get("/incidents").json()}
    assert open_ids == {"open-1", "open-2", "open-3"}


def test_list_incidents_carries_metrics_enrichment_fields():
    """Open-incident payloads gain acknowledged_at/resolved_at/signal_source/
    synthetic so the client can recompute env-scoped KPIs (issue #40)."""
    now = datetime.now(UTC)
    inc = _incident()
    inc.acknowledged_at = now
    c = _client(_FakeIncidentStore([inc]))
    item = c.get("/incidents").json()[0]
    # acknowledged_at is an ISO string; the open incident has no resolution yet.
    assert item["acknowledged_at"] == now.isoformat()
    assert item["resolved_at"] is None
    assert item["signal_source"] == "CLOUDWATCH_ALARM"
    assert item["synthetic"] is False


def test_list_incidents_enrichment_null_when_unacked():
    """An unacknowledged open incident reports null acknowledged_at/resolved_at."""
    c = _client(_FakeIncidentStore([_incident()]))
    item = c.get("/incidents").json()[0]
    assert item["acknowledged_at"] is None
    assert item["resolved_at"] is None


def test_history_carries_metrics_enrichment_with_resolved_at():
    """Terminal incidents serialize a derived resolved_at (from the timeline
    event, falling back to updated_at) plus the other three new fields."""
    inc = _incident("done-1", IncidentState.RESOLVED)
    # No explicit resolved timeline event → derivation falls back to updated_at.
    item = next(
        i
        for i in _client(_FakeIncidentStore([inc])).get("/incidents/history").json()
        if i["correlation_id"] == "done-1"
    )
    assert item["resolved_at"] == inc.updated_at.isoformat()
    assert item["signal_source"] == "CLOUDWATCH_ALARM"
    assert item["synthetic"] is False
    assert item["acknowledged_at"] is None


def test_get_incident_returns_full_record_with_timeline():
    c = _client(_FakeIncidentStore([_incident()]))
    r = c.get("/incidents/c-123")
    assert r.status_code == 200
    body = r.json()
    assert body["correlation_id"] == "c-123"
    assert len(body["timeline"]) == 1
    assert body["timeline"][0]["detail"] == {"reason": "alarm ALARM"}


def test_get_incident_returns_tags_and_deployment_metadata():
    """Incident detail endpoint must expose tags and deployment_metadata for the drawer."""
    now = datetime.now(UTC)
    inc = Incident(
        correlation_id="c-tags",
        account_id="111122223333",
        region="us-west-2",
        app_name="tag-test-svc",
        severity=Severity.SEV3,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        state=IncidentState.TRIGGERED,
        alarm_name="tag-test-alarm",
        environment="test",
        deployment_id="dep-tags",
        created_at=now,
        updated_at=now,
        tags={"COMPONENT_ID": "checkout", "GIT_SHA": "abc123def456"},
        deployment_metadata={
            "gitlab_project": "org/checkout-api",
            "git_sha": "abc123def456feedbeef",
            "pipeline_url": "https://gitlab.example.com/org/checkout-api/-/pipelines/42",
        },
    )
    c = _client(_FakeIncidentStore([inc]))
    r = c.get("/incidents/c-tags")
    assert r.status_code == 200
    body = r.json()
    assert "tags" in body
    assert body["tags"]["COMPONENT_ID"] == "checkout"
    assert "deployment_metadata" in body
    assert body["deployment_metadata"]["gitlab_project"] == "org/checkout-api"
    assert body["deployment_metadata"]["pipeline_url"].startswith("https://")


def test_get_missing_incident_404():
    c = _client(_FakeIncidentStore([_incident()]))
    assert c.get("/incidents/nope").status_code == 404


def test_list_incidents_empty_store():
    c = _client(_FakeIncidentStore([]))
    r = c.get("/incidents")
    assert r.status_code == 200
    assert r.json() == []


def test_dashboard_serves_incidents_view_and_footer():
    c = _client(_FakeIncidentStore([]))
    html = c.get("/").text
    assert "view-incidents" in html
    assert "Westport Partners" in html


def test_dashboard_modules_served_with_no_cache():
    """Dashboard ES modules are versionless URLs, so they must be served with
    Cache-Control: no-cache or a browser serves a stale module after redeploy."""
    c = _client(_FakeIncidentStore([]))
    r = c.get("/static/dashboard/main.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


# ---------------------------------------------------------------------------
# Auth + write endpoints (acknowledge, contacts)
# ---------------------------------------------------------------------------


class _FakeContactStore:
    def __init__(self, contacts: list[Contact] | None = None) -> None:
        self._db = {c.contact_id: c for c in (contacts or [])}

    def list_contacts(self) -> list[Contact]:
        return list(self._db.values())

    def get_contact(self, cid: str) -> Contact | None:
        return self._db.get(cid)

    def put_contact(self, c: Contact) -> None:
        self._db[c.contact_id] = c

    def delete_contact(self, cid: str) -> None:
        self._db.pop(cid, None)


def _client_full(incident_store=None, contact_store=None) -> TestClient:
    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = incident_store or _FakeIncidentStore([_incident()])
    app_obj._contact_store = contact_store or _FakeContactStore()
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


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


def test_auth_default_none_is_readonly():
    c = _client_full()
    body = c.get("/auth").json()
    assert body["mode"] == "none"
    assert body["can_write"] is False
    assert body["subject"] is None
    # Timezone defaults to UTC when RELAY_TZ is unset.
    assert body["timezone"] == "UTC"


def test_auth_reports_team_timezone(monkeypatch):
    monkeypatch.setenv("RELAY_TZ", "America/New_York")
    c = _client_full()
    assert c.get("/auth").json()["timezone"] == "America/New_York"


def test_auth_reports_hub_scope_default_local(monkeypatch):
    # Unset scope → 'local' (a Team Hub) so the UI labels it accordingly.
    monkeypatch.delenv("RELAY_HUB_SCOPE", raising=False)
    c = _client_full()
    assert c.get("/auth").json()["hub_scope"] == "local"


def test_auth_reports_hub_scope_central(monkeypatch):
    monkeypatch.setenv("RELAY_HUB_SCOPE", "central")
    c = _client_full()
    assert c.get("/auth").json()["hub_scope"] == "central"


def test_config_reports_build_and_runtime(monkeypatch):
    monkeypatch.setenv("RELAY_BUILD_SHA", "deadbee")
    monkeypatch.setenv("RELAY_HUB_SCALING", "on-demand")
    monkeypatch.setenv("RELAY_AI_ENABLED", "true")
    monkeypatch.setenv("RELAY_AI_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    c = _client_full()
    body = c.get("/config").json()
    assert body["build"]["git_sha"] == "deadbee"
    assert body["build"]["version"]  # from relay.__version__
    assert body["runtime"]["scaling"] == "on-demand"
    assert body["features"]["ai_enabled"] is True
    # provider defaults to bedrock when enabled but RELAY_AI_PROVIDER unset
    assert body["features"]["ai_provider"] == "bedrock"
    assert "claude" in body["features"]["ai_model"]
    # No secrets leak into the payload.
    assert "secret" not in str(body).lower()


def test_config_reports_openai_provider(monkeypatch):
    monkeypatch.setenv("RELAY_AI_ENABLED", "true")
    monkeypatch.setenv("RELAY_AI_PROVIDER", "openai")
    c = _client_full()
    body = c.get("/config").json()
    assert body["features"]["ai_provider"] == "openai"


def test_config_hides_ai_model_when_disabled(monkeypatch):
    monkeypatch.delenv("RELAY_AI_ENABLED", raising=False)
    c = _client_full()
    body = c.get("/config").json()
    assert body["features"]["ai_enabled"] is False
    assert body["features"]["ai_model"] == ""
    assert body["features"]["ai_provider"] == ""


def test_acknowledge_blocked_without_auth():
    c = _client_full()
    assert c.post("/incidents/c-123/acknowledge").status_code == 403


def test_acknowledge_succeeds_in_dev_mode(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_DEV_USER", "tester")
    c = _client_full()
    r = c.post("/incidents/c-123/acknowledge")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ACKNOWLEDGED"
    assert body["acknowledged_by"] == "tester"


def test_acknowledge_missing_incident_404(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_full()
    assert c.post("/incidents/nope/acknowledge").status_code == 404


def test_contacts_crud_requires_auth_for_writes(monkeypatch):
    store = _FakeContactStore([Contact(contact_id="cnt-a", name="Alice", email="a@x.com")])
    c = _client_full(contact_store=store)
    # Read is open.
    assert [x["name"] for x in c.get("/contacts").json()] == ["Alice"]
    bob = {"contact_id": "cnt-b", "name": "Bob", "phone": "+15553334444"}
    # Writes blocked without auth.
    assert c.post("/contacts", json=bob).status_code == 403
    assert c.delete("/contacts/cnt-a").status_code == 403
    # With auth, create + delete work.
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    assert c.post("/contacts", json=bob).status_code == 200
    names = sorted(x["name"] for x in c.get("/contacts").json())
    assert names == ["Alice", "Bob"]
    assert c.delete("/contacts/cnt-b").status_code == 200


def test_oncall_returns_schedule_backed_response():
    c = _client_full()
    body = c.get("/oncall").json()
    assert "now_on_call" in body


# ---------------------------------------------------------------------------
# resolve / test-page / history / override
# ---------------------------------------------------------------------------


class _FakeNotifier:
    def __init__(self):
        self.calls = []

    def publish_test(self, *, phone, email_topic_arn, message):
        self.calls.append((phone, email_topic_arn, message))
        return {"sms": bool(phone), "topic": bool(email_topic_arn)}


def _client_actions(monkeypatch, *, contacts=None):
    """Client with incident + contact stores, notifier."""
    inc = _incident()

    class IncStore(_FakeIncidentStore):
        def list_incidents(self, account_id=None):
            return list(self._incidents)

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = IncStore([inc])
    app_obj._contact_store = _FakeContactStore(contacts or [])
    app_obj._notifier = _FakeNotifier()
    app_obj._paging_topic_arn = "arn:topic"
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


def test_resolve_requires_auth_then_works(monkeypatch):
    c = _client_actions(monkeypatch)
    assert c.post("/incidents/c-123/resolve").status_code == 403
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    r = c.post("/incidents/c-123/resolve")
    assert r.status_code == 200
    assert r.json()["state"] == "RESOLVED"


def test_resolve_closes_gitlab_issue(monkeypatch):
    """Resolving via the UI dispatches RESOLVED → GitLab issue is closed."""
    from unittest.mock import MagicMock

    from relay.hub.app import HubProcessor

    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    inc = _incident()
    inc.set_ticket("gitlab_iid", "55")  # an issue was opened for this incident on TRIGGERED

    class IncStore(_FakeIncidentStore):
        def list_incidents(self, account_id=None):
            return list(self._incidents)

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = IncStore([inc])
    app_obj._contact_store = _FakeContactStore([])
    app_obj._notifier = _FakeNotifier()
    app_obj._paging_topic_arn = "arn:topic"
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    hs._org_tree = None
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    from relay.adapters.integrations.gitlab.listener import GitLabListener

    gitlab_sink = MagicMock()
    app_obj._processor = HubProcessor(
        incident_store=app_obj._incident_store,
        notifier=app_obj._notifier,
        hub_state=hs,
        sse_publisher=app_obj._sse_publisher,
        settings_store=None,
        listeners=[GitLabListener(gitlab_sink, app_obj._incident_store)],
    )

    c = TestClient(app_obj.build_fastapi_app())
    r = c.post(f"/incidents/{inc.correlation_id}/resolve")
    assert r.status_code == 200
    gitlab_sink.close_incident.assert_called_once()
    assert gitlab_sink.close_incident.call_args.args[0] == "55"


def test_acknowledge_dispatches_lifecycle_event(monkeypatch):
    """Acknowledging via the UI dispatches ACKNOWLEDGED to listeners."""
    from relay.core.lifecycle import IncidentLifecycleEvent
    from relay.hub.app import HubProcessor

    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")

    inc = _incident()

    class IncStore(_FakeIncidentStore):
        def list_incidents(self, account_id=None):
            return list(self._incidents)

    app_obj = HubApp.__new__(HubApp)
    app_obj._incident_store = IncStore([inc])
    app_obj._contact_store = _FakeContactStore([])
    app_obj._notifier = _FakeNotifier()
    app_obj._paging_topic_arn = "arn:topic"
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    hs._org_tree = None
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()

    # A spy listener captures dispatched events.
    seen = []

    class SpyListener:
        def on_event(self, *, event, incident):
            seen.append(event)

    app_obj._processor = HubProcessor(
        incident_store=app_obj._incident_store,
        notifier=app_obj._notifier,
        hub_state=hs,
        sse_publisher=app_obj._sse_publisher,
        settings_store=None,
        listeners=[SpyListener()],
    )

    c = TestClient(app_obj.build_fastapi_app())
    r = c.post(f"/incidents/{inc.correlation_id}/acknowledge")
    assert r.status_code == 200
    assert IncidentLifecycleEvent.ACKNOWLEDGED in seen


def test_history_excludes_open_incidents():
    # The seeded incident (c-123) is TRIGGERED, so it must NOT appear in history.
    c = _client_actions(None)
    r = c.get("/incidents/history")
    assert r.status_code == 200
    assert not any(i["correlation_id"] == "c-123" for i in r.json())


def test_history_not_shadowed_by_id_route():
    # /incidents/history must resolve to the history list, not be treated as a
    # correlation_id (which would 404).
    c = _client_actions(None)
    assert c.get("/incidents/history").status_code == 200


def test_test_page_requires_auth_then_fires(monkeypatch):
    contacts = [Contact(contact_id="cnt-a", name="Alice", phone="+15551112222")]
    c = _client_actions(monkeypatch, contacts=contacts)
    assert c.post("/contacts/cnt-a/test").status_code == 403
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    r = c.post("/contacts/cnt-a/test")
    assert r.status_code == 200
    assert r.json()["channels"]["sms"] is True
