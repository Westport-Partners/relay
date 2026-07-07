"""Regression tests for Hub config-source resolution (first-deploy Issue 9).

A fresh BYOR deploy left ``RELAY_CONFIG_SOURCE`` unset, so ``_load_hub_config``
returned ``None`` and the routing/ignore seeds (incl. the ``TargetTracking-``
ignore rule) never reached DynamoDB. The stack now defaults the source to
``local``, and ``_load_hub_config`` also auto-detects the bundled ``/app/config``
as a defensive fallback — but only when no GitLab source is configured, so it
never shadows a GitLab-backed deployment.
"""

from __future__ import annotations

import pytest

from relay.hub import app as hub_app


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch):
    for k in (
        "RELAY_CONFIG_SOURCE",
        "RELAY_CONFIG_DIR",
        "RELAY_GITLAB_REPO",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_explicit_local_source_loads_from_config_dir(monkeypatch, tmp_path):
    """An explicit RELAY_CONFIG_SOURCE=local reads the given RELAY_CONFIG_DIR."""
    captured: dict[str, str] = {}

    class _FakeLoader:
        def __init__(self, d):
            captured["dir"] = str(d)

        def get(self):
            return "LOADED"

    monkeypatch.setattr(hub_app, "LocalConfigLoader", _FakeLoader, raising=False)
    # LocalConfigLoader is imported inside the function; patch at its source too.
    monkeypatch.setattr(
        "relay.config.local_loader.LocalConfigLoader", _FakeLoader, raising=False
    )
    monkeypatch.setenv("RELAY_CONFIG_SOURCE", "local")
    monkeypatch.setenv("RELAY_CONFIG_DIR", str(tmp_path))

    assert hub_app._load_hub_config() == "LOADED"
    assert captured["dir"] == str(tmp_path)


def test_unset_source_falls_back_to_bundled_app_config(monkeypatch):
    """With nothing configured, fall back to the image-bundled /app/config."""
    captured: dict[str, str] = {}

    class _FakeLoader:
        def __init__(self, d):
            captured["dir"] = str(d)

        def get(self):
            return "BUNDLED"

    monkeypatch.setattr(
        "relay.config.local_loader.LocalConfigLoader", _FakeLoader, raising=False
    )
    # Pretend the bundled config dir exists in the image.
    monkeypatch.setattr(hub_app.os.path, "isdir", lambda p: p == "/app/config")

    assert hub_app._load_hub_config() == "BUNDLED"
    assert captured["dir"] == "/app/config"


def test_gitlab_source_not_shadowed_by_bundled_fallback(monkeypatch):
    """A configured GitLab repo must win — the /app/config fallback must not fire."""
    used: dict[str, bool] = {"local": False, "gitlab": False}

    class _FakeLocal:
        def __init__(self, d):
            used["local"] = True

        def get(self):
            return "LOCAL"

    class _FakeGitLab:
        def __init__(self, repo, secrets_manager_secret_name=None):
            used["gitlab"] = True

        def get(self):
            return "GITLAB"

    monkeypatch.setattr(
        "relay.config.local_loader.LocalConfigLoader", _FakeLocal, raising=False
    )
    monkeypatch.setattr(
        "relay.config.loader.GitLabConfigLoader", _FakeGitLab, raising=False
    )
    # Bundled dir exists, but a GitLab repo is configured → GitLab must win.
    monkeypatch.setattr(hub_app.os.path, "isdir", lambda p: p == "/app/config")
    monkeypatch.setenv("RELAY_GITLAB_REPO", "group/relay-config")

    assert hub_app._load_hub_config() == "GITLAB"
    assert used["gitlab"] is True
    assert used["local"] is False


def test_no_source_no_bundled_dir_returns_none(monkeypatch):
    """Nothing configured and no bundled dir (e.g. non-container run) → None."""
    monkeypatch.setattr(hub_app.os.path, "isdir", lambda p: False)
    assert hub_app._load_hub_config() is None


# ---------------------------------------------------------------------------
# Issue 11 — the Hub must page through the TEAM topic, not the federation topic.
# ---------------------------------------------------------------------------

TEAM = "arn:aws:sns:us-east-1:111122223333:relay-gears-lab-paging"
CENTRAL = "arn:aws:sns:us-east-1:111122223333:relay-gears-lab-central-paging"


@pytest.fixture(autouse=True)
def _clear_topic_env(monkeypatch):
    for k in (
        "RELAY_SNS_TOPIC_ARN",
        "RELAY_PAGING_TOPIC_ARN",
        "RELAY_CENTRAL_PAGING_TOPIC_ARN",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_team_topic_preferred_over_central(monkeypatch):
    """With both set (the real task-def wiring), the team topic wins."""
    monkeypatch.setenv("RELAY_SNS_TOPIC_ARN", TEAM)
    monkeypatch.setenv("RELAY_CENTRAL_PAGING_TOPIC_ARN", CENTRAL)
    assert hub_app._resolve_paging_topic_arn() == TEAM


def test_paging_topic_arn_used_when_sns_unset(monkeypatch):
    monkeypatch.setenv("RELAY_PAGING_TOPIC_ARN", TEAM)
    monkeypatch.setenv("RELAY_CENTRAL_PAGING_TOPIC_ARN", CENTRAL)
    assert hub_app._resolve_paging_topic_arn() == TEAM


def test_central_topic_is_last_resort_fallback(monkeypatch):
    """Only when no team topic is wired does the central topic get used."""
    monkeypatch.setenv("RELAY_CENTRAL_PAGING_TOPIC_ARN", CENTRAL)
    assert hub_app._resolve_paging_topic_arn() == CENTRAL


def test_no_topics_resolves_empty(monkeypatch):
    assert hub_app._resolve_paging_topic_arn() == ""
