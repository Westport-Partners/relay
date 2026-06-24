"""Microsoft Teams incident adapter (Incoming Webhook)."""

from __future__ import annotations

from relay.adapters.integrations.teams.adapter import MANIFEST, build
from relay.adapters.integrations.teams.listener import TeamsListener
from relay.adapters.integrations.teams.notifier import NoOpTeamsNotifier, TeamsWebhookNotifier

__all__ = [
    "MANIFEST",
    "TeamsWebhookNotifier",
    "NoOpTeamsNotifier",
    "TeamsListener",
    "build",
]
