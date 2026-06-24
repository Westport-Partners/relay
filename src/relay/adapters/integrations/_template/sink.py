"""Template sink — the external client for your integration.

Replace the body with real calls to your service. Keep ``from_env`` as the one
place that reads ``RELAY_<NAME>_*`` env vars and resolves secrets, returning
None when the integration isn't configured.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from relay.core.model import Incident

logger = logging.getLogger(__name__)


@dataclass
class TemplateConfig:
    """Connection config for the template integration."""

    token: str
    base_url: str = "https://example.com"


class TemplateSink:
    """Client that creates/closes records in the external system."""

    def __init__(self, config: TemplateConfig, http_fn: Any | None = None) -> None:
        self._config = config
        self._http_fn = http_fn  # inject for tests

    @classmethod
    def from_env(cls, secret_fetcher: Any | None = None) -> TemplateSink | None:
        """Build from environment, or None when not configured.

        Read your own ``RELAY_TEMPLATE_*`` vars here. Use ``secret_fetcher`` for
        secrets so this module needs no cloud SDK. Return None to disable.
        """
        secret_name = os.environ.get("RELAY_TEMPLATE_TOKEN_SECRET", "").strip()
        if not secret_name or secret_fetcher is None:
            return None
        try:
            token = secret_fetcher(secret_name) or ""
        except Exception:
            logger.warning("template secret fetch failed; adapter disabled")
            return None
        if not token:
            return None
        base_url = os.environ.get("RELAY_TEMPLATE_BASE_URL", "https://example.com")
        return cls(TemplateConfig(token=token, base_url=base_url))

    def create_record(self, incident: Incident) -> str:
        """Create an external record; return its id (or "" on failure)."""
        # TODO: real API call. Keep failure-isolated (return "" on error).
        return ""

    def close_record(self, external_id: str, incident: Incident) -> None:
        """Close the external record for a resolved incident."""
        # TODO: real API call.
        return None
