"""GitLab adapter manifest — the plug point the registry discovers.

Declares what the GitLab integration is and how to build its lifecycle listener
from the shared :class:`~relay.adapters.registry.AdapterContext`. Everything
GitLab-specific (env vars, token precedence, issue/DORA behaviour) lives in this
package; the Hub only calls ``build``.
"""

from __future__ import annotations

from collections.abc import Callable

from relay.adapters.integrations.gitlab.listener import GitLabListener
from relay.adapters.integrations.gitlab.sink import GitLabSink
from relay.adapters.registry import AdapterContext, AdapterManifest
from relay.core.lifecycle import IncidentLifecycleEvent
from relay.core.settings import SettingsKey


def build(ctx: AdapterContext) -> GitLabListener | None:
    """Build the GitLab listener, or None when GitLab isn't configured.

    Token precedence: a UI-set token (settings store) overrides the Secrets
    Manager fallback, resolved live per request via the provider (see the
    package README + [[gitlab-token-settings-precedence]]).
    """
    def _token_provider() -> str | None:
        if ctx.settings_store is None:
            return None
        try:
            return ctx.settings_store.get(SettingsKey.GITLAB_TOKEN) or None
        except Exception:
            return None

    sink = GitLabSink.from_env(
        token_provider=_token_provider,
        secret_fetcher=ctx.secret_fetcher,
    )
    if sink is None:
        return None

    # Adapt the generic (deployment_id, key) resolver to the project lookup.
    project_resolver: Callable[[str], str | None] | None = None
    if ctx.deployment_resolver is not None:
        _dep_resolver = ctx.deployment_resolver

        def project_resolver(deployment_id: str) -> str | None:
            return _dep_resolver(deployment_id, "gitlab_project")

    return GitLabListener(
        sink, ctx.incident_store, project_resolver=project_resolver
    )


MANIFEST = AdapterManifest(
    name="gitlab",
    build=build,
    events=(IncidentLifecycleEvent.TRIGGERED, IncidentLifecycleEvent.RESOLVED),
    required_env=(
        "RELAY_GITLAB_TOKEN_SECRET",
        "RELAY_GITLAB_PROJECT_ID",
        "RELAY_GITLAB_BASE_URL",
        "RELAY_GITLAB_ENV_TIER_MAP",
    ),
    settings_keys=(SettingsKey.GITLAB_TOKEN,),
    required_metadata=("gitlab_project",),
    suggested_tag_map={"gitlab_project": "GITLAB_PROJECT_ID"},
)
