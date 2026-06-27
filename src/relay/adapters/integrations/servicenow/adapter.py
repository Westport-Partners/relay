"""ServiceNow adapter manifest — the plug point the registry discovers."""

from __future__ import annotations

from collections.abc import Callable

from relay.adapters.integrations.servicenow.listener import ServiceNowListener
from relay.adapters.integrations.servicenow.sink import ServiceNowSink
from relay.adapters.registry import AdapterContext, AdapterManifest
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.settings import SettingsKey


def build(ctx: AdapterContext) -> ServiceNowListener | None:
    """Build the ServiceNow listener, or None when ServiceNow isn't configured.

    Credential precedence: UI-set values (settings store) override the
    env/Secrets-Manager fallback, resolved live per request via the providers
    (mirrors the GitLab token precedence — see [[gitlab-token-settings-precedence]]).
    """
    def _setting_provider(key: SettingsKey) -> Callable[[], str | None]:
        def _provider() -> str | None:
            if ctx.settings_store is None:
                return None
            try:
                return ctx.settings_store.get(key) or None
            except Exception:
                return None
        return _provider

    sink = ServiceNowSink.from_env(
        secret_fetcher=ctx.secret_fetcher,
        instance_url_provider=_setting_provider(SettingsKey.SERVICENOW_INSTANCE_URL),
        username_provider=_setting_provider(SettingsKey.SERVICENOW_USERNAME),
        password_provider=_setting_provider(SettingsKey.SERVICENOW_PASSWORD),
    )
    if sink is None:
        return None
    return ServiceNowListener(sink, ctx.incident_store)


MANIFEST = AdapterManifest(
    name="servicenow",
    build=build,
    events=(IncidentLifecycleEvent.TRIGGERED, IncidentLifecycleEvent.RESOLVED),
    required_env=(
        "RELAY_SERVICENOW_INSTANCE_URL",
        "RELAY_SERVICENOW_USERNAME",
        "RELAY_SERVICENOW_SECRET",
    ),
    settings_keys=(
        SettingsKey.SERVICENOW_INSTANCE_URL,
        SettingsKey.SERVICENOW_USERNAME,
        SettingsKey.SERVICENOW_PASSWORD,
    ),
)
