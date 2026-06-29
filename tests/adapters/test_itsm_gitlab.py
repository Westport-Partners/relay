"""Tests for GitLabSink — no network calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from relay.adapters.integrations.gitlab import GitLabConfig, GitLabSink
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


def _gitlab_config(**kwargs) -> GitLabConfig:
    defaults = {
        "project_id": "99",
        "token": "glpat-test-token",
    }
    defaults.update(kwargs)
    return GitLabConfig(**defaults)


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
