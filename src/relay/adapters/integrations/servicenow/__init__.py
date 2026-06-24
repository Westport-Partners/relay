"""ServiceNow incident adapter (Table API)."""

from __future__ import annotations

from relay.adapters.integrations.servicenow.adapter import MANIFEST, build
from relay.adapters.integrations.servicenow.listener import ServiceNowListener
from relay.adapters.integrations.servicenow.sink import ServiceNowConfig, ServiceNowSink

__all__ = [
    "MANIFEST",
    "ServiceNowConfig",
    "ServiceNowSink",
    "ServiceNowListener",
    "build",
]
