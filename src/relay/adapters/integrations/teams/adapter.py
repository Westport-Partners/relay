"""Microsoft Teams adapter manifest — the plug point the registry discovers."""

from __future__ import annotations

from relay.adapters.integrations.teams.listener import TeamsListener
from relay.adapters.registry import AdapterContext, AdapterManifest
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.settings import SettingsKey


def build(ctx: AdapterContext) -> TeamsListener | None:
    """Build the Teams listener, or None when no settings store is available.

    The webhook URL itself is read live from the settings store per event (it is
    UI-editable), so the only build-time requirement is a settings store to read
    from. With none, the Hub runs without Teams.
    """
    if ctx.settings_store is None:
        return None
    return TeamsListener(ctx.settings_store, ctx.dashboard_url)


MANIFEST = AdapterManifest(
    name="teams",
    build=build,
    events=(IncidentLifecycleEvent.TRIGGERED,),
    settings_keys=(SettingsKey.TEAMS_WEBHOOK_URL,),
)
