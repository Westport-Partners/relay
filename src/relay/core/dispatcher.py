"""DualStreamDispatcher fans out an Incident to Team stream and Central stream
independently. One stream failing must not block the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from relay.adapters.base import Notifier, Transport
from relay.core.model import Incident, Stream

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    incident_id: str
    team_stream_ok: bool
    central_stream_ok: bool
    team_stream_error: str | None = None
    central_stream_error: str | None = None

    @property
    def fully_successful(self) -> bool:
        return self.team_stream_ok and self.central_stream_ok

    @property
    def any_successful(self) -> bool:
        return self.team_stream_ok or self.central_stream_ok


class DualStreamDispatcher:

    def __init__(
        self,
        notifier: Notifier,
        transport: Transport,
        contact_ids: list[str],
    ) -> None:
        self._notifier = notifier
        self._transport = transport
        self._contact_ids = contact_ids

    def dispatch(self, incident: Incident) -> DispatchResult:
        """Fan out to Team and Central streams sequentially with per-stream failure isolation.

        Catches all exceptions per-stream so one failure never blocks the other.
        Returns DispatchResult summarizing both outcomes.
        """
        team_ok = False
        team_error: str | None = None
        central_ok = False
        central_error: str | None = None

        try:
            self._dispatch_team(incident)
            team_ok = True
        except Exception as exc:
            team_error = str(exc)
            logger.error(
                "Team stream dispatch failed for incident %s: %s",
                incident.correlation_id,
                exc,
            )

        try:
            self._dispatch_central(incident)
            central_ok = True
        except Exception as exc:
            central_error = str(exc)
            logger.error(
                "Central stream dispatch failed for incident %s: %s",
                incident.correlation_id,
                exc,
            )

        return DispatchResult(
            incident_id=incident.correlation_id,
            team_stream_ok=team_ok,
            central_stream_ok=central_ok,
            team_stream_error=team_error,
            central_stream_error=central_error,
        )

    def _dispatch_team(self, incident: Incident) -> None:
        """Send SNS pages to on-call contacts. Raises on failure."""
        self._notifier.send(
            incident=incident,
            contact_ids=self._contact_ids,
            stream=Stream.TEAM,
        )
        logger.info(
            "Team stream dispatched for incident %s, contacts=%s",
            incident.correlation_id,
            self._contact_ids,
        )

    def _dispatch_central(self, incident: Incident) -> None:
        """Emit event to central Hub via cross-account EventBridge transport. Raises on failure."""
        self._transport.emit(incident=incident, stream=Stream.CENTRAL)
        logger.info(
            "Central stream dispatched for incident %s",
            incident.correlation_id,
        )
