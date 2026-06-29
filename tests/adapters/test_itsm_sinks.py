"""Tests for ServiceNowSink and GitLabSink — no network calls."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from relay.adapters.integrations.gitlab import GitLabConfig, GitLabSink
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


def _gitlab_config(**kwargs) -> GitLabConfig:
    defaults = {
        "project_id": "99",
        "token": "glpat-test-token",
    }
    defaults.update(kwargs)
    return GitLabConfig(**defaults)


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

    def test_http_error_returns_empty_string(self, incident: Incident) -> None:
        """Non-2xx response causes create_incident to return empty string."""
        fake = FakeHttp(status=500, response={})
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == ""

    def test_exception_swallowed(self, incident: Incident) -> None:
        """RuntimeError from http_fn is swallowed; empty string is returned."""
        fake = FakeHttp(raise_exc=RuntimeError("boom"))
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

    def test_update_exception_swallowed(self, incident: Incident) -> None:
        """Exception from http_fn during update is swallowed."""
        fake = FakeHttp(raise_exc=RuntimeError("network error"))
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        # Should not raise.
        sink.update_incident("SYS-001", incident)


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

    def test_close_exception_swallowed(self, incident: Incident) -> None:
        """Exception during close is swallowed."""
        fake = FakeHttp(raise_exc=OSError("conn refused"))
        sink = ServiceNowSink(_snow_config(), http_fn=fake)

        sink.close_incident("SYS-001", incident)


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


# ---------------------------------------------------------------------------
# GitLab tests
# ---------------------------------------------------------------------------


class TestGitLabCreate:
    def test_create_success(self, incident: Incident) -> None:
        """Happy-path: 201 response with iid is returned as string."""
        fake = FakeHttp(
            status=201,
            response={"iid": 42, "web_url": "https://gitlab.com/proj/-/issues/42"},
        )
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == "42"
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert "/api/v4/projects/" in call["url"]
        assert "PRIVATE-TOKEN" in call["headers"]
        body = json.loads(call["body"])
        assert str(incident.severity) in body["title"]
        assert incident.app_name in body["title"]
        assert "incident" in body["labels"]
        assert "sev2" in body["labels"]

    @pytest.mark.parametrize(
        "severity",
        [Severity.SEV1, Severity.SEV2, Severity.SEV3, Severity.SEV4],
    )
    def test_severity_labels(self, incident: Incident, severity: Severity) -> None:
        """Labels contain the lowercase severity string for each severity."""
        incident = incident.model_copy(update={"severity": severity})
        fake = FakeHttp(
            status=201,
            response={"iid": 1, "web_url": "https://gitlab.com/proj/-/issues/1"},
        )
        sink = GitLabSink(_gitlab_config(), http_fn=fake)
        sink.create_incident(incident)

        body = json.loads(fake.calls[0]["body"])
        assert str(severity).lower() in body["labels"]

    def test_http_error_returns_empty_string(self, incident: Incident) -> None:
        """403 response causes create_incident to return empty string."""
        fake = FakeHttp(status=403, response={})
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == ""

    def test_exception_swallowed(self, incident: Incident) -> None:
        """Exception from http_fn is swallowed; empty string is returned."""
        fake = FakeHttp(raise_exc=ConnectionError("refused"))
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        result = sink.create_incident(incident)

        assert result == ""


class TestGitLabUpdate:
    def test_update_sends_put(self, incident: Incident) -> None:
        """update_incident sends a PUT to the correct URL."""
        fake = FakeHttp(status=200, response={"iid": 5})
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        sink.update_incident("5", incident)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "PUT"
        assert "/issues/5" in call["url"]

    def test_update_exception_swallowed(self, incident: Incident) -> None:
        """Exception during update is swallowed."""
        fake = FakeHttp(raise_exc=RuntimeError("timeout"))
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        sink.update_incident("5", incident)


class TestGitLabClose:
    def test_close_sends_state_event_close(self, incident: Incident) -> None:
        """close_incident sends a PUT containing state_event=close."""
        fake = FakeHttp(status=200, response={"iid": 5})
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        sink.close_incident("5", incident)

        put_calls = [c for c in fake.calls if c["method"] == "PUT"]
        assert put_calls, "Expected at least one PUT call"
        put_body = json.loads(put_calls[0]["body"])
        assert put_body.get("state_event") == "close"

    def test_close_exception_swallowed(self, incident: Incident) -> None:
        """Exception during close is swallowed."""
        fake = FakeHttp(raise_exc=OSError("conn refused"))
        sink = GitLabSink(_gitlab_config(), http_fn=fake)

        sink.close_incident("5", incident)


class TestGitLabConfig:
    def test_uses_custom_base_url(self, incident: Incident) -> None:
        """Configured base_url is used as the URL prefix for all requests."""
        config = _gitlab_config(base_url="https://mygitlab.example.com")
        fake = FakeHttp(
            status=201,
            response={"iid": 7, "web_url": "https://mygitlab.example.com/proj/-/issues/7"},
        )
        sink = GitLabSink(config, http_fn=fake)

        sink.create_incident(incident)

        assert fake.calls[0]["url"].startswith("https://mygitlab.example.com")


class TestGitLabDora:
    """DORA wiring: incident-type issues + per-incident project + env tier."""

    def test_create_uses_issue_type_incident(self, incident: Incident) -> None:
        """Issues are created as issue_type=incident so GitLab DORA picks them up."""
        fake = FakeHttp(status=201, response={"iid": 1})
        GitLabSink(_gitlab_config(), http_fn=fake).create_incident(incident)
        body = json.loads(fake.calls[0]["body"])
        assert body["issue_type"] == "incident"

    def test_per_incident_project_overrides_config(self, incident: Incident) -> None:
        """incident gitlab_project ticket wins over config.project_id and is URL-encoded."""
        incident = incident.model_copy(
            update={"external_tickets": {"gitlab_project": "identity/auth-api"}}
        )
        fake = FakeHttp(status=201, response={"iid": 1})
        GitLabSink(_gitlab_config(project_id="99"), http_fn=fake).create_incident(incident)
        # group/project path is percent-encoded into the :id path segment.
        assert "/projects/identity%2Fauth-api/issues" in fake.calls[0]["url"]

    def test_no_project_resolves_skips_cleanly(self, incident: Incident) -> None:
        """No incident project AND no config project → no HTTP call, empty id."""
        incident = incident.model_copy(update={"external_tickets": {}})
        fake = FakeHttp(status=201, response={"iid": 1})
        sink = GitLabSink(_gitlab_config(project_id=None), http_fn=fake)
        assert sink.create_incident(incident) == ""
        assert fake.calls == []

    def test_environment_tier_label_attached(self, incident: Incident) -> None:
        """environment_tier_map adds a scoped environment::<tier> label."""
        incident = incident.model_copy(update={"environment": "prod"})
        config = _gitlab_config(environment_tier_map={"prod": "production"})
        fake = FakeHttp(status=201, response={"iid": 1})
        GitLabSink(config, http_fn=fake).create_incident(incident)
        body = json.loads(fake.calls[0]["body"])
        assert "environment::production" in body["labels"]

    def test_no_tier_when_env_unmapped(self, incident: Incident) -> None:
        """An unmapped environment adds no environment:: label."""
        incident = incident.model_copy(update={"environment": "weird"})
        config = _gitlab_config(environment_tier_map={"prod": "production"})
        fake = FakeHttp(status=201, response={"iid": 1})
        GitLabSink(config, http_fn=fake).create_incident(incident)
        body = json.loads(fake.calls[0]["body"])
        assert "environment::" not in body["labels"]


class TestGitLabTokenProvider:
    """Token precedence: provider override beats config token, resolved live."""

    def test_provider_token_overrides_config(self, incident: Incident) -> None:
        fake = FakeHttp(status=201, response={"iid": 1})
        sink = GitLabSink(
            _gitlab_config(token="config-token"),
            http_fn=fake,
            token_provider=lambda: "ui-token",
        )
        sink.create_incident(incident)
        assert fake.calls[0]["headers"]["PRIVATE-TOKEN"] == "ui-token"

    def test_falls_back_to_config_when_provider_empty(self, incident: Incident) -> None:
        fake = FakeHttp(status=201, response={"iid": 1})
        sink = GitLabSink(
            _gitlab_config(token="config-token"),
            http_fn=fake,
            token_provider=lambda: None,
        )
        sink.create_incident(incident)
        assert fake.calls[0]["headers"]["PRIVATE-TOKEN"] == "config-token"

    def test_provider_exception_falls_back(self, incident: Incident) -> None:
        def boom() -> str:
            raise RuntimeError("settings store down")

        fake = FakeHttp(status=201, response={"iid": 1})
        sink = GitLabSink(
            _gitlab_config(token="config-token"), http_fn=fake, token_provider=boom
        )
        sink.create_incident(incident)
        assert fake.calls[0]["headers"]["PRIVATE-TOKEN"] == "config-token"


# ---------------------------------------------------------------------------
# from_env() factories + token test (adapters own their config loading)
# ---------------------------------------------------------------------------


class TestGitLabFromEnv:
    def test_disabled_when_no_token(self, monkeypatch) -> None:
        monkeypatch.delenv("RELAY_GITLAB_TOKEN_SECRET", raising=False)
        assert GitLabSink.from_env() is None

    def test_enabled_via_secret_fetcher(self, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_GITLAB_TOKEN_SECRET", "relay/gitlab-token")
        monkeypatch.setenv("RELAY_GITLAB_PROJECT_ID", "99")
        monkeypatch.setenv("RELAY_GITLAB_ENV_TIER_MAP", "prod:production")
        sink = GitLabSink.from_env(secret_fetcher=lambda name: "glpat-xyz")
        assert sink is not None
        assert sink._config.token == "glpat-xyz"
        assert sink._config.project_id == "99"
        assert sink._config.environment_tier_map == {"prod": "production"}

    def test_enabled_via_token_provider_only(self, monkeypatch) -> None:
        """No secret, but a UI token provider → enabled."""
        monkeypatch.delenv("RELAY_GITLAB_TOKEN_SECRET", raising=False)
        sink = GitLabSink.from_env(token_provider=lambda: "ui-token")
        assert sink is not None

    def test_secret_fetch_failure_without_provider_disables(self, monkeypatch) -> None:
        monkeypatch.setenv("RELAY_GITLAB_TOKEN_SECRET", "relay/gitlab-token")

        def boom(name):
            raise RuntimeError("secrets manager down")

        assert GitLabSink.from_env(secret_fetcher=boom) is None


class _SeqHttp:
    """Fake HTTP that returns a scripted (status, body) per call, keyed by URL.

    ``test_token`` makes up to three calls (``/user`` → ``/personal_access_tokens
    /self`` → ``/projects/:id``). This lets each leg be scripted independently and
    records the calls in order for assertions.
    """

    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]):
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers})
        for fragment, resp in self._routes.items():
            if fragment in url:
                return resp
        return 404, {}


_USER = "/api/v4/user"
_PAT = "/personal_access_tokens/self"
_PROJ = "/api/v4/projects/"


class TestGitLabTestToken:
    def test_ok_returns_username(self) -> None:
        # No PAT-scope endpoint and no project → auth-only happy path.
        fake = _SeqHttp({_USER: (200, {"username": "relay-bot"}), _PAT: (404, {})})
        result = GitLabSink.test_token("glpat-x", http_fn=fake)
        assert result["ok"] is True
        assert result["username"] == "relay-bot"
        assert result["error"] is None
        assert fake.calls[0]["url"].endswith("/api/v4/user")
        assert fake.calls[0]["headers"]["PRIVATE-TOKEN"] == "glpat-x"

    def test_non_2xx_reports_error(self) -> None:
        fake = _SeqHttp({_USER: (401, {})})
        result = GitLabSink.test_token("bad", http_fn=fake)
        assert result["ok"] is False
        assert "401" in result["error"]

    def test_exception_is_caught(self) -> None:
        fake = FakeHttp(raise_exc=ConnectionError("refused"))
        result = GitLabSink.test_token("x", http_fn=fake)
        assert result["ok"] is False
        assert result["error"]

    def test_read_only_scope_is_rejected(self) -> None:
        """A token that authenticates but lacks the 'api' scope must fail."""
        fake = _SeqHttp(
            {
                _USER: (200, {"username": "ro"}),
                _PAT: (200, {"scopes": ["read_api", "read_repository"]}),
            }
        )
        result = GitLabSink.test_token("glpat-ro", http_fn=fake)
        assert result["ok"] is False
        assert "api" in result["error"]
        assert result["scopes"] == ["read_api", "read_repository"]

    def test_api_scope_passes(self) -> None:
        fake = _SeqHttp(
            {_USER: (200, {"username": "rw"}), _PAT: (200, {"scopes": ["api"]})}
        )
        result = GitLabSink.test_token("glpat-rw", http_fn=fake)
        assert result["ok"] is True
        assert result["scopes"] == ["api"]

    def test_unknown_scope_endpoint_is_skipped(self) -> None:
        """Older/self-managed GitLab without the PAT endpoint → scope check skipped."""
        fake = _SeqHttp({_USER: (200, {"username": "x"}), _PAT: (404, {})})
        result = GitLabSink.test_token("glpat", http_fn=fake)
        assert result["ok"] is True
        assert result["scopes"] == []

    def test_project_reporter_access_passes(self) -> None:
        fake = _SeqHttp(
            {
                _USER: (200, {"username": "rw"}),
                _PAT: (200, {"scopes": ["api"]}),
                _PROJ: (200, {"permissions": {"project_access": {"access_level": 30}}}),
            }
        )
        result = GitLabSink.test_token("glpat", http_fn=fake, project="group/app")
        assert result["ok"] is True
        assert result["project"] == "group/app"
        assert result["access_level"] == 30
        # The project leg must URL-encode the path.
        assert any("group%2Fapp" in c["url"] for c in fake.calls)

    def test_project_guest_access_is_rejected(self) -> None:
        fake = _SeqHttp(
            {
                _USER: (200, {"username": "rw"}),
                _PAT: (200, {"scopes": ["api"]}),
                _PROJ: (200, {"permissions": {"project_access": {"access_level": 10}}}),
            }
        )
        result = GitLabSink.test_token("glpat", http_fn=fake, project="group/app")
        assert result["ok"] is False
        assert "reporter" in result["error"]
        assert result["access_level"] == 10

    def test_project_inherited_group_access_counts(self) -> None:
        """Reporter inherited from the parent group (not direct) is sufficient."""
        fake = _SeqHttp(
            {
                _USER: (200, {"username": "rw"}),
                _PAT: (200, {"scopes": ["api"]}),
                _PROJ: (
                    200,
                    {
                        "permissions": {
                            "project_access": None,
                            "group_access": {"access_level": 20},
                        }
                    },
                ),
            }
        )
        result = GitLabSink.test_token("glpat", http_fn=fake, project="g/app")
        assert result["ok"] is True
        assert result["access_level"] == 20

    def test_project_not_visible_is_rejected(self) -> None:
        fake = _SeqHttp(
            {
                _USER: (200, {"username": "rw"}),
                _PAT: (200, {"scopes": ["api"]}),
                _PROJ: (404, {}),
            }
        )
        result = GitLabSink.test_token("glpat", http_fn=fake, project="secret/app")
        assert result["ok"] is False
        assert "secret/app" in result["error"]


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
