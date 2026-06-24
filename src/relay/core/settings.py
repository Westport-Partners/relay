"""Centralized keys for the runtime settings store (DynamoSettingsStore).

The Hub's settings store is a simple key/value table for UI-editable runtime
config (Teams webhook URL, GitLab token, …). Those keys were previously bare
string literals scattered across ``hub/app.py`` and the adapter packages —
a typo in any one place silently breaks the feature. This enum is the single
source of truth so reads, writes, and presence checks always agree.

``SettingsKey`` is a ``StrEnum``: a member *is* its string value, so it can be
passed straight to ``settings_store.get(...)`` / ``.set(...)`` and compared to
raw strings without ``.value``.
"""

from __future__ import annotations

from enum import StrEnum


class SettingsKey(StrEnum):
    """Keys stored in the runtime settings store."""

    TEAMS_WEBHOOK_URL = "teams_webhook_url"
    GITLAB_TOKEN = "gitlab_token"
    SERVICENOW_INSTANCE_URL = "servicenow_instance_url"
    SERVICENOW_USERNAME = "servicenow_username"
    SERVICENOW_PASSWORD = "servicenow_password"
