"""Tests for ServiceNowSink — no network calls."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from relay.adapters.integrations.servicenow import ServiceNowConfig, ServiceNowSink
from relay.core.model import Incident, Severity

# ---------------------------------------------------------------------------
# Fake HTTP helper
# ---------------------------------------------------------------------------


@dataclass
class FakeHttp:
    status: int = 201
    response: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_exc: Exception | None = None

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, Any]]:
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snow_config() -> ServiceNowConfig:
    return ServiceNowConfig(
        instance_url="https://example.service-now.com",
        username="username",
        password="password",
    )


# ---------------------------------------------------------------------------
# ServiceNow tests
# ---------------------------------------------------------------------------


class TestServiceNowCreate:
    def test_create_success(self, incident: Incident) -> None:
        """Happy-path: 201 response with sys_id is returned."""
        fake = FakeHttp(
            status=201,
            response={"result": {"sys_id": "SYS-001", "number": "INC001"}},
        )
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == "SYS-001"
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert "/api/now/table/incident" in call["url"]
        assert call["headers"]["Authorization"].startswith("Basic ")
        body = json.loads(call["body"])
        assert incident.app_name in body["short_description"]
        assert incident.alarm_name in body["short_description"]
        assert body["correlation_id"] == incident.correlation_id
        assert "urgency" in body
        assert "impact" in body

    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            (Severity.SEV1, "1"),
            (Severity.SEV2, "2"),
            (Severity.SEV3, "3"),
            (Severity.SEV4, "3"),
        ],
    )
    def test_severity_mapping(
        self, incident: Incident, severity: Severity, expected: str
    ) -> None:
        """Urgency and impact are mapped correctly for each severity."""
        incident = incident.model_copy(update={"severity": severity})
        fake = FakeHttp(
            status=201,
            response={"result": {"sys_id": "SYS-X", "number": "INCX"}},
        )
        sink = ServiceNowSink(_snow_config(), http_fn=fake)
        sink.create_incident(incident)

        body = json.loads(fake.calls[0]["body"])
        assert body["urgency"] == expected
        assert body["impact"] == expected

    @pytest.mark.parametrize(
        "fake",
        [
            FakeHttp(status=500, response={}),
            FakeHttp(raise_exc=RuntimeError("boom")),
        ],
        ids=["http_error_500", "exception_swallowed"],
    )
    def test_create_returns_empty_on_failure(
        self, incident: Incident, fake: FakeHttp
    ) -> None:
        """Non-2xx response or exception causes create_incident to return empty string."""
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == ""


class TestServiceNowDeploymentContext:
    """description includes deployment_metadata + resource tags when present."""

    def test_description_includes_deployment_metadata(self, incident: Incident) -> None:
        """deployment_metadata values appear in the description under Deployment context."""
        incident = incident.model_copy(
            update={"deployment_metadata": {"gitlab_project": "pay/api", "git_sha": "abc123"}}
        )
        fake = FakeHttp(status=201, response={"result": {"sys_id": "SYS-DM", "number": "INC-DM"}})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)
        sink.create_incident(incident)

        body = json.loads(fake.calls[0]["body"])
        desc = body["description"]
        assert "Deployment context:" in desc
        assert "gitlab_project" in desc
        assert "pay/api" in desc
        assert "git_sha" in desc
        assert "abc123" in desc
        # short_description must be unchanged.
        assert incident.alarm_name in body["short_description"]

    def test_description_includes_resource_tags(self, incident: Incident) -> None:
        """Resource tags appear in the description under Resource tags."""
        incident = incident.model_copy(update={"tags": {"env": "prod", "team": "payments"}})
        fake = FakeHttp(status=201, response={"result": {"sys_id": "SYS-TAGS", "number": "INC-TAGS"}})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)
        sink.create_incident(incident)

        body = json.loads(fake.calls[0]["body"])
        desc = body["description"]
        assert "Resource tags:" in desc
        assert "env" in desc
        assert "prod" in desc

    def test_description_unchanged_when_no_context(self, incident: Incident) -> None:
        """Empty deployment_metadata + tags produce no extra block in description."""
        # incident fixture has empty deployment_metadata and tags by default.
        fake = FakeHttp(status=201, response={"result": {"sys_id": "SYS-CLEAN", "number": "INC-CLEAN"}})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)
        sink.create_incident(incident)

        body = json.loads(fake.calls[0]["body"])
        desc = body["description"]
        assert "Deployment context:" not in desc
        assert "Resource tags:" not in desc
        # Core fields still present.
        assert incident.correlation_id in desc


class TestServiceNowUpdate:
    def test_update_posts_patch(self, incident: Incident) -> None:
        """update_incident sends a PATCH to the correct URL."""
        fake = FakeHttp(status=200, response={"result": {}})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        sink.update_incident("SYS-001", incident)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "PATCH"
        assert call["url"].endswith("/SYS-001")

    @pytest.mark.parametrize(
        ("exc", "operation"),
        [
            (RuntimeError("network error"), "update_incident"),
            (OSError("conn refused"), "close_incident"),
        ],
        ids=["update_exception_swallowed", "close_exception_swallowed"],
    )
    def test_mutating_exception_swallowed(
        self, incident: Incident, exc: Exception, operation: str
    ) -> None:
        """Exception from http_fn during update or close is swallowed without raising."""
        fake = FakeHttp(raise_exc=exc)
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        getattr(sink, operation)("SYS-001", incident)


class TestServiceNowClose:
    def test_close_sends_resolved_state(self, incident: Incident) -> None:
        """close_incident sends a PATCH with state == '6'."""
        fake = FakeHttp(status=200, response={"result": {}})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        sink.close_incident("SYS-001", incident)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "PATCH"
        body = json.loads(call["body"])
        assert body["state"] == "6"


class TestServiceNowAuth:
    def test_basic_auth_encoding(self, incident: Incident) -> None:
        """Authorization header is correct base64 encoding of username:password."""
        config = ServiceNowConfig(
            instance_url="https://example.service-now.com",
            username="myuser",
            password="s3cret",
        )
        fake = FakeHttp(
            status=201,
            response={"result": {"sys_id": "SYS-AUTH", "number": "INC999"}},
        )
        sink = ServiceNowSink(config, http_fn=fake)
        sink.create_incident(incident)

        auth_header = fake.calls[0]["headers"]["Authorization"]
        assert auth_header.startswith("Basic ")
        encoded_part = auth_header[len("Basic "):]
        decoded = base64.b64decode(encoded_part).decode()
        assert decoded == "myuser:s3cret"


class TestServiceNowCredentialProviders:
    """Settings-store providers override the env/config fallback, live per request."""

    def test_providers_override_config(self, incident: Incident) -> None:
        """A non-empty provider value wins over the config fallback in requests."""
        fake = FakeHttp(status=201, response={"result": {"sys_id": "SYS-P"}})
        sink = ServiceNowSink(
            _snow_config(),  # fallback creds
            http_fn=fake,
            instance_url_provider=lambda: "https://ui.service-now.com",
            username_provider=lambda: "ui_user",
            password_provider=lambda: "ui_pw",
        )
        sink.create_incident(incident)
        call = fake.calls[0]
        assert call["url"].startswith("https://ui.service-now.com/")
        decoded = base64.b64decode(call["headers"]["Authorization"][len("Basic "):]).decode()
        assert decoded == "ui_user:ui_pw"

    def test_empty_provider_falls_back_to_config(self, incident: Incident) -> None:
        """An empty/None provider value falls back to the config credential."""
        fake = FakeHttp(status=201, response={"result": {"sys_id": "SYS-F"}})
        sink = ServiceNowSink(
            _snow_config(),
            http_fn=fake,
            instance_url_provider=lambda: "",
            password_provider=lambda: None,
        )
        sink.create_incident(incident)
        call = fake.calls[0]
        assert call["url"].startswith("https://example.service-now.com/")
        decoded = base64.b64decode(call["headers"]["Authorization"][len("Basic "):]).decode()
        assert decoded == "username:password"

    def test_from_env_enabled_by_providers_without_env(self, monkeypatch) -> None:
        """No env vars set, but settings providers supply URL+password → sink built."""
        for var in (
            "RELAY_SERVICENOW_INSTANCE_URL",
            "RELAY_SERVICENOW_USERNAME",
            "RELAY_SERVICENOW_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        sink = ServiceNowSink.from_env(
            instance_url_provider=lambda: "https://ui.service-now.com",
            username_provider=lambda: "ui_user",
            password_provider=lambda: "ui_pw",
        )
        assert sink is not None

    def test_from_env_none_when_nothing_configured(self, monkeypatch) -> None:
        """No env vars and empty providers → sink disabled (None)."""
        for var in (
            "RELAY_SERVICENOW_INSTANCE_URL",
            "RELAY_SERVICENOW_USERNAME",
            "RELAY_SERVICENOW_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        sink = ServiceNowSink.from_env(
            instance_url_provider=lambda: "",
            password_provider=lambda: "",
        )
        assert sink is None


class TestServiceNowTestConnection:
    """ServiceNowSink.test_connection validates against the incident table."""

    def test_ok_on_2xx(self) -> None:
        fake = FakeHttp(status=200, response={"result": []})
        out = ServiceNowSink.test_connection(
            "https://x.service-now.com/", "u", "p", http_fn=fake
        )
        assert out["ok"] is True
        assert out["instance_url"] == "https://x.service-now.com"
        assert fake.calls[0]["method"] == "GET"
        assert "/api/now/table/incident" in fake.calls[0]["url"]

    def test_401_reports_auth_failure(self) -> None:
        fake = FakeHttp(status=401, response={})
        out = ServiceNowSink.test_connection("https://x.service-now.com", "u", "bad", http_fn=fake)
        assert out["ok"] is False
        assert "authentication failed" in out["error"]

    def test_missing_fields_no_call(self) -> None:
        fake = FakeHttp(status=200, response={})
        out = ServiceNowSink.test_connection("", "u", "", http_fn=fake)
        assert out["ok"] is False
        assert fake.calls == []


class TestServiceNowFromEnv:
    def test_disabled_when_no_instance(self, monkeypatch) -> None:
        monkeypatch.delenv("RELAY_SERVICENOW_INSTANCE_URL", raising=False)
        assert ServiceNowSink.from_env() is None

    def test_enabled_with_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_SERVICENOW_INSTANCE_URL", "https://x.service-now.com")
        monkeypatch.setenv("RELAY_SERVICENOW_USERNAME", "svc")
        monkeypatch.setenv("RELAY_SERVICENOW_SECRET", "relay/snow")
        sink = ServiceNowSink.from_env(secret_fetcher=lambda name: "pw")
        assert sink is not None
        assert sink._config.password == "pw"

    def test_disabled_when_no_password(self, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_SERVICENOW_INSTANCE_URL", "https://x.service-now.com")
        monkeypatch.delenv("RELAY_SERVICENOW_SECRET", raising=False)
        assert ServiceNowSink.from_env() is None
