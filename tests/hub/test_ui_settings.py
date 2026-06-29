"""Tests for the Hub UI settings endpoints (Teams/GitLab/ServiceNow + integrations-locked)."""

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


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("RELAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("RELAY_DEV_USER", raising=False)
    yield


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
