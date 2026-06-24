"""Transport implementation using Amazon EventBridge PutEvents for cross-account incident routing to the Relay Hub."""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from relay.core.model import Incident, Stream

logger = logging.getLogger(__name__)


class EventBridgeTransport:
    """Transport that routes incident events to the central Relay Hub via EventBridge PutEvents.

    The Hub lives in a dedicated monitoring account.  Its event bus policy
    allows PutEvents org-wide via ``aws:PrincipalOrgID`` so no cross-account
    role assumption is required from team nodes.

    Implements the Transport protocol from relay.adapters.base.
    """

    def __init__(
        self,
        hub_event_bus_arn: str,
        source_name: str = "relay.node",
        boto3_session: Any | None = None,
    ) -> None:
        """Initialise the transport.

        Args:
            hub_event_bus_arn: ARN of the central Hub's EventBridge bus in the
                               monitoring account.  The Hub bus policy allows
                               PutEvents org-wide via aws:PrincipalOrgID so no
                               per-account credentials are needed.
            source_name:       EventBridge ``Source`` field value.  Defaults to
                               ``"relay.node"``; override for testing.
            boto3_session:     Optional custom session for cross-account roles or
                               unit tests with moto.
        """
        self.hub_event_bus_arn = hub_event_bus_arn
        self.source_name = source_name
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._eb = session.client("events")

    def emit(self, *, incident: Incident, stream: Stream) -> None:
        """PutEvents to the Hub bus.

        Serializes the Incident as JSON in the detail field.  Raises on partial
        failure (FailedEntryCount > 0) or boto3 error.

        Args:
            incident: The incident to publish.
            stream:   The routing stream attached to this event.

        Raises:
            RuntimeError: If EventBridge reports a failed entry.
            ClientError:  On unrecoverable boto3 API error.
        """
        entry: dict[str, Any] = {
            "Source": self.source_name,
            "DetailType": "relay.IncidentTriggered",
            "Detail": self._serialize_incident(incident),
            "EventBusName": self.hub_event_bus_arn,
        }

        try:
            response = self._eb.put_events(Entries=[entry])
        except ClientError:
            logger.exception(
                "EventBridge PutEvents API error for incident %s",
                incident.correlation_id,
            )
            raise

        failed = response.get("FailedEntryCount", 0)
        if failed > 0:
            failed_entries = response.get("Entries", [])
            failure_detail = [
                e for e in failed_entries if e.get("ErrorCode")
            ]
            raise RuntimeError(
                f"EventBridge PutEvents failed for incident {incident.correlation_id!r}: "
                f"{failure_detail}"
            )

        logger.info(
            "Emitted incident to Hub bus",
            extra={
                "correlation_id": incident.correlation_id,
                "hub_event_bus_arn": self.hub_event_bus_arn,
                "stream": stream,
            },
        )

    def emit_heartbeat(
        self,
        *,
        account_id: str,
        app_name: str,
        timestamp: str,
        environment: str = "unrouted",
        deployment_id: str | None = None,
        service_path: list[str] | None = None,
        org_path: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        on_call: dict[str, Any] | None = None,
    ) -> None:
        """PutEvents a relay.heartbeat liveness event to the Hub bus.

        Carries the node's own identity so the Hub fleet store registers the
        app on deploy (not just on first incident) and keeps its tile LIVE
        between incidents. Best-effort: unlike emit() for incidents, a
        heartbeat failure is logged and swallowed — it must never disrupt the
        node's hot path.

        ``org_path`` is the node's org ancestry, ordered root→leaf, each entry a
        dict with at least ``id``/``name``/``level`` (plus optional ``parent``,
        ``gitlab_project``, ``owner_ref``). The federated Hub rebuilds the whole
        catalog/hierarchy from these registrations, so it never has to store a
        static catalog of its own — org information always originates team-side.
        """
        detail: dict[str, Any] = {
            "relay_event": "heartbeat",
            "account_id": account_id,
            "app_name": app_name,
            "timestamp": timestamp,
            "environment": environment,
        }
        if deployment_id is not None:
            detail["deployment_id"] = deployment_id
        if service_path is not None:
            detail["service_path"] = service_path
        if org_path:
            detail["org_path"] = org_path
        # Optional enrichment: free-form deployment metadata (owner, gitlab,
        # aws_tags) and the team's on-call snapshot. Only emitted when present so
        # an un-enriched Node stays byte-for-byte compatible with older Hubs.
        if metadata:
            detail["metadata"] = metadata
        if on_call:
            detail["on_call"] = on_call

        entry: dict[str, Any] = {
            "Source": self.source_name,
            "DetailType": "relay.heartbeat",
            "Detail": json.dumps(detail),
            "EventBusName": self.hub_event_bus_arn,
        }

        try:
            response = self._eb.put_events(Entries=[entry])
        except ClientError:
            logger.warning(
                "EventBridge PutEvents failed for heartbeat %s/%s; skipping",
                account_id,
                app_name,
                exc_info=True,
            )
            return

        if response.get("FailedEntryCount", 0) > 0:
            logger.warning(
                "EventBridge heartbeat entry failed for %s/%s: %s",
                account_id,
                app_name,
                response.get("Entries"),
            )
            return

        logger.debug("Emitted heartbeat to Hub bus for %s/%s", account_id, app_name)

    def _serialize_incident(self, incident: Incident) -> str:
        """Serialize an Incident to a JSON string suitable for EventBridge Detail.

        Uses Pydantic's model_dump(mode="json") to ensure all types (datetime,
        enums, UUIDs) are JSON-serialisable.

        Args:
            incident: The incident to serialize.

        Returns:
            A JSON string.
        """
        return json.dumps(incident.model_dump(mode="json"))
