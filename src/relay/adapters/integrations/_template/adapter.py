"""Template adapter manifest — the plug point the registry would discover.

NOTE: this package is named ``_template`` so the registry SKIPS it. Copy the
folder to ``<name>/`` (no underscore) to make it a live, discovered adapter.
"""

from __future__ import annotations

from relay.adapters.integrations._template.listener import TemplateListener
from relay.adapters.integrations._template.sink import TemplateSink
from relay.adapters.registry import AdapterContext, AdapterManifest
from relay.core.lifecycle import IncidentLifecycleEvent


def build(ctx: AdapterContext) -> TemplateListener | None:
    """Build the listener, or None when the integration isn't configured."""
    sink = TemplateSink.from_env(secret_fetcher=ctx.secret_fetcher)
    if sink is None:
        return None
    return TemplateListener(sink, ctx.incident_store)


MANIFEST = AdapterManifest(
    name="template",
    build=build,
    events=(IncidentLifecycleEvent.TRIGGERED, IncidentLifecycleEvent.RESOLVED),
    required_env=("RELAY_TEMPLATE_TOKEN_SECRET", "RELAY_TEMPLATE_BASE_URL"),
)
