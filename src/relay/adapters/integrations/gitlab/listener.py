"""GitLabListener — maps incident lifecycle events to GitLab issue actions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from relay.adapters._support import record_sink_event
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.model import Incident

logger = logging.getLogger(__name__)


class GitLabListener:
    """Opens a GitLab incident issue on TRIGGERED; closes it on RESOLVED.

    Project resolution is incident-first: if the incident already carries a
    resolved ``deployment_metadata["gitlab_project"]`` (stamped Node-side from
    the catalog tag templates), that value is used without calling the org-tree
    resolver.  The ``project_resolver`` callback (given the incident's
    ``deployment_id``) serves as the org-tree fallback when the incident does
    not carry the key. The resolved value is stamped onto
    ``incident.external_tickets["gitlab_project"]`` so the sink and the close
    path agree, and the returned issue IID is stamped onto
    ``incident.external_tickets["gitlab_iid"]``.
    """

    def __init__(
        self,
        sink: Any,
        incident_store: Any,
        project_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._sink = sink
        self._incident_store = incident_store
        self._project_resolver = project_resolver

    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event == IncidentLifecycleEvent.TRIGGERED:
            self._on_triggered(incident)
        elif event == IncidentLifecycleEvent.RESOLVED:
            self._on_resolved(incident)

    def _on_triggered(self, incident: Incident) -> None:
        # Incident-first: prefer already-resolved deployment metadata over the
        # org-tree resolver (which requires a live catalog lookup).
        if not incident.get_ticket("gitlab_project"):
            pre = incident.deployment_metadata.get("gitlab_project")
            if pre:
                incident.set_ticket("gitlab_project", str(pre))

        # Org-tree fallback: resolve from catalog if not yet set.
        if (
            not incident.get_ticket("gitlab_project")
            and self._project_resolver is not None
        ):
            try:
                resolved = self._project_resolver(incident.deployment_id)
            except Exception:
                logger.warning(
                    "GitLab project resolution failed for %s",
                    incident.correlation_id,
                    exc_info=True,
                )
                resolved = None
            if resolved:
                incident.set_ticket("gitlab_project", resolved)

        issue_iid = self._sink.create_incident(incident)
        if issue_iid:
            incident.set_ticket("gitlab_iid", issue_iid)
            logger.info(
                "GitLab issue created: iid=%s correlation_id=%s project=%s",
                issue_iid,
                incident.correlation_id,
                incident.get_ticket("gitlab_project"),
            )
            record_sink_event(incident, self._incident_store, "gitlab", issue_iid)

    def _on_resolved(self, incident: Incident) -> None:
        issue_iid = incident.get_ticket("gitlab_iid")
        if not issue_iid:
            return  # No issue was opened for this incident; nothing to close.
        self._sink.close_incident(issue_iid, incident)
        logger.info(
            "GitLab issue closed: iid=%s correlation_id=%s",
            issue_iid,
            incident.correlation_id,
        )
