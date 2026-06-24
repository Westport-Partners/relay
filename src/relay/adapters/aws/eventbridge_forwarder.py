"""EventBridge Forwarder — publishes selected incidents from a local-federated Hub to a central Hub bus."""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from relay.core.model import Incident

logger = logging.getLogger(__name__)

_SOURCE_NAME = "relay.hub"
_DETAIL_TYPE = "relay.IncidentForwarded"


class EventBridgeForwarder:
    """Forwards incidents to a central Hub via EventBridge PutEvents.

    Used when scope=local-federated.  Adds a ``relay_forwarded_from`` marker to
    the detail so the central Hub can distinguish forwarded events from direct ones.

    Implements the Forwarder protocol from relay.adapters.base.

    Args:
        central_bus_arn: ARN of the central Hub's EventBridge bus.
        source_account_id: The AWS account ID of this local Hub (embedded as
            ``relay_forwarded_from`` in the event detail for dedup/tracing).
        hub_scope: The scope string of this Hub (e.g. ``"local-federated"``), also
            embedded in the forwarded event for tracing.
        source_name: EventBridge ``Source`` field value.  Defaults to ``"relay.hub"``.
        boto3_session: Optional custom session for cross-account roles or unit tests.
    """

    def __init__(
        self,
        central_bus_arn: str,
        source_account_id: str = "",
        hub_scope: str = "local-federated",
        source_name: str = _SOURCE_NAME,
        boto3_session: Any | None = None,
    ) -> None:
        self.central_bus_arn = central_bus_arn
        self.source_account_id = source_account_id
        self.hub_scope = hub_scope
        self.source_name = source_name
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._eb = session.client("events")

    def forward(self, incident: Incident) -> bool:
        """Publish incident to the central bus with a relay_forwarded_from marker.

        Returns True on success.  Returns False on boto3 / EventBridge error so
        the caller can log and continue without raising.

        Args:
            incident: The incident to forward.

        Returns:
            True if the event was successfully published; False otherwise.
        """
        detail = self._build_detail(incident)
        entry: dict[str, Any] = {
            "Source": self.source_name,
            "DetailType": _DETAIL_TYPE,
            "Detail": json.dumps(detail),
            "EventBusName": self.central_bus_arn,
        }

        try:
            response = self._eb.put_events(Entries=[entry])
        except ClientError:
            logger.exception(
                "EventBridgeForwarder: PutEvents API error for incident %s",
                incident.correlation_id,
            )
            return False

        failed = response.get("FailedEntryCount", 0)
        if failed > 0:
            failed_entries = [e for e in response.get("Entries", []) if e.get("ErrorCode")]
            logger.error(
                "EventBridgeForwarder: PutEvents partial failure for incident %s: %r",
                incident.correlation_id,
                failed_entries,
            )
            return False

        logger.info(
            "EventBridgeForwarder: forwarded incident %s to central bus %s",
            incident.correlation_id,
            self.central_bus_arn,
        )
        return True

    def _build_detail(self, incident: Incident) -> dict[str, Any]:
        """Build the EventBridge detail dict with forwarding marker."""
        payload = incident.model_dump(mode="json")
        payload["relay_forwarded_from"] = self.source_account_id
        payload["relay_forwarded_hub_scope"] = self.hub_scope
        return payload


class NoOpForwarder:
    """No-op forwarder used when scope=local or scope=central.

    Always returns False (nothing forwarded).  Implements the Forwarder protocol.
    """

    def forward(self, incident: Incident) -> bool:
        """Do nothing; return False.

        Args:
            incident: Ignored.

        Returns:
            Always False.
        """
        logger.debug(
            "NoOpForwarder: skipping forward for incident %s (scope=local or central)",
            incident.correlation_id,
        )
        return False
