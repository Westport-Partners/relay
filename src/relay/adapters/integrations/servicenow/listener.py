"""ServiceNowListener — maps incident lifecycle events to ServiceNow records."""

from __future__ import annotations

import logging
from typing import Any

from relay.adapters._support import record_sink_event
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident

logger = logging.getLogger(__name__)


class ServiceNowListener:
    """Creates a ServiceNow incident on TRIGGERED; closes it on RESOLVED."""

    def __init__(self, sink: Any, incident_store: Any) -> None:
        self._sink = sink
        self._incident_store = incident_store

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event == IncidentLifecycleEvent.TRIGGERED:
            sys_id = self._sink.create_incident(incident)
            if sys_id:
                incident.set_ticket("servicenow_sys_id", sys_id)
                logger.info(
                    "ServiceNow incident created: sys_id=%s correlation_id=%s",
                    sys_id,
                    incident.correlation_id,
                )
                record_sink_event(incident, self._incident_store, "servicenow", sys_id)
        elif event == IncidentLifecycleEvent.RESOLVED:
            sys_id = incident.get_ticket("servicenow_sys_id")
            if sys_id:
                self._sink.close_incident(sys_id, incident)
                logger.info(
                    "ServiceNow incident closed: sys_id=%s correlation_id=%s",
                    sys_id,
                    incident.correlation_id,
                )
