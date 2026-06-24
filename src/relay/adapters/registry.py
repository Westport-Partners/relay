"""Adapter registry — discovers adapter packages and builds their listeners.

Every donatable integration adapter is a package under ``src/relay/adapters/``
that exposes a module-level ``MANIFEST`` (an :class:`AdapterManifest`). The
registry scans the adapters namespace for those manifests and asks each one to
``build`` a lifecycle listener from a shared :class:`AdapterContext`. A new
adapter therefore plugs in by *adding a folder* — no edit to the Hub.

See ``src/relay/adapters/README.md`` for the adapter contract and ``_template/``
for a copy-paste skeleton.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from relay.core.lifecycle import IncidentLifecycleEvent

logger = logging.getLogger(__name__)


@dataclass
class AdapterContext:
    """Shared dependencies an adapter's ``build`` may pull from.

    The Hub fills this once and passes it to every adapter's ``build``. Adapters
    take only what they need; unused fields are simply ignored. This is the one
    seam between the Hub and the adapters — keep it dependency-light and stable
    so donated adapters have a fixed target.
    """

    incident_store: Any = None
    settings_store: Any = None
    dashboard_url: str = ""
    # Resolve a deployment_id -> the catalog/org-tree value for a given key
    # (e.g. an adapter's project/service id). Adapters that route per deployment
    # use this instead of reaching into the org tree themselves.
    deployment_resolver: Callable[[str, str], str | None] | None = None
    # Fetch a plaintext secret by name (Secrets Manager in prod). Injected so
    # adapter modules carry no cloud SDK dependency.
    secret_fetcher: Callable[[str], str] | None = None
    # Attach a t=0 AI brief (builtin; not a donatable concern).
    attach_ai_brief: Callable[[Any], None] | None = None


@dataclass
class AdapterManifest:
    """What an adapter package declares about itself.

    Attributes:
        name:              Stable short id, e.g. ``"gitlab"``. Used in logs + docs.
        build:             ``build(ctx) -> IncidentListener | None``. Returns None
                           when the adapter is not configured (so a bare Hub simply
                           has fewer listeners — no None-guards at dispatch).
        events:            Lifecycle events this adapter reacts to (docs/preflight).
        required_env:      Env vars the adapter reads (docs/preflight only).
        settings_keys:     Runtime settings-store keys it reads (docs/preflight).
        builtin:           True for first-party non-donatable listeners (AI brief).
        required_metadata: Deployment-metadata keys this adapter needs to function
                           (e.g. ``"gitlab_project"``).  Used by the preflight gate
                           to check every catalog leaf before a deployment; never
                           enforced at runtime (the adapter degrades gracefully when
                           a key is absent).
        suggested_tag_map: Hints for the preflight and placeholder generator only.
                           Maps each ``required_metadata`` key to the recommended
                           AWS resource-tag name to source it from (e.g.
                           ``{"gitlab_project": "GITLAB_PROJECT_ID"}``).  Never
                           consulted at runtime — that is ``deployment_defaults.tag_map``
                           in hierarchy.yaml.
    """

    name: str
    build: Callable[[AdapterContext], Any | None]
    events: tuple[IncidentLifecycleEvent, ...] = ()
    required_env: tuple[str, ...] = ()
    settings_keys: tuple[str, ...] = ()
    builtin: bool = False
    required_metadata: tuple[str, ...] = ()
    suggested_tag_map: dict[str, str] = field(default_factory=dict)


def discover_manifests() -> list[AdapterManifest]:
    """Find every adapter package under ``relay.adapters.integrations``.

    Scans only the ``integrations`` package — the single home for discoverable
    lifecycle adapters — and imports each subpackage's ``adapter`` module; any
    that defines a ``MANIFEST`` of type :class:`AdapterManifest` is included.
    Packages whose name starts with ``_`` (e.g. ``_template``) are skipped. The
    sibling ``aws`` (substrate) and ``ai`` (providers) packages live outside
    ``integrations`` and are therefore never scanned — no skip-list needed.
    Import/attribute errors are logged and skipped so one broken donated adapter
    can't take down the Hub.
    """
    import relay.adapters.integrations as integrations_pkg

    manifests: list[AdapterManifest] = []
    for info in pkgutil.iter_modules(integrations_pkg.__path__):
        if not info.ispkg or info.name.startswith("_"):
            continue
        mod_name = f"{integrations_pkg.__name__}.{info.name}.adapter"
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            logger.warning("Adapter %r failed to import; skipping", info.name, exc_info=True)
            continue
        manifest = getattr(module, "MANIFEST", None)
        if isinstance(manifest, AdapterManifest):
            manifests.append(manifest)
        else:
            logger.warning("Adapter %r has no MANIFEST; skipping", info.name)
    return manifests


def build_listeners(
    ctx: AdapterContext,
    *,
    manifests: list[AdapterManifest] | None = None,
    builtins: list[AdapterManifest] | None = None,
) -> list[Any]:
    """Build the lifecycle-listener set from discovered + builtin adapters.

    Each manifest's ``build(ctx)`` is called inside its own try/except so a bad
    adapter can't block the others; a None result means "not configured" and is
    dropped. ``manifests`` defaults to discovery; pass an explicit list in tests.
    ``builtins`` are appended (e.g. the AI-brief listener) so they go through the
    same path without being discoverable folders.
    """
    found = discover_manifests() if manifests is None else manifests
    all_manifests = [*found, *(builtins or [])]
    listeners: list[Any] = []
    for manifest in all_manifests:
        try:
            listener = manifest.build(ctx)
        except Exception:
            logger.warning(
                "Adapter %r build() failed; skipping", manifest.name, exc_info=True
            )
            continue
        if listener is not None:
            listeners.append(listener)
    return listeners
