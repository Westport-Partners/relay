"""Template listener — maps lifecycle events to your sink's actions."""

from __future__ import annotations

import logging
from typing import Any

from relay.adapters._support import record_sink_event
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident

logger = logging.getLogger(__name__)

# Pick a stable system key for timeline events + any external-id bookkeeping.
_SYSTEM = "template"


class TemplateListener:
    """Creates a record on TRIGGERED; closes it on RESOLVED."""

    def __init__(self, sink: Any, incident_store: Any) -> None:
        self._sink = sink
        self._incident_store = incident_store

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event == IncidentLifecycleEvent.TRIGGERED:
            external_id = self._sink.create_record(incident)
            if external_id:
                # Stamp the external id on the incident's generic ticket map so
                # the close path can find it — no per-integration core field.
                incident.set_ticket(f"{_SYSTEM}_id", external_id)
                # record_sink_event appends a "<system>.ticket_created" timeline
                # event (the durable audit record of the link).
                record_sink_event(
                    incident, self._incident_store, _SYSTEM, external_id
                )
        elif event == IncidentLifecycleEvent.RESOLVED:
            external_id = incident.get_ticket(f"{_SYSTEM}_id")
            if external_id:
                self._sink.close_record(external_id, incident)
