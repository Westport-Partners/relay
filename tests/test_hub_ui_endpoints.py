"""Tests for the Hub UI read-only incident endpoints (/incidents, /incidents/{id})."""

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


# ---------------------------------------------------------------------------
# Auth + write endpoints (acknowledge, contacts)
# ---------------------------------------------------------------------------

from relay.core.model import Contact  # noqa: E402


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


# ---------------------------------------------------------------------------
# Settings + Teams webhook
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def get_all(self):
        return dict(self.d)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


def _client_settings(monkeypatch, settings=None):
    app_obj = HubApp.__new__(HubApp)
    app_obj._settings_store = settings if settings is not None else _FakeSettings()
    app_obj._incident_store = None
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
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


def test_settings_get_unconfigured():
    c = _client_settings(None)
    assert c.get("/settings").json()["teams_webhook_configured"] is False


def test_set_teams_webhook_requires_auth_and_https(monkeypatch):
    c = _client_settings(monkeypatch)
    # unauth
    assert c.put("/settings/teams-webhook",
                 json={"webhook_url": "https://x.webhook.office.com/y"}).status_code == 403
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    # non-https rejected
    assert c.put("/settings/teams-webhook",
                 json={"webhook_url": "http://x"}).status_code == 422
    # https accepted + masked on read
    assert c.put("/settings/teams-webhook",
                 json={"webhook_url": "https://x.webhook.office.com/abc123def456"}).json()["ok"]
    body = c.get("/settings").json()
    assert body["teams_webhook_configured"] is True
    assert body["teams_webhook_masked"].startswith("https://")
    assert "abc123def456" not in body["teams_webhook_masked"] or len(
        "https://x.webhook.office.com/abc123def456") <= 30


def test_clear_teams_webhook(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    s = _FakeSettings()
    s.set("teams_webhook_url", "https://x.webhook.office.com/y")
    c = _client_settings(monkeypatch, settings=s)
    assert c.put("/settings/teams-webhook", json={"webhook_url": ""}).json()["configured"] is False
    assert c.get("/settings").json()["teams_webhook_configured"] is False


def test_settings_get_gitlab_unconfigured():
    c = _client_settings(None)
    assert c.get("/settings").json()["gitlab_token_configured"] is False


def test_set_gitlab_token_requires_auth_then_stores_masked(monkeypatch):
    c = _client_settings(monkeypatch)
    # unauth
    assert c.put("/settings/gitlab-token",
                 json={"token": "glpat-secrettoken1234"}).status_code == 403
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    # accepted + masked on read (never echoes the full token)
    assert c.put("/settings/gitlab-token",
                 json={"token": "glpat-secrettoken1234"}).json()["ok"]
    body = c.get("/settings").json()
    assert body["gitlab_token_configured"] is True
    assert body["gitlab_token_masked"] == "…1234"
    assert "secrettoken" not in body["gitlab_token_masked"]


def test_clear_gitlab_token(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    s = _FakeSettings()
    s.set("gitlab_token", "glpat-secrettoken1234")
    c = _client_settings(monkeypatch, settings=s)
    assert c.put("/settings/gitlab-token", json={"token": ""}).json()["configured"] is False
    assert c.get("/settings").json()["gitlab_token_configured"] is False


def test_gitlab_token_test_404_when_unconfigured(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_settings(monkeypatch)
    assert c.post("/settings/gitlab-token/test").status_code == 404


def test_settings_get_servicenow_unconfigured():
    c = _client_settings(None)
    assert c.get("/settings").json()["servicenow_configured"] is False


def test_set_servicenow_credentials_requires_auth_then_stores_masked(monkeypatch):
    c = _client_settings(monkeypatch)
    creds = {
        "instance_url": "https://dev123.service-now.com",
        "username": "relay_api",
        "password": "s3cretpw7890",
    }
    # unauth
    assert c.put("/settings/servicenow-credentials", json=creds).status_code == 403
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    # accepted + masked on read (echoes URL + username, never the full password)
    assert c.put("/settings/servicenow-credentials", json=creds).json()["ok"]
    body = c.get("/settings").json()
    assert body["servicenow_configured"] is True
    assert body["servicenow_instance_url"] == "https://dev123.service-now.com"
    assert body["servicenow_username"] == "relay_api"
    assert body["servicenow_password_masked"] == "…7890"
    assert "s3cretpw" not in body["servicenow_password_masked"]


def test_set_servicenow_requires_instance_and_password(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_settings(monkeypatch)
    # username only, no password/instance → 400
    assert c.put("/settings/servicenow-credentials",
                 json={"username": "u"}).status_code == 400


def test_clear_servicenow_credentials(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    s = _FakeSettings()
    s.set("servicenow_instance_url", "https://dev123.service-now.com")
    s.set("servicenow_username", "relay_api")
    s.set("servicenow_password", "s3cretpw7890")
    c = _client_settings(monkeypatch, settings=s)
    assert c.put("/settings/servicenow-credentials",
                 json={"instance_url": "", "username": "", "password": ""}
                 ).json()["configured"] is False
    assert c.get("/settings").json()["servicenow_configured"] is False


def test_servicenow_test_404_when_unconfigured(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_settings(monkeypatch)
    assert c.post("/settings/servicenow-credentials/test").status_code == 404


# ---------------------------------------------------------------------------
# RELAY_INTEGRATIONS_LOCKED flag
# ---------------------------------------------------------------------------


def test_integrations_locked_gitlab_save_returns_403(monkeypatch):
    """PUT /settings/gitlab-token with a non-empty token is blocked when locked."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_INTEGRATIONS_LOCKED", "true")
    c = _client_settings(monkeypatch)
    r = c.put("/settings/gitlab-token", json={"token": "glpat-secrettoken1234"})
    assert r.status_code == 403
    assert "locked" in r.json()["detail"].lower()


def test_integrations_locked_servicenow_save_returns_403(monkeypatch):
    """PUT /settings/servicenow-credentials with non-empty creds is blocked when locked."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_INTEGRATIONS_LOCKED", "true")
    c = _client_settings(monkeypatch)
    creds = {
        "instance_url": "https://dev123.service-now.com",
        "username": "relay_api",
        "password": "s3cretpw7890",
    }
    r = c.put("/settings/servicenow-credentials", json=creds)
    assert r.status_code == 403
    assert "locked" in r.json()["detail"].lower()


def test_integrations_locked_gitlab_clear_still_succeeds(monkeypatch):
    """PUT /settings/gitlab-token with empty token (clear) is allowed even when locked."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("RELAY_INTEGRATIONS_LOCKED", "true")
    s = _FakeSettings()
    s.set("gitlab_token", "glpat-secrettoken1234")
    c = _client_settings(monkeypatch, settings=s)
    r = c.put("/settings/gitlab-token", json={"token": ""})
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_integrations_locked_config_flag_reported_in_config(monkeypatch):
    """GET /config includes features.integrations_locked == true when the flag is set."""
    monkeypatch.setenv("RELAY_INTEGRATIONS_LOCKED", "true")
    c = _client_settings(monkeypatch)
    body = c.get("/config").json()
    assert body["features"]["integrations_locked"] is True


def test_integrations_locked_false_by_default(monkeypatch):
    """GET /config features.integrations_locked is false when the flag is unset."""
    monkeypatch.delenv("RELAY_INTEGRATIONS_LOCKED", raising=False)
    c = _client_settings(monkeypatch)
    body = c.get("/config").json()
    assert body["features"]["integrations_locked"] is False


def test_teams_webhook_notifier_builds_dual_payload():
    from relay.adapters.integrations.teams import TeamsWebhookNotifier
    from relay.core.model import IncidentState, Severity, SignalSource

    captured = {}

    def fake_post(url, body):
        import json as _j
        captured["card"] = _j.loads(body)
        return 200

    inc = _incident()
    inc.severity = Severity.SEV1
    inc.signal_source = SignalSource.CLOUDWATCH_ALARM
    inc.state = IncidentState.TRIGGERED
    n = TeamsWebhookNotifier("https://x.webhook.office.com/y", http_post=fake_post)
    assert n.notify_incident(inc, {"Open": "http://hub/"}) is True
    # dual payload: top-level text (Workflows) + MessageCard (classic connector)
    assert "text" in captured["card"]
    assert captured["card"]["@type"] == "MessageCard"


def test_teams_webhook_notifier_non_2xx_returns_false():
    from relay.adapters.integrations.teams import TeamsWebhookNotifier
    n = TeamsWebhookNotifier("https://x", http_post=lambda u, b: 500)
    assert n.notify_incident(_incident()) is False


# ---------------------------------------------------------------------------
# Scheduling endpoints
# ---------------------------------------------------------------------------


class _FakeScheduleStore:
    def __init__(self):
        self.avail: dict[str, dict] = {}
        self.sched: dict[str, dict] = {}

    def list_availability(self):
        return list(self.avail.values())

    def get_availability(self, cid):
        return self.avail.get(cid)

    def put_availability(self, cid, data):
        d = dict(data)
        d["contact_id"] = cid
        self.avail[cid] = d

    def get_schedule(self, ws):
        return self.sched.get(ws)

    def put_schedule(self, ws, data):
        self.sched[ws] = data


def _client_sched(monkeypatch, store=None):
    app_obj = HubApp.__new__(HubApp)
    app_obj._schedule_store = store if store is not None else _FakeScheduleStore()
    app_obj._settings_store = None
    app_obj._incident_store = None
    app_obj._contact_store = None
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
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


_ALL = {d: ["night", "day", "evening"]
        for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}


def test_put_availability_requires_auth(monkeypatch):
    c = _client_sched(monkeypatch)
    assert c.put("/availability/cnt-a",
                 json={"available": True, "slots": _ALL, "ooo": None}).status_code == 403


def test_availability_roundtrip_and_auto_schedule(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    for cid in ("cnt-a", "cnt-b"):
        assert c.put(f"/availability/{cid}",
                     json={"available": True, "slots": _ALL, "ooo": None}).status_code == 200
    assert len(c.get("/availability").json()) == 2
    r = c.post("/schedule/auto?week=2026-06-22")
    assert r.status_code == 200
    body = r.json()
    # 21 (day,shift) x 3 roles = 63 role-slots; two people eligible for
    # primary+secondary (default), nobody for manager => all manager slots gap.
    assert body["coverage"] == [42, 63]
    assert body["gaps"] == 21
    assert body["coverage_by_role"]["primary"] == [21, 21]
    assert body["coverage_by_role"]["secondary"] == [21, 21]
    assert body["coverage_by_role"]["manager"] == [0, 21]
    # primary+secondary balanced across the two people: 42 / 2 = 21 each
    assert sorted(body["counts"].values()) == [21, 21]
    # stored + retrievable
    g = c.get("/schedule?week=2026-06-22").json()
    assert len(g["slots"]) == 63


def test_put_availability_explicit_empty_roles_stays_empty(monkeypatch):
    """An explicit empty roles list means 'eligible for no roles' and must be
    honored (a contact can be created with none) — not defaulted."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-none",
              json={"available": False, "slots": {}, "ooo": None, "roles": []})
    assert r.status_code == 200
    assert store.avail["cnt-none"]["roles"] == []


def test_put_availability_omitted_roles_defaults(monkeypatch):
    """A MISSING roles key falls back to the primary+secondary default."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-def",
              json={"available": True, "slots": _ALL, "ooo": None})
    assert r.status_code == 200
    assert store.avail["cnt-def"]["roles"] == ["primary", "secondary"]


def test_put_availability_explicit_roles_filtered_to_valid(monkeypatch):
    """An explicit list keeps only valid roles (invalid values dropped)."""
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    store = _FakeScheduleStore()
    c = _client_sched(monkeypatch, store=store)
    r = c.put("/availability/cnt-mgr",
              json={"available": True, "slots": _ALL, "ooo": None,
                    "roles": ["manager", "bogus"]})
    assert r.status_code == 200
    assert store.avail["cnt-mgr"]["roles"] == ["manager"]


def test_auto_schedule_requires_auth():
    c = _client_sched(None)
    assert c.post("/schedule/auto?week=2026-06-22").status_code == 403


def test_auto_schedule_with_no_availability_is_all_gaps(monkeypatch):
    monkeypatch.setenv("RELAY_AUTH_MODE", "dev")
    c = _client_sched(monkeypatch)
    body = c.post("/schedule/auto?week=2026-06-22").json()
    assert body["coverage"] == [0, 63]
    assert body["gaps"] == 63


def test_get_schedule_read_open():
    c = _client_sched(None)
    assert c.get("/schedule?week=2026-06-22").status_code == 200


# ---------------------------------------------------------------------------
# Tile-detail endpoint — GET /fleet/{account}/{app}
#
# One data-driven payload for both topologies: a federated Hub serves the
# pushed on_call snapshot; a team Hub (schedule store present) overwrites it
# with a live resolution. Same shape either way.
# ---------------------------------------------------------------------------

from relay.hub.health import FleetTile, Liveness  # noqa: E402


def _tile(**over):
    base = dict(
        account_id="123456789012",
        app_name="checkout-api",
        status="green",
        liveness=Liveness.LIVE,
        open_incidents=0,
        worst_severity=None,
        last_heartbeat_at=datetime.now(UTC),
        registered_at=datetime.now(UTC),
        environment="prod",
        deployment_id="dep-1",
        service_path=["Payments", "Checkout", "checkout-api"],
        org_path=[{"id": "dep-1", "name": "checkout-api", "level": "deployment", "parent": None}],
        metadata={"owner": "team-pay", "aws_tags": {"env": "prod"}},
        on_call={"source": "team_snapshot", "shift": "day",
                 "roles": {"primary": {"contact_id": "cnt-x", "name": "Carol"}}},
    )
    base.update(over)
    return FleetTile(**base)


def _client_tile(monkeypatch, tile, *, schedule_store=None, contacts=None):
    app_obj = HubApp.__new__(HubApp)
    app_obj._schedule_store = schedule_store
    app_obj._contact_store = _FakeContactStore(contacts or []) if contacts is not None else None
    app_obj._settings_store = None
    app_obj._incident_store = _FakeIncidentStore([])
    app_obj._notifier = None
    app_obj._paging_topic_arn = None
    app_obj._config = None
    hs = HubState.__new__(HubState)
    hs._tiles = {tile.key: tile} if tile else {}
    hs.lock = threading.Lock()
    hs._store = None
    hs._cadence = 60
    hs._clock = lambda: datetime.now(UTC)
    app_obj._hub_state = hs
    app_obj._sse_publisher = SSEPublisher()
    return TestClient(app_obj.build_fastapi_app())


def test_tile_detail_serves_snapshot_when_no_schedule_store(monkeypatch):
    # Federated Hub: no local schedule → the pushed snapshot is returned as-is.
    c = _client_tile(monkeypatch, _tile(), schedule_store=None)
    r = c.get("/fleet/123456789012/checkout-api")
    assert r.status_code == 200
    body = r.json()
    assert body["metadata"]["owner"] == "team-pay"
    assert body["metadata"]["aws_tags"]["env"] == "prod"
    assert body["org_path"][0]["level"] == "deployment"
    assert body["on_call"]["source"] == "team_snapshot"
    assert body["on_call"]["roles"]["primary"]["name"] == "Carol"


def test_tile_detail_fills_oncall_live_on_team_hub(monkeypatch):
    # Team Hub: a real schedule covering "now" overrides the snapshot with a
    # live resolution. Build a schedule for this week so a slot exists now.
    from relay.core.scheduling import Availability, auto_schedule, monday_of

    now = datetime.now(UTC)
    ws = monday_of(now.date())
    everyone_all = Availability(
        contact_id="cnt-live",
        available=True,
        slots={d: ["night", "day", "evening"] for d in
               ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        ooo=None,
    )
    sched = auto_schedule(ws, [everyone_all])
    store = _FakeScheduleStore()
    store.put_schedule(ws.isoformat(), _schedule_to_stored(sched))

    c = _client_tile(
        monkeypatch, _tile(), schedule_store=store,
        contacts=[Contact(contact_id="cnt-live", name="Live Person", email="live@x.com")],
    )
    r = c.get("/fleet/123456789012/checkout-api")
    assert r.status_code == 200
    oc = r.json()["on_call"]
    # Live resolution wins over the pushed snapshot.
    assert oc["source"] == "schedule"
    assert oc["roles"]["primary"]["name"] == "Live Person"


def _schedule_to_stored(sched):
    """Serialise a Schedule into the DynamoScheduleStore dict form."""
    return {
        "week_start": sched.week_start.isoformat(),
        "slots": [
            {"date": s.date.isoformat(), "shift": s.shift.value,
             "role": s.role.value, "contact_id": s.contact_id}
            for s in sched.slots
        ],
        "roles": [r.value for r in sched.roles],
    }


def test_tile_detail_404_for_unknown(monkeypatch):
    c = _client_tile(monkeypatch, _tile(), schedule_store=None)
    assert c.get("/fleet/000/ghost").status_code == 404
