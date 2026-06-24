"""TeamsListener — posts an incident card to a Teams webhook on TRIGGERED."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from relay.adapters._support import incident_dashboard_links
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident
from relay.core.settings import SettingsKey


class TeamsListener:
    """Posts an incident card to a Teams webhook on TRIGGERED.

    The webhook URL is read fresh from the settings store on each event (it is
    UI-editable). ``notifier_factory`` builds a notifier from the webhook URL —
    injectable so tests don't have to patch the module, and so the concrete
    notifier dependency is explicit rather than imported inside ``on_event``.
    ``links_builder`` produces the deep-link map (UI concern, kept out of the
    listener body).
    """

    def __init__(
        self,
        settings_store: Any,
        dashboard_url: str = "",
        notifier_factory: Callable[[str], Any] | None = None,
        links_builder: Callable[[str, Incident], dict[str, str]] | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._dashboard_url = dashboard_url
        self._links_builder = links_builder or incident_dashboard_links
        if notifier_factory is not None:
            self._notifier_factory = notifier_factory
        else:
            from relay.adapters.integrations.teams.notifier import TeamsWebhookNotifier

            self._notifier_factory = TeamsWebhookNotifier

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event != IncidentLifecycleEvent.TRIGGERED or self._settings_store is None:
            return
        hook = self._settings_store.get(SettingsKey.TEAMS_WEBHOOK_URL)
        if not hook:
            return
        links = self._links_builder(self._dashboard_url, incident)
        self._notifier_factory(hook).notify_incident(incident, links)
