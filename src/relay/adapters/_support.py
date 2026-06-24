"""Shared helpers for adapter listeners.

These are utilities every adapter package may reuse — kept here (not duplicated
per adapter) so the per-adapter packages stay focused on their own integration.
Donated adapters are free to import from this module.
"""

from __future__ import annotations

import logging
from typing import Any

from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident, external_ticket_event

logger = logging.getLogger(__name__)


def record_sink_event(
    incident: Incident,
    incident_store: Any,
    system: str,
    external_id: str,
) -> None:
    """Append a ``<system>.ticket_created`` timeline event and persist.

    The event shape lives in the core domain (``external_ticket_event``); this
    helper just appends + persists. Best-effort: a persistence failure must
    never break incident flow.
    """
    try:
        incident.timeline.append(external_ticket_event(incident, system, external_id))
        if incident_store is not None:
            incident_store.put_incident(incident)
    except Exception:
        logger.warning(
            "Failed to record %s ticket event for %s",
            system,
            incident.correlation_id,
            exc_info=True,
        )


def incident_dashboard_links(dashboard_url: str, incident: Incident) -> dict[str, str]:
    """Build the UI deep-link map for an incident notification.

    Deep-linking into the dashboard is an application/UI concern, not a per-
    adapter concern, so it lives here as a shared helper any notification-style
    adapter can reuse rather than re-deriving the route.
    """
    links: dict[str, str] = {}
    if dashboard_url:
        base = dashboard_url.rstrip("/")
        links["Open in Relay"] = f"{base}/#/incident/{incident.correlation_id}"
    return links


class AIBriefListener:
    """Builtin listener: drafts and attaches a t=0 AI briefing on TRIGGERED.

    Not a donatable integration — it delegates to the Hub's ``attach_ai_brief``
    callable (AI provider selection / fallback stays in one place). Kept here so
    every lifecycle listener is still assembled through the one registry path.
    """

    def __init__(self, attach_brief: Any) -> None:
        self._attach_brief = attach_brief

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event == IncidentLifecycleEvent.TRIGGERED:
            self._attach_brief(incident)
