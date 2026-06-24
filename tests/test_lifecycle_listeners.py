"""Tests for the incident lifecycle seam (core/lifecycle) and adapter listeners."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from relay.adapters._support import AIBriefListener
from relay.adapters.integrations.gitlab.listener import GitLabListener
from relay.adapters.integrations.servicenow.listener import ServiceNowListener
from relay.adapters.integrations.teams.listener import TeamsListener
from relay.core.lifecycle import IncidentLifecycleEvent, dispatch
from relay.core.model import Incident, Severity, SignalSource


@pytest.fixture
def incident() -> Incident:
    now = datetime.now(UTC)
    return Incident(
        correlation_id="inc-001",
        account_id="123456789012",
        region="us-east-1",
        app_name="myapp",
        severity=Severity.SEV2,
        signal_source=SignalSource.CLOUDWATCH_ALARM,
        alarm_name="myapp-errors",
        deployment_id="dep-auth-api-prod",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# dispatch() — fan-out + failure isolation
# ---------------------------------------------------------------------------


def test_dispatch_calls_every_listener(incident: Incident) -> None:
    a, b = MagicMock(), MagicMock()
    dispatch([a, b], event=IncidentLifecycleEvent.TRIGGERED, incident=incident)
    a.on_event.assert_called_once_with(
        event=IncidentLifecycleEvent.TRIGGERED, incident=incident
    )
    b.on_event.assert_called_once()


def test_dispatch_isolates_listener_failure(incident: Incident) -> None:
    """A listener that raises does not stop the others."""
    boom = MagicMock()
    boom.on_event.side_effect = RuntimeError("kaboom")
    ok = MagicMock()
    # Order matters: failing listener first.
    dispatch([boom, ok], event=IncidentLifecycleEvent.RESOLVED, incident=incident)
    ok.on_event.assert_called_once()


# ---------------------------------------------------------------------------
# GitLabListener
# ---------------------------------------------------------------------------


def test_gitlab_listener_opens_issue_on_triggered(incident: Incident) -> None:
    sink = MagicMock()
    sink.create_incident.return_value = "42"
    store = MagicMock()
    listener = GitLabListener(sink, store, project_resolver=lambda dep: "team/proj")

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    # Project resolved onto the incident; issue id stamped back.
    assert incident.get_ticket("gitlab_project") == "team/proj"
    assert incident.get_ticket("gitlab_iid") == "42"
    sink.create_incident.assert_called_once_with(incident)
    # A gitlab.ticket_created timeline event was recorded + persisted.
    assert any(e.event_type == "gitlab.ticket_created" for e in incident.timeline)
    store.put_incident.assert_called()


def test_gitlab_listener_respects_preset_project(incident: Incident) -> None:
    """If the incident already carries a project, the resolver is not used."""
    incident.set_ticket("gitlab_project", "preset/proj")
    sink = MagicMock()
    sink.create_incident.return_value = "7"
    resolver = MagicMock(return_value="other/proj")
    listener = GitLabListener(sink, MagicMock(), project_resolver=resolver)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    resolver.assert_not_called()
    assert incident.get_ticket("gitlab_project") == "preset/proj"


def test_gitlab_listener_closes_issue_on_resolved(incident: Incident) -> None:
    incident.set_ticket("gitlab_iid", "42")
    sink = MagicMock()
    listener = GitLabListener(sink, MagicMock())

    listener.on_event(event=IncidentLifecycleEvent.RESOLVED, incident=incident)

    sink.close_incident.assert_called_once_with("42", incident)


def test_gitlab_listener_resolve_noop_without_iid(incident: Incident) -> None:
    """No issue was opened → nothing to close."""
    sink = MagicMock()
    listener = GitLabListener(sink, MagicMock())
    listener.on_event(event=IncidentLifecycleEvent.RESOLVED, incident=incident)
    sink.close_incident.assert_not_called()


def test_gitlab_listener_no_iid_recorded_when_create_fails(incident: Incident) -> None:
    """create returning '' (skip/failure) leaves no iid and records no event."""
    sink = MagicMock()
    sink.create_incident.return_value = ""
    store = MagicMock()
    listener = GitLabListener(sink, store, project_resolver=lambda dep: None)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    assert incident.get_ticket("gitlab_iid") is None
    assert not any(e.event_type == "gitlab.ticket_created" for e in incident.timeline)


# ---------------------------------------------------------------------------
# ServiceNowListener
# ---------------------------------------------------------------------------


def test_servicenow_listener_create_then_close(incident: Incident) -> None:
    sink = MagicMock()
    sink.create_incident.return_value = "SYS123"
    store = MagicMock()
    listener = ServiceNowListener(sink, store)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)
    assert incident.get_ticket("servicenow_sys_id") == "SYS123"
    assert any(e.event_type == "servicenow.ticket_created" for e in incident.timeline)

    listener.on_event(event=IncidentLifecycleEvent.RESOLVED, incident=incident)
    sink.close_incident.assert_called_once_with("SYS123", incident)


# ---------------------------------------------------------------------------
# TeamsListener
# ---------------------------------------------------------------------------


def test_teams_listener_posts_on_triggered(incident: Incident, monkeypatch) -> None:
    settings = MagicMock()
    settings.get.return_value = "https://x.webhook.office.com/abc"
    sent = {}

    class FakeNotifier:
        def __init__(self, hook: str) -> None:
            sent["hook"] = hook

        def notify_incident(self, inc, links=None) -> bool:
            sent["incident"] = inc
            sent["links"] = links
            return True

    monkeypatch.setattr(
        "relay.adapters.integrations.teams.notifier.TeamsWebhookNotifier", FakeNotifier
    )
    listener = TeamsListener(settings, dashboard_url="https://relay.example.com")
    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    assert sent["hook"] == "https://x.webhook.office.com/abc"
    assert sent["incident"] is incident
    assert "Open in Relay" in sent["links"]


def test_teams_listener_noop_without_webhook(incident: Incident) -> None:
    settings = MagicMock()
    settings.get.return_value = None
    # Should not raise even though no notifier is constructed.
    TeamsListener(settings).on_event(
        event=IncidentLifecycleEvent.TRIGGERED, incident=incident
    )


def test_teams_listener_ignores_resolved(incident: Incident) -> None:
    settings = MagicMock()
    TeamsListener(settings).on_event(
        event=IncidentLifecycleEvent.RESOLVED, incident=incident
    )
    settings.get.assert_not_called()


# ---------------------------------------------------------------------------
# AIBriefListener
# ---------------------------------------------------------------------------


def test_ai_brief_listener_runs_on_triggered_only(incident: Incident) -> None:
    calls = []
    listener = AIBriefListener(lambda inc: calls.append(inc))
    listener.on_event(event=IncidentLifecycleEvent.RESOLVED, incident=incident)
    assert calls == []
    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)
    assert calls == [incident]


# ---------------------------------------------------------------------------
# TeamsListener — injected notifier factory (no module patching needed)
# ---------------------------------------------------------------------------


def test_teams_listener_uses_injected_notifier_factory(incident: Incident) -> None:
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.get.return_value = "https://x.webhook.office.com/abc"
    sent = {}

    class FakeNotifier:
        def __init__(self, hook):
            sent["hook"] = hook

        def notify_incident(self, inc, links=None):
            sent["links"] = links
            return True

    listener = TeamsListener(
        settings,
        dashboard_url="https://relay.example.com",
        notifier_factory=FakeNotifier,
    )
    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)
    assert sent["hook"] == "https://x.webhook.office.com/abc"
    assert "Open in Relay" in sent["links"]


def test_incident_dashboard_links_empty_without_url(incident: Incident) -> None:
    from relay.adapters._support import incident_dashboard_links

    assert incident_dashboard_links("", incident) == {}
    links = incident_dashboard_links("https://relay.example.com/", incident)
    assert links["Open in Relay"].endswith(f"/#/incident/{incident.correlation_id}")


# ---------------------------------------------------------------------------
# Registry — discovery + build
# ---------------------------------------------------------------------------


def test_registry_discovers_builtin_adapters() -> None:
    from relay.adapters.registry import discover_manifests

    names = {m.name for m in discover_manifests()}
    # The three shipped integration adapters are auto-discovered by folder.
    assert {"gitlab", "servicenow", "teams"} <= names


def test_build_listeners_only_wires_configured_adapters(monkeypatch) -> None:
    from relay.adapters.integrations.gitlab.listener import GitLabListener
    from relay.adapters.integrations.teams.listener import TeamsListener
    from relay.adapters.registry import AdapterContext, build_listeners

    # Nothing configured (no env, no settings store, no secret fetcher) → empty.
    monkeypatch.delenv("RELAY_GITLAB_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("RELAY_SERVICENOW_INSTANCE_URL", raising=False)
    assert build_listeners(AdapterContext()) == []

    # GitLab (token provider) + Teams (settings store) configured → those two.
    ctx = AdapterContext(
        incident_store=MagicMock(),
        settings_store=MagicMock(),
    )
    # Make the GitLab token provider return a token by stubbing the settings get.
    ctx.settings_store.get.return_value = "glpat-x"
    listeners = build_listeners(ctx)
    types = {type(x) for x in listeners}
    assert GitLabListener in types
    assert TeamsListener in types


def test_build_listeners_includes_builtins() -> None:
    from relay.adapters._support import AIBriefListener
    from relay.adapters.registry import (
        AdapterContext,
        AdapterManifest,
        build_listeners,
    )

    ctx = AdapterContext(attach_ai_brief=lambda inc: None)
    ai = AdapterManifest(
        name="ai_brief",
        build=lambda c: AIBriefListener(c.attach_ai_brief),
        builtin=True,
    )
    # Pass an explicit empty manifest list so only the builtin is built.
    listeners = build_listeners(ctx, manifests=[], builtins=[ai])
    assert [type(x) for x in listeners] == [AIBriefListener]


# ---------------------------------------------------------------------------
# Registry discovery hygiene + the _template skeleton
# ---------------------------------------------------------------------------


def test_discovery_excludes_template_and_substrate() -> None:
    from relay.adapters.registry import discover_manifests

    names = {m.name for m in discover_manifests()}
    # The _template skeleton and the aws/ai packages are not lifecycle adapters.
    assert "template" not in names
    assert "aws" not in names
    assert "ai" not in names


def test_template_adapter_builds_and_handles_events(incident: Incident) -> None:
    """The skeleton is wired correctly: configured → builds → create/close."""
    from relay.adapters.integrations._template.adapter import build
    from relay.adapters.integrations._template.listener import TemplateListener
    from relay.adapters.registry import AdapterContext

    # Not configured → None (no secret fetcher).
    assert build(AdapterContext()) is None

    # Drive the listener directly with a mock sink.
    sink = MagicMock()
    sink.create_record.return_value = "EXT-1"
    store = MagicMock()
    listener = TemplateListener(sink, store)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)
    assert any(e.event_type == "template.ticket_created" for e in incident.timeline)

    listener.on_event(event=IncidentLifecycleEvent.RESOLVED, incident=incident)
    sink.close_record.assert_called_once()
    assert sink.close_record.call_args.args[0] == "EXT-1"


# ---------------------------------------------------------------------------
# GitLabListener — incident-first deployment_metadata resolution
# ---------------------------------------------------------------------------


def test_gitlab_listener_uses_deployment_metadata_project(incident: Incident) -> None:
    """deployment_metadata["gitlab_project"] is used without calling the resolver."""
    incident.deployment_metadata["gitlab_project"] = "meta/proj"
    sink = MagicMock()
    sink.create_incident.return_value = "99"
    resolver = MagicMock(return_value="other/proj")
    listener = GitLabListener(sink, MagicMock(), project_resolver=resolver)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    # Metadata value was promoted to external_tickets
    assert incident.get_ticket("gitlab_project") == "meta/proj"
    # Resolver was NOT called (incident-first short-circuit)
    resolver.assert_not_called()
    assert incident.get_ticket("gitlab_iid") == "99"


def test_gitlab_listener_falls_back_to_resolver_when_no_metadata(incident: Incident) -> None:
    """When deployment_metadata lacks gitlab_project, the org-tree resolver is used."""
    # deployment_metadata is empty by default on a fresh incident
    assert incident.deployment_metadata == {}

    sink = MagicMock()
    sink.create_incident.return_value = "55"
    resolver = MagicMock(return_value="fallback/proj")
    listener = GitLabListener(sink, MagicMock(), project_resolver=resolver)

    listener.on_event(event=IncidentLifecycleEvent.TRIGGERED, incident=incident)

    resolver.assert_called_once_with(incident.deployment_id)
    assert incident.get_ticket("gitlab_project") == "fallback/proj"
    assert incident.get_ticket("gitlab_iid") == "55"
