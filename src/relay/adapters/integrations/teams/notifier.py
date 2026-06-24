"""Microsoft Teams webhook notifier.

Tier 1 of the Teams integration (see docs/TEAMS.md): post an incident
notification to a Teams **Incoming Webhook** URL. No app registration, no Graph,
no tokens — just an HTTP POST to a per-channel URL the team configures.

This does NOT create a per-incident group chat or add specific people (that is
the Graph-based phase 2, documented but not implemented). It posts a card to a
standing channel.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from relay.core.model import Incident

logger = logging.getLogger(__name__)


class TeamsWebhookNotifier:
    """Posts incident notifications to a Teams Incoming Webhook URL.

    Args:
        webhook_url: The Teams Incoming Webhook URL (configured per team).
        http_post:   Optional injectable ``(url, json_bytes) -> int`` for tests;
                     defaults to a urllib POST returning the HTTP status.
    """

    def __init__(self, webhook_url: str, http_post: Any | None = None) -> None:
        self._url = webhook_url
        self._post = http_post or self._urllib_post

    @staticmethod
    def _urllib_post(url: str, body: bytes) -> int:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            logger.warning("Teams webhook returned HTTP %s", exc.code)
            return exc.code

    def _build_card(self, incident: Incident, links: dict[str, str]) -> dict[str, Any]:
        """Build a webhook payload that works with BOTH classic Office connectors
        (which render the MessageCard) AND Power Automate "Workflows" webhooks
        (which typically render a top-level ``text``). We include both: a plain
        ``text`` summary plus the MessageCard structure, so the message shows up
        regardless of which webhook type the team configured.
        """
        sev = str(incident.severity)
        theme = "005b6d"  # brand teal
        svc = " › ".join(incident.service_path) if incident.service_path else ""
        # Plain-text summary (Markdown) — what Workflows/plain webhooks render.
        lines = [
            f"🚨 **Relay incident — {incident.app_name} ({sev})**",
            f"State: {incident.state} · Env: {incident.environment or '—'} · "
            f"Alarm: {incident.alarm_name or '—'}",
        ]
        if svc:
            lines.append(f"Service: {svc}")
        for label, url in links.items():
            if url:
                lines.append(f"[{label}]({url})")
        text = "  \n".join(lines)

        facts = [
            {"name": "State", "value": str(incident.state)},
            {"name": "Severity", "value": sev},
            {"name": "Environment", "value": incident.environment or "—"},
            {"name": "Alarm", "value": incident.alarm_name or "—"},
        ]
        if svc:
            facts.append({"name": "Service", "value": svc})
        actions = [
            {"@type": "OpenUri", "name": label, "targets": [{"os": "default", "uri": url}]}
            for label, url in links.items()
            if url
        ]
        return {
            "text": text,  # rendered by Workflows / plain webhooks
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": theme,
            "summary": f"Relay incident: {incident.app_name} ({sev})",
            "sections": [
                {
                    "activityTitle": f"🚨 Relay incident — {incident.app_name} ({sev})",
                    "activitySubtitle": f"Started {incident.created_at.isoformat()}",
                    "facts": facts,
                    "markdown": True,
                }
            ],
            "potentialAction": actions,
        }

    @staticmethod
    def build_test_card() -> dict[str, Any]:
        """Build the connectivity-test payload (Teams MessageCard shape).

        Kept here so the Teams card schema lives only in this adapter — callers
        (e.g. the Hub's settings test endpoint) post it without knowing the
        MessageCard format.
        """
        return {
            "text": "✅ Relay test message — your Teams webhook is connected.",
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "005b6d",
            "summary": "Relay test",
        }

    def send_test(self) -> bool:
        """Post a test card to the webhook. Returns True on 2xx, else False.

        Never raises — surfaces success/failure as a bool for the UI.
        """
        if not self._url:
            return False
        body = json.dumps(self.build_test_card()).encode("utf-8")
        try:
            status = self._post(self._url, body)
        except Exception:
            logger.warning("Teams webhook test post failed", exc_info=True)
            return False
        return 200 <= int(status) < 300

    def notify_incident(self, incident: Incident, links: dict[str, str] | None = None) -> bool:
        """Post an incident card to the webhook. Returns True on 2xx, else False.

        Never raises — a webhook failure must not break incident processing.
        """
        if not self._url:
            return False
        body = json.dumps(self._build_card(incident, links or {})).encode("utf-8")
        try:
            status = self._post(self._url, body)
        except Exception:
            logger.warning(
                "Teams webhook post failed for incident %s", incident.correlation_id,
                exc_info=True,
            )
            return False
        ok = 200 <= int(status) < 300
        if not ok:
            logger.warning("Teams webhook non-2xx (%s) for incident %s",
                           status, incident.correlation_id)
        return ok


class NoOpTeamsNotifier:
    """Used when no Teams webhook is configured."""

    def notify_incident(self, incident: Incident, links: dict[str, str] | None = None) -> bool:
        return False
