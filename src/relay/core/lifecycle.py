"""Incident lifecycle events + the listener seam that fans them out to adapters.

Historically the Hub called each external adapter (ServiceNow, GitLab, Teams,
AI brief) imperatively at hard-coded points in ``HubProcessor`` — and the
resolve path called none of them, so external tickets were created but never
closed. This module introduces a small in-process pub/sub seam so adapters
*subscribe* to standard lifecycle events instead:

    TRIGGERED / ACKNOWLEDGED / ESCALATED / RESOLVED

Each adapter is wrapped as an :class:`IncidentListener` and decides for itself
what each event means (GitLab opens an issue on TRIGGERED and closes it on
RESOLVED; Teams posts a card on TRIGGERED; etc.). The Hub just emits events and
fans out — it has no per-adapter knowledge.

This is deliberately an **in-process** seam, not a new message bus: cross-account
routing already happens over EventBridge (see ``adapters/aws``). This only
decouples the Hub's local dispatch from the concrete adapters.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol, runtime_checkable

from relay.core.model import Incident

logger = logging.getLogger(__name__)


class IncidentLifecycleEvent(StrEnum):
    """The standard incident lifecycle events adapters can react to.

    These mirror the meaningful transitions of :class:`~relay.core.model.IncidentState`
    but are framed as *events* (something happened) rather than states (current
    value), which is what listeners want to key off.
    """

    TRIGGERED = "incident.triggered"
    ACKNOWLEDGED = "incident.acknowledged"
    ESCALATED = "incident.escalated"
    RESOLVED = "incident.resolved"


@runtime_checkable
class IncidentListener(Protocol):
    """A subscriber to incident lifecycle events.

    Implementations are typically thin adapter wrappers (GitLab, ServiceNow,
    Teams). They MUST be failure-isolated in spirit, but the dispatcher
    (:func:`dispatch`) also guards every call so one misbehaving listener can
    never break incident processing or starve the others.
    """

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        """React to a lifecycle event for ``incident``.

        Listeners should no-op for events they don't care about. Any exception
        raised here is caught and logged by :func:`dispatch`.
        """
        ...


def dispatch(
    listeners: list[IncidentListener],
    *,
    event: IncidentLifecycleEvent,
    incident: Incident,
) -> None:
    """Fan ``event`` out to every listener with per-listener failure isolation.

    A listener that raises is logged and skipped; the remaining listeners still
    run. This preserves the prior behaviour where each adapter call sat in its
    own try/except, while removing the hard-coded per-adapter ``if`` blocks.
    """
    for listener in listeners:
        try:
            listener.on_event(event=event, incident=incident)
        except Exception:
            logger.warning(
                "Incident listener %s failed on %s for incident %s",
                type(listener).__name__,
                event,
                incident.correlation_id,
                exc_info=True,
            )
