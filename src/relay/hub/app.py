"""Relay Hub — Fargate service entrypoint for the central role.

Responsibilities:
  - Consume incident events from the central EventBridge bus (SQS queue backed)
  - Aggregate and correlate incidents across all team accounts (~200)
  - Drive ServiceNow incident creation/update and GitLab issue creation
  - Serve the live fleet big-board dashboard (health tiles per app)
  - Page the central on-call team

Architecture:
  - Long-running process (Fargate container)
  - FastAPI app serving health endpoint and dashboard API
  - Background thread consuming SQS (EventBridge -> SQS -> Hub)
  - DynamoDB for aggregated incident state and fleet health
  - SSE /stream endpoint for real-time tile updates (push, not poll)
  - 30-second sweep timer recomputes liveness and emits deltas

Environment variables required:
  RELAY_ROLE: must be 'hub'
  RELAY_SQS_QUEUE_URL: SQS queue URL fed by the central EventBridge bus
  RELAY_DYNAMO_INCIDENTS_TABLE: fleet-wide incident state table
  RELAY_DYNAMO_CONTACTS_TABLE: central team contacts
  RELAY_FLEET_TABLE: fleet health table (defaults to RELAY_DYNAMO_INCIDENTS_TABLE)
  RELAY_SNS_TOPIC_ARN: central team notification topic
  RELAY_SERVICENOW_INSTANCE_URL: e.g. https://yourinstance.service-now.com
  RELAY_SERVICENOW_USERNAME: ServiceNow user
  RELAY_SERVICENOW_SECRET: Secrets Manager secret name for ServiceNow password
  RELAY_GITLAB_PROJECT_ID: optional fallback GitLab project (the project is
      normally resolved per incident from the catalog/org tree)
  RELAY_GITLAB_TOKEN_SECRET: Secrets Manager secret for GitLab token (fallback;
      a UI-set token in the settings store takes precedence)
  RELAY_GITLAB_BASE_URL: GitLab instance base URL (default https://gitlab.com)
  RELAY_GITLAB_ENV_TIER_MAP: maps Relay env -> GitLab env tier for DORA, e.g.
      "prod:production,staging:staging,test:testing"
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import boto3
import yaml
from botocore.exceptions import ClientError

from relay.adapters.aws.dynamo_stores import (
    DynamoContactStore,
    DynamoIgnoreRuleStore,
    DynamoIncidentStore,
    DynamoRoutingRuleStore,
    DynamoScheduleStore,
    DynamoSettingsStore,
)
from relay.adapters.aws.sns_notifier import SNSNotifier
from relay.adapters.integrations.gitlab import GitLabSink
from relay.adapters.integrations.servicenow import ServiceNowSink
from relay.core.lifecycle import IncidentLifecycleEvent, dispatch
from relay.core.logging_config import configure_logging
from relay.core.model import (
    Contact,
    Incident,
    IncidentState,
    OrgTree,
    Stream,
    TimelineEvent,
)
from relay.core.settings import SettingsKey
from relay.hub.fleet_store import FleetStore
from relay.hub.health import (
    DEFAULT_CADENCE_SECONDS,
    FleetTile,
    liveness_from_heartbeat,
    status_sort_key,
    worst_of,
)

logger = logging.getLogger(__name__)

# Map an incident's persisted state to the lifecycle event dispatched when the
# Hub sees a genuine transition into that state over the bus. ACKNOWLEDGED and
# RESOLVED are driven from their API endpoints (the Hub owns those transitions),
# so they are intentionally absent here to avoid double-dispatch.
_STATE_TO_LIFECYCLE_EVENT: dict[IncidentState, IncidentLifecycleEvent] = {
    IncidentState.TRIGGERED: IncidentLifecycleEvent.TRIGGERED,
    IncidentState.ESCALATED: IncidentLifecycleEvent.ESCALATED,
}

# EventBridge detail-type for raw CloudWatch alarm events that the prod SQS
# ingress routes to the detection pipeline (collapse Step 3).
_CLOUDWATCH_ALARM_DETAIL_TYPE = "CloudWatch Alarm State Change"

# ---------------------------------------------------------------------------
# Hub scope
# ---------------------------------------------------------------------------


class HubScope(StrEnum):
    """Deployment scope for the Hub, read from RELAY_HUB_SCOPE env var.

    Controls whether the Hub forwards events to a central Hub:

    * local          — standalone, same-account only; no forwarding (default).
    * local-federated — same-account bus; forwards selected events to central Hub.
    * central        — org-wide bus (requires PrincipalOrgID grant); no forwarding.
    """

    LOCAL = "local"
    LOCAL_FEDERATED = "local-federated"
    CENTRAL = "central"

    @classmethod
    def from_env(cls) -> HubScope:
        """Parse RELAY_HUB_SCOPE from env; return LOCAL if unset or invalid.

        An invalid value logs a warning and falls back to LOCAL so deployment
        misconfiguration doesn't silently break the Hub.
        """
        raw = os.environ.get("RELAY_HUB_SCOPE", "local").strip().lower()
        try:
            return cls(raw)
        except ValueError:
            logger.warning(
                "Unknown RELAY_HUB_SCOPE=%r; defaulting to 'local'. "
                "Valid values: local, local-federated, central.",
                raw,
            )
            return cls.LOCAL


def _team_timezone() -> Any:
    """The team's wall-clock timezone for schedule resolution.

    Schedules are authored as local wall-clock (the grid shows 'Fri 16-24'
    with no UTC qualifier), so 'who's on call now' must resolve against the
    team's local time, not UTC. Configurable via RELAY_TZ (IANA name, e.g.
    'America/New_York'); defaults to UTC.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = os.environ.get("RELAY_TZ", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("invalid RELAY_TZ=%r; falling back to UTC", name)
        return ZoneInfo("UTC")


def _resolve_now_on_call(
    schedule_store: Any, now: datetime, names: dict[str, str]
) -> dict[str, Any] | None:
    """Who is on call right now per the generated schedule (None if no schedule
    covers this moment). This is the authoritative paging answer when present.

    ``now`` is UTC; it is converted to the team's local wall-clock time
    (RELAY_TZ) before resolving date + shift, because the schedule is authored
    in local time.
    """
    if schedule_store is None:
        return None
    try:
        from relay.core.scheduling import (
            Role,
            monday_of,
            schedule_from_stored,
            shift_for_hour,
        )

        # Resolve against the team's local wall-clock, not UTC.
        local = now.astimezone(_team_timezone())
        ws = monday_of(local.date())
        stored = schedule_store.get_schedule(ws.isoformat())
        if not stored:
            return None
        # Overlay ad-hoc overrides (cover-me) so paging respects them.
        try:
            from relay.core.scheduling import apply_overrides
            overrides = schedule_store.get_overrides(ws.isoformat())
            if overrides:
                stored = apply_overrides(stored, overrides)
        except Exception:
            logger.warning("applying overrides failed; using base schedule", exc_info=True)
        sched = schedule_from_stored(stored)
        shift = shift_for_hour(local.hour)
        # Does a slot exist for this date+shift at all?
        has_slot = any(s.date == local.date() and s.shift == shift for s in sched.slots)
        if not has_slot:
            return None
        # All roles' assignees for this moment (naive local wall-clock).
        naive_local = local.replace(tzinfo=None)
        by_role = sched.assignments_at(naive_local)
        roles_out: dict[str, Any] = {}
        for role, rcid in by_role.items():
            roles_out[str(role)] = (
                {"contact_id": rcid, "name": names.get(rcid, rcid)} if rcid
                else {"contact_id": None, "name": None, "gap": True}
            )
        # Top-level fields mirror PRIMARY for backward compatibility.
        primary = by_role.get(Role.PRIMARY)
        result: dict[str, Any] = {
            "shift": str(shift),
            "source": "schedule",
            "roles": roles_out,
        }
        if primary:
            result["contact_id"] = primary
            result["name"] = names.get(primary, primary)
        else:
            result["contact_id"] = None
            result["name"] = None
            result["gap"] = True
        return result
    except Exception:
        logger.warning("schedule-backed on-call resolution failed", exc_info=True)
        return None


def _load_hub_config() -> Any | None:
    """Load Relay config (escalation + routing + federation) for the Hub.

    Best-effort: a local Hub may run without a config source. Returns the
    validated RelayConfig or None if no source is configured / load fails.
    The federation gate and the On-Call view both read from this; a None
    result simply means the Hub falls back to its env-var forwarding knobs.
    """
    try:
        from relay.config.loader import GitLabConfigLoader
        from relay.config.local_loader import LocalConfigLoader

        cfg_source = os.environ.get(
            "RELAY_CONFIG_SOURCE",
            "local" if os.environ.get("RELAY_CONFIG_DIR") else "",
        )
        if cfg_source == "local":
            return LocalConfigLoader(
                os.environ.get("RELAY_CONFIG_DIR", "config")
            ).get()
        if os.environ.get("RELAY_GITLAB_REPO"):
            return GitLabConfigLoader(
                os.environ["RELAY_GITLAB_REPO"],
                secrets_manager_secret_name=os.environ.get(
                    "RELAY_GITLAB_SECRET_NAME", "relay/gitlab-token"
                ),
            ).get()
    except Exception:
        logger.warning("Hub config load failed; On-Call view disabled", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Optional FastAPI / uvicorn — degrade gracefully if not installed
# ---------------------------------------------------------------------------

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, StreamingResponse

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = None  # type: ignore[assignment,misc]

# The dashboard UI is authored as ordered fragments under dashboard_parts/
# (the document open, the <style> sheet, and the body shell) assembled into a
# single HTML document at serve time. The JavaScript is no longer concatenated:
# it lives as native ES modules under dashboard_modules/, served read-only at
# /static/dashboard/ and loaded by the shell via <script type="module">. Editing
# a CSS/markup section means editing its fragment; editing behavior means editing
# a module. A monolithic dashboard.html is still honored as a fallback.
_DASHBOARD_DIR = pathlib.Path(__file__).parent
_DASHBOARD_PARTS_DIR = _DASHBOARD_DIR / "dashboard_parts"
_DASHBOARD_MODULES_DIR = _DASHBOARD_DIR / "dashboard_modules"
_DASHBOARD_HTML_PATH = _DASHBOARD_DIR / "dashboard.html"


def _render_dashboard_html() -> str:
    """Assemble the dashboard HTML from ordered fragments.

    Reads ``dashboard_parts/manifest.txt`` (ignoring blank/``#`` lines) and
    concatenates each named fragment in order. Falls back to a monolithic
    ``dashboard.html`` if the parts directory or manifest is absent.
    """
    manifest = _DASHBOARD_PARTS_DIR / "manifest.txt"
    if manifest.is_file():
        names = [
            ln.strip()
            for ln in manifest.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        return "".join(
            (_DASHBOARD_PARTS_DIR / name).read_text(encoding="utf-8")
            for name in names
        )
    return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# In-memory cache (HubState) — fronts FleetStore
# ---------------------------------------------------------------------------


class HubState:
    """Thread-safe in-memory cache of FleetTile objects, fronting FleetStore.

    Liveness is computed at READ time from last_heartbeat_at (not stored as
    a static flag) so a tile goes red on silence even without an inbound event.

    The cache is keyed by ``{account_id}/{app_name}``.
    """

    def __init__(
        self,
        fleet_store: FleetStore,
        cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = fleet_store
        self._cadence = cadence_seconds
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._tiles: dict[str, FleetTile] = {}
        # Org hierarchy built purely from node registrations (heartbeat org_path).
        # The Hub stores no static catalog; this is the live, registration-derived
        # tree powering /fleet/rollup. Keyed by leaf deployment id.
        self._org_paths: dict[str, list[dict[str, Any]]] = {}
        self._org_tree: OrgTree | None = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Boot hydration
    # ------------------------------------------------------------------

    def hydrate(self) -> None:
        """Load all tiles from DynamoDB into the in-memory cache on startup."""
        tiles = self._store.hydrate()
        # Rebuild the registration-derived org tree from persisted org_path data
        # so /fleet/rollup works immediately after a Hub restart (no catalog file).
        try:
            tree = self._store.build_org_tree()
        except Exception:
            logger.warning("org tree rebuild on hydrate failed", exc_info=True)
            tree = None
        with self.lock:
            self._tiles = {t.key: t for t in tiles}
            self._org_tree = tree
        logger.info("HubState hydrated %d tiles from FleetStore", len(tiles))

    # ------------------------------------------------------------------
    # Snapshot / read
    # ------------------------------------------------------------------

    def get_tiles(self) -> list[FleetTile]:
        """Return a snapshot list of all tiles, liveness recomputed at call time."""
        with self.lock:
            raw = list(self._tiles.values())
        # Recompute liveness at read time — do this OUTSIDE the lock to keep it short.
        refreshed = []
        for tile in raw:
            liveness = liveness_from_heartbeat(
                tile.last_heartbeat_at,
                cadence_seconds=self._cadence,
                clock=self._clock,
            )
            status = worst_of(
                liveness,
                open_incidents=tile.open_incidents,
                worst_severity=tile.worst_severity,
            )
            # Return a new tile only if something changed.
            if liveness != tile.liveness or status != tile.status:
                tile = FleetTile(
                    account_id=tile.account_id,
                    app_name=tile.app_name,
                    environment=tile.environment,
                    deployment_id=tile.deployment_id,
                    service_path=tile.service_path,
                    org_path=tile.org_path,
                    metadata=tile.metadata,
                    on_call=tile.on_call,
                    status=status,
                    liveness=liveness,
                    open_incidents=tile.open_incidents,
                    worst_severity=tile.worst_severity,
                    last_heartbeat_at=tile.last_heartbeat_at,
                    registered_at=tile.registered_at,
                    last_updated=self._clock(),
                )
            refreshed.append(tile)
        return sorted(refreshed, key=status_sort_key)

    def get_tile(self, account_id: str, app_name: str) -> FleetTile | None:
        with self.lock:
            # Scan for matching account_id + app_name (key is now env/deployment_id)
            tile = next(
                (t for t in self._tiles.values()
                 if t.account_id == account_id and t.app_name == app_name),
                None,
            )
        if tile is None:
            return None
        # Recompute liveness.
        liveness = liveness_from_heartbeat(
            tile.last_heartbeat_at,
            cadence_seconds=self._cadence,
            clock=self._clock,
        )
        status = worst_of(
            liveness,
            open_incidents=tile.open_incidents,
            worst_severity=tile.worst_severity,
        )
        return FleetTile(
            account_id=tile.account_id,
            app_name=tile.app_name,
            environment=tile.environment,
            deployment_id=tile.deployment_id,
            service_path=tile.service_path,
            org_path=tile.org_path,
            metadata=tile.metadata,
            on_call=tile.on_call,
            status=status,
            liveness=liveness,
            open_incidents=tile.open_incidents,
            worst_severity=tile.worst_severity,
            last_heartbeat_at=tile.last_heartbeat_at,
            registered_at=tile.registered_at,
            last_updated=self._clock(),
        )

    # ------------------------------------------------------------------
    # Mutation helpers (called by HubProcessor)
    # ------------------------------------------------------------------

    def upsert_tile(self, tile: FleetTile) -> None:
        """Insert or replace a tile in the cache."""
        with self.lock:
            self._tiles[tile.key] = tile

    def cached_tile(self, key: str) -> FleetTile | None:
        """Lock-safe read of one cached tile by key (no liveness recompute).

        Used by the sweep loop, which now shares the cache with the SQS consumer
        and in-process pipeline threads — a bare ``_tiles.get`` would race a
        concurrent ``upsert_tile``.
        """
        with self.lock:
            return self._tiles.get(key)

    def update_app(self, incident: Incident) -> FleetTile:
        """Update cache from an incident event (and persist via FleetStore).

        Fixes the datetime.utcnow() deprecation — uses timezone-aware UTC.
        Returns the updated tile.
        """
        tile = self._store.apply_incident(incident)
        self.upsert_tile(tile)
        logger.debug(
            "HubState updated: key=%s status=%s open=%s",
            tile.key,
            tile.status,
            tile.open_incidents,
        )
        return tile

    def recompute_tile(
        self,
        account_id: str,
        app_name: str,
        open_incidents: list[Incident],
        environment: str = "unrouted",
        deployment_id: str | None = None,
    ) -> FleetTile | None:
        """Recompute one tile's aggregate from surviving open incidents + cache it.

        Used after a purge deletes incident rows directly (bypassing the
        per-event decrement). Delegates to ``FleetStore.recompute``; returns the
        refreshed tile (and updates the in-memory cache), or ``None`` when the
        tile is not registered.
        """
        tile = self._store.recompute(
            account_id, app_name, open_incidents, environment, deployment_id
        )
        if tile is not None:
            self.upsert_tile(tile)
            logger.debug(
                "HubState recomputed: key=%s status=%s open=%s",
                tile.key,
                tile.status,
                tile.open_incidents,
            )
        return tile

    def record_heartbeat(
        self,
        account_id: str,
        app_name: str,
        ts: datetime,
        environment: str = "unrouted",
        deployment_id: str | None = None,
        service_path: list[str] | None = None,
        org_path: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        on_call: dict[str, Any] | None = None,
    ) -> FleetTile:
        """Record a heartbeat, persisting to DynamoDB and updating cache.

        When the heartbeat carries an ``org_path`` (the node's org ancestry),
        merge it into the dynamically-built org tree so /fleet/rollup reflects
        the live hierarchy without any Hub-side catalog. ``metadata`` and
        ``on_call`` (the owning team's pushed on-call snapshot) ride through to
        storage so the tile-detail drawer can show them on a federated Hub.
        """
        tile = self._store.record_heartbeat(
            account_id, app_name, ts, environment, deployment_id, service_path,
            org_path, metadata, on_call,
        )
        self.upsert_tile(tile)
        if org_path:
            self._merge_org_path(org_path)
        logger.debug("Heartbeat recorded: %s/%s ts=%s", account_id, app_name, ts)
        return tile

    def _merge_org_path(self, org_path: list[dict[str, Any]]) -> None:
        """Fold one node's org_path into the in-memory registration set + tree."""
        with self.lock:
            self._org_paths[self._org_path_key(org_path)] = org_path
            paths = list(self._org_paths.values())
        try:
            tree = OrgTree.from_registrations(paths)
        except Exception:
            logger.warning("failed to rebuild org tree from registrations", exc_info=True)
            return
        # Publish the rebuilt tree under the lock — three threads (SQS consumer,
        # sweep, in-process pipeline) may read/write _org_tree concurrently.
        with self.lock:
            self._org_tree = tree

    @staticmethod
    def _org_path_key(org_path: list[dict[str, Any]]) -> str:
        """Stable key for a registration: the leaf (deployment) node id."""
        return str(org_path[-1].get("id")) if org_path else ""

    def get_org_tree(self) -> OrgTree | None:
        """Return the org tree built from registrations, or None if empty."""
        with self.lock:
            return self._org_tree

    # ------------------------------------------------------------------
    # Legacy compat (used by existing /health endpoint)
    # ------------------------------------------------------------------

    @property
    def fleet(self) -> dict[str, FleetTile]:
        """Read-only view of the internal tiles dict (for fleet_size in /health)."""
        with self.lock:
            return dict(self._tiles)


# ---------------------------------------------------------------------------
# SSE publisher
# ---------------------------------------------------------------------------


class SSEPublisher:
    """Manages a set of SSE subscriber queues and publishes events to them.

    Each connected /stream client gets its own asyncio-friendly queue.
    The sweep timer and event handlers push to this publisher; the SSE
    endpoint generator reads from the per-client queue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.SimpleQueue[str]] = set()

    def subscribe(self) -> queue.SimpleQueue[str]:
        """Register a new subscriber and return its queue."""
        q: queue.SimpleQueue[str] = queue.SimpleQueue[str]()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.SimpleQueue[str]) -> None:
        """Remove a subscriber (called when client disconnects)."""
        with self._lock:
            self._subscribers.discard(q)

    def publish_delta(self, tile: FleetTile) -> None:
        """Push a single tile delta to all subscribers."""
        payload = json.dumps(tile.to_dict())
        msg = f"event: delta\ndata: {payload}\n\n"
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass  # Dead subscriber; will be cleaned up on disconnect.

    def publish_ping(self) -> None:
        """Push a named SSE 'ping' event to all subscribers.

        Uses a real named event (not a ': ' comment): the browser's EventSource
        fires no JS event for comments, so the client can't use them for
        liveness. A named event lets the dashboard refresh its connection timer.
        """
        msg = "event: ping\ndata: {}\n\n"
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Sweep timer
# ---------------------------------------------------------------------------

_SWEEP_INTERVAL_SECONDS = 30
_PING_INTERVAL_SECONDS = 15


class DeadlineSweeper:
    """Fires due escalation deadlines from DynamoDB on each sweep tick.

    The collapsed single-container replacement for EventBridge Scheduler
    (collapsed-single-container plan §3 / Step 2). The Scheduler used to call
    back the Node Lambda when a step's timeout expired; here the always-on
    container polls a DynamoDB deadline store instead. Each tick:

      1. ``query_due_deadlines()`` — PENDING deadlines whose ``fire_at`` passed.
      2. ``claim_deadline()`` — atomically PENDING→FIRED so overlapping sweeps
         (or a deadline lingering after EXHAUSTED) never double-fire.
      3. ``fire(incident_id, step_index)`` — the in-process timeout entry point
         (``DetectionPipeline.handle_timeout``), which advances the escalation
         engine and re-runs the Node→Hub effects.

    A failure on one deadline is isolated and logged; the rest still fire.
    """

    def __init__(
        self,
        timer: Any,
        fire: Callable[[str, int], Any],
    ) -> None:
        self._timer = timer
        self._fire = fire

    def sweep_once(self) -> int:
        """Fire all currently-due deadlines. Returns the count fired."""
        try:
            due = self._timer.query_due_deadlines()
        except Exception:
            logger.exception("DeadlineSweeper: query_due_deadlines failed")
            return 0

        fired = 0
        for deadline in due:
            try:
                if not self._timer.claim_deadline(
                    deadline.incident_id, deadline.step_index
                ):
                    # Another sweep tick (or an advance/ack) already took it.
                    continue
                logger.info(
                    "DeadlineSweeper: firing escalation timeout incident=%s step=%s "
                    "(fire_at=%s)",
                    deadline.incident_id,
                    deadline.step_index,
                    deadline.fire_at,
                )
                self._fire(deadline.incident_id, deadline.step_index)
                fired += 1
            except Exception:
                logger.exception(
                    "DeadlineSweeper: error firing deadline incident=%s step=%s",
                    deadline.incident_id,
                    deadline.step_index,
                )
        return fired


class SweepTimer:
    """Background daemon thread that recomputes liveness every 30s.

    Emits deltas to SSE subscribers when a tile's status changes.
    Also sends SSE ping comments every ~15s for browser-side freeze detection.
    Stops cleanly on shutdown_event.set().
    """

    def __init__(
        self,
        hub_state: HubState,
        sse_publisher: SSEPublisher,
        shutdown_event: threading.Event,
        sweep_interval: int = _SWEEP_INTERVAL_SECONDS,
        ping_interval: int = _PING_INTERVAL_SECONDS,
        deadline_sweeper: DeadlineSweeper | None = None,
    ) -> None:
        self._hub_state = hub_state
        self._sse = sse_publisher
        self._shutdown = shutdown_event
        self._sweep_interval = sweep_interval
        self._ping_interval = ping_interval
        # Optional: fires due escalation deadlines each sweep (collapsed
        # single-container runtime — plan §3 / Step 2). None on a remote-Node
        # deployment where EventBridge Scheduler still owns the timers.
        self._deadline_sweeper = deadline_sweeper

    def run(self) -> None:
        """Main loop — run as a daemon thread."""
        last_sweep = 0.0
        last_ping = 0.0
        logger.info("SweepTimer started (sweep=%ds, ping=%ds)", self._sweep_interval, self._ping_interval)
        while not self._shutdown.is_set():
            now = time.monotonic()

            if now - last_ping >= self._ping_interval:
                self._sse.publish_ping()
                last_ping = now

            if now - last_sweep >= self._sweep_interval:
                self._do_sweep()
                if self._deadline_sweeper is not None:
                    self._deadline_sweeper.sweep_once()
                last_sweep = now

            self._shutdown.wait(timeout=1.0)

        logger.info("SweepTimer stopped")

    def _do_sweep(self) -> None:
        """Recompute liveness for all tiles; publish deltas for changed ones."""
        try:
            tiles = self._hub_state.get_tiles()
        except Exception:
            logger.exception("SweepTimer: error fetching tiles")
            return

        for tile in tiles:
            # get_tiles() already recomputes liveness.  Update cache + emit delta.
            old = self._hub_state.cached_tile(tile.key)
            if old is None or old.status != tile.status or old.liveness != tile.liveness:
                self._hub_state.upsert_tile(tile)
                self._sse.publish_delta(tile)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="sweep-timer", daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------------------
# Hub processor
# ---------------------------------------------------------------------------


class HubProcessor:
    """Processes inbound events from team nodes (incidents + heartbeats)."""

    def __init__(
        self,
        incident_store: DynamoIncidentStore,
        notifier: SNSNotifier,
        hub_state: HubState,
        sse_publisher: SSEPublisher,
        forwarder: Any | None = None,
        federation: Any | None = None,
        settings_store: Any | None = None,
        dashboard_url: str = "",
        secret_fetcher: Callable[[str], str] | None = None,
        listeners: list[Any] | None = None,
    ) -> None:
        self._incident_store = incident_store
        self._notifier = notifier
        self._hub_state = hub_state
        self._sse = sse_publisher
        # Settings store (Teams webhook URL etc.) — optional.
        self._settings_store = settings_store
        self._dashboard_url = dashboard_url
        # Injected secret fetcher (Secrets Manager in prod) passed to adapters'
        # from_env() so the adapter modules carry no cloud SDK dependency.
        self._secret_fetcher = secret_fetcher

        # --- Lifecycle-event seam ---------------------------------------
        # Adapters subscribe to standard incident events (TRIGGERED/RESOLVED/…).
        # The registry discovers each adapter package's MANIFEST and builds its
        # listener from a shared AdapterContext; dispatch() fans events out with
        # per-listener failure isolation. Tests may inject ``listeners`` directly.
        self._listeners = listeners if listeners is not None else self._build_listeners()
        # Forwarding seam: injected forwarder (EventBridgeForwarder or NoOpForwarder).
        # Defaults to NoOpForwarder so all existing callers/tests are unaffected.
        from relay.adapters.aws.eventbridge_forwarder import NoOpForwarder
        self._forwarder = forwarder if forwarder is not None else NoOpForwarder()
        # Config-driven federation policy (routing.yaml `federation:` block) is
        # the single source of the forwarding gate. When no block is configured
        # we fall back to FederationConfig() model defaults (min_severity SEV2,
        # all states, no overrides) — there is no env-var gate.
        from relay.config.schema import FederationConfig
        self._federation = federation if federation is not None else FederationConfig()
        # Detection pipeline for raw CloudWatch alarm events arriving over SQS
        # (collapsed-single-container plan §7 / Step 3: EventBridge alarm rule →
        # SQS → container consumer → pipeline, no Node Lambda). Injected after
        # construction via set_pipeline(); None on a Hub that only ingests
        # already-parsed incident events (federated aggregator).
        self._pipeline: Any | None = None

    def set_pipeline(self, pipeline: Any) -> None:
        """Wire the in-process detection pipeline (see ``handle_event``)."""
        self._pipeline = pipeline

    def _build_listeners(self) -> list[Any]:
        """Assemble the incident lifecycle listeners from configured adapters.

        The registry discovers each adapter package's MANIFEST and builds its
        listener, so adding an integration is a new folder under
        ``relay/adapters/`` — no edit here. The builtin AI-brief listener is
        passed through the same path as a builtin manifest.
        """
        from relay.adapters._support import AIBriefListener
        from relay.adapters.registry import (
            AdapterContext,
            AdapterManifest,
            build_listeners,
        )

        ctx = AdapterContext(
            incident_store=self._incident_store,
            settings_store=self._settings_store,
            dashboard_url=self._dashboard_url,
            deployment_resolver=self._resolve_deployment_attr,
            secret_fetcher=self._secret_fetcher,
            attach_ai_brief=self._attach_ai_brief,
        )
        ai_brief = AdapterManifest(
            name="ai_brief",
            build=lambda c: (
                AIBriefListener(c.attach_ai_brief)
                if c.attach_ai_brief is not None
                else None
            ),
            builtin=True,
        )
        return build_listeners(ctx, builtins=[ai_brief])

    def _resolve_deployment_attr(self, deployment_id: str, key: str) -> str | None:
        """Resolve a catalog/org-tree attribute for a deployment by key.

        The federated Hub builds its org tree from node heartbeats (it stores no
        static catalog). Adapters use this generic resolver instead of reaching
        into the org tree themselves — e.g. ``key="gitlab_project"`` for the
        GitLab adapter. Resolves a structural model attribute (e.g.
        ``owner_ref``) or, for integration routing keys, the node's
        ``metadata`` entry. Returns None when there's no tree or the
        node/attr is unknown.
        """
        try:
            org_tree = self._hub_state.get_org_tree()
        except Exception:
            return None
        if org_tree is None:
            return None
        node = org_tree.get(deployment_id)
        if node is None:
            return None
        value = getattr(node, key, None)
        if value is None and isinstance(node.metadata, dict):
            value = node.metadata.get(key)
        return value

    def dispatch_event(
        self, event: IncidentLifecycleEvent, incident: Incident
    ) -> None:
        """Fan a lifecycle event out to all listeners (failure-isolated)."""
        dispatch(self._listeners, event=event, incident=incident)

    def handle_event(self, event: dict[str, Any]) -> None:
        """Dispatch an inbound event.

        Switches on detail-type / relay_event marker:
        - ``relay.heartbeat`` or ``"relay_event": "heartbeat"`` → heartbeat
        - ``CloudWatch Alarm State Change`` → detection pipeline (Step 3:
          raw alarm arrives over SQS; the container parses + detects in-process
          instead of a separate Node Lambda)
        - Everything else → incident event (a peer/federated Hub forwarding an
          already-parsed Incident)
        """
        detail_type = event.get("detail-type", "")
        detail = event.get("detail", event)

        # --- Heartbeat dispatch (Transport option A — same bus/SQS path) ---
        if detail_type == "relay.heartbeat" or detail.get("relay_event") == "heartbeat":
            self._handle_heartbeat(detail)
            return

        # --- Raw CloudWatch alarm → detection pipeline (Step 3 prod ingress) ---
        # The EventBridge "CloudWatch Alarm State Change" rule delivers the raw
        # alarm to SQS; we run the same in-process pipeline as POST /ingest/alarm
        # (parse → classify → persist → page → escalate → on_local_incident),
        # so there is no second bus hop and the headline drop-bug cannot exist.
        if detail_type == _CLOUDWATCH_ALARM_DETAIL_TYPE:
            if self._pipeline is None:
                logger.error(
                    "Received a CloudWatch alarm over SQS but no detection pipeline "
                    "is wired; dropping. (alarm=%s)",
                    detail.get("alarmName"),
                )
                return
            self._pipeline.handle_alarm(event)
            return

        # --- Incident event (forwarded from a peer Hub) ---
        self._handle_incident(event)

    def _handle_heartbeat(self, detail: dict[str, Any]) -> None:
        """Process a relay.heartbeat event."""
        account_id = detail.get("account_id", "")
        app_name = detail.get("app_name", "")
        if not account_id or not app_name:
            logger.warning("Heartbeat event missing account_id or app_name: %r", detail)
            return

        ts_str = detail.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        environment = detail.get("environment", "unrouted")
        deployment_id = detail.get("deployment_id") or None
        service_path = detail.get("service_path") or None
        org_path = detail.get("org_path") or None
        metadata = detail.get("metadata") or None
        on_call = detail.get("on_call") or None

        logger.debug("Heartbeat: account=%s app=%s ts=%s", account_id, app_name, ts)
        tile = self._hub_state.record_heartbeat(
            account_id, app_name, ts, environment, deployment_id, service_path,
            org_path, metadata, on_call,
        )
        self._sse.publish_delta(tile)

    def _should_forward(self, incident: Incident) -> bool:
        """Decide whether *incident* clears the federation gate.

        Delegates entirely to the config-driven :class:`FederationConfig`
        (routing.yaml ``federation:`` block, or model defaults when no block is
        present): global severity threshold + optional state filter + per-app/
        tag overrides. Loop prevention (``relay_forwarded_from``) is the
        caller's responsibility.
        """
        return bool(self._federation.decide_forward(incident))

    def _apply_and_dispatch(
        self, incident: Incident, *, already_forwarded: bool = False
    ) -> None:
        """Apply tile + lifecycle effects for a genuine new incident or state transition.

        Called from both the SQS path (``_handle_incident``) and the in-process
        path (``on_local_incident``) once redelivery / dedup checks have passed.

        Sequence:
          1. Persist the new state so concurrent deliveries see it.
          2. Update the big-board tile (DynamoDB + in-memory cache).
          3. Publish the tile delta to SSE subscribers.
          4. Drive lifecycle adapters (ServiceNow/GitLab/Teams…) via the seam.
          5. Optionally forward to a central Hub (scope=local-federated).

        Args:
            incident: The validated Incident to apply.
            already_forwarded: When ``True`` the §8.3 loop-prevention marker was
                present on the raw event; forwarding is skipped.
        """
        # Persist the new state FIRST so concurrent/duplicate deliveries see it.
        self._incident_store.put_incident(incident)

        # Update the fleet health big-board (DynamoDB + in-memory cache).
        tile = self._hub_state.update_app(incident)

        # Publish tile delta to SSE subscribers.
        self._sse.publish_delta(tile)

        # Drive external adapters via the lifecycle seam on genuine state
        # transitions (redelivery already returned above, so no duplicate
        # tickets). Each adapter is a listener that decides what an event means
        # to it (open issue/ticket, post card, draft brief, escalate); dispatch()
        # isolates per-listener failures, and a bare Hub simply has fewer
        # listeners. TRIGGERED + ESCALATED arrive here over the bus; ACKNOWLEDGED
        # + RESOLVED are dispatched from their respective API endpoints.
        lifecycle_event = _STATE_TO_LIFECYCLE_EVENT.get(incident.state)
        if lifecycle_event is not None:
            self.dispatch_event(lifecycle_event, incident)

        # --- Forward to central Hub if scope=local-federated and both gates pass ---
        # §8.1: severity gate + state filter must both pass.
        # §8.3: never re-forward an already-forwarded event.
        # This runs AFTER all local processing; a forward error never breaks local flow.
        try:
            if already_forwarded:
                logger.debug(
                    "HubProcessor: skipping forward for incident %s — relay_forwarded_from present",
                    incident.correlation_id,
                )
            elif self._should_forward(incident):
                forwarded = self._forwarder.forward(incident)
                if forwarded:
                    logger.info(
                        "Forwarded incident %s (severity=%s state=%s) to central Hub",
                        incident.correlation_id,
                        incident.severity,
                        incident.state,
                    )
        except Exception:
            logger.error(
                "Forwarder error for incident %s — local processing already complete",
                incident.correlation_id,
                exc_info=True,
            )

    def on_local_incident(self, incident: Incident) -> None:
        """In-process entry called by DetectionPipeline's on_incident hook.

        This is the call the buggy cross-process EventBridge hop used to (fail
        to) deliver.  The Node has already persisted the Incident and paged
        on-call; our job here is to apply the tile update (big-board turns red)
        and drive the lifecycle adapters (ServiceNow ticket, GitLab issue, …).

        Design refs: §2 (team topology), §8 (no-drift seam).

        Key differences from the SQS path (``_handle_incident``):
          • No dedup check based on row existence: the Node just wrote the row,
            so ``get_incident`` would immediately return "already present".
          • No relay_forwarded_from loop guard: this is a local Python object,
            not a deserialized event detail that could carry that marker.

        Args:
            incident: Persisted Incident from the Node pipeline.  Must be the
                      same object the Node passed to ``incident_store.put_incident``
                      so correlation_id, state, and severity are already final.
        """
        logger.info(
            "HubProcessor.on_local_incident: applying incident %s state=%s severity=%s "
            "app=%s account=%s",
            incident.correlation_id,
            incident.state,
            incident.severity,
            incident.app_name,
            incident.account_id,
        )
        self._apply_and_dispatch(incident, already_forwarded=False)

    def _handle_incident(self, event: dict[str, Any]) -> None:
        """Process an incident event (existing logic, extended for dedup + federation).

        §8.2 Idempotent ingest: load existing record; if same (correlation_id, state)
        already stored, skip count delta + sinks + forwarding (redelivery).  Only a
        genuine state transition applies effects.

        §8.3 Loop prevention: if the raw detail carries relay_forwarded_from, never
        re-forward (regardless of scope / forwarder configured).
        """
        detail = event.get("detail", event)

        # §8.3 — read loop-prevention marker from raw detail BEFORE model_validate
        # (Incident is a strict model; extra fields are dropped by Pydantic).
        already_forwarded: bool = "relay_forwarded_from" in detail

        incident = Incident.model_validate(detail)

        logger.info(
            "HubProcessor handling incident %s state=%s severity=%s app=%s account=%s",
            incident.correlation_id,
            incident.state,
            incident.severity,
            incident.app_name,
            incident.account_id,
        )

        # §8.2 — dedup: load existing record to distinguish redelivery from transition.
        # Note: in team/in-process mode the Node already wrote the row first, so a
        # separate path (on_local_incident) skips this check entirely.
        existing = self._incident_store.get_incident(incident.correlation_id)
        is_redelivery = existing is not None and existing.state == incident.state

        if is_redelivery:
            logger.info(
                "HubProcessor: redelivery detected for incident %s state=%s — "
                "skipping count delta, sinks, and forwarding",
                incident.correlation_id,
                incident.state,
            )
            # Re-publish current tile state from cache without mutating counts.
            try:
                cached_tile = self._hub_state.get_tile(incident.account_id, incident.app_name)
                if cached_tile is not None:
                    self._sse.publish_delta(cached_tile)
            except Exception:
                pass  # Non-critical; don't let SSE refresh break anything.
            return

        # Genuine new incident or state transition — apply effects.
        self._apply_and_dispatch(incident, already_forwarded=already_forwarded)

    def _attach_ai_brief(self, incident: Incident) -> None:
        """Draft a t=0 briefing and append it to the incident timeline.

        Opt-in (RELAY_AI_ENABLED=true) for the live model; otherwise a
        deterministic brief is attached so the dashboard always has one.
        Best-effort — never raises into incident processing.
        """
        from relay.core.analysis import generate_brief

        # The factory selects the provider from RELAY_AI_PROVIDER and honors the
        # RELAY_AI_ENABLED opt-in; returns None (-> deterministic brief) when AI
        # is off, the provider is unknown, or construction fails. AI augments,
        # never gates.
        assistant = None
        try:
            from relay.adapters.ai import make_assistant
            assistant = make_assistant()
        except Exception:
            logger.warning("AI assistant init failed; deterministic brief", exc_info=True)

        result = generate_brief(incident, assistant)
        now = datetime.now(UTC)
        incident.timeline.append(
            TimelineEvent(
                event_id=f"ai-brief-{int(now.timestamp())}",
                incident_id=incident.correlation_id,
                stream=Stream.CENTRAL,
                occurred_at=now,
                actor="ai" if result["ai_generated"] else "system",
                event_type="ai.brief",
                detail={
                    "ai_generated": result["ai_generated"],
                    "markdown": result["markdown"],
                },
            )
        )
        self._incident_store.put_incident(incident)


# ---------------------------------------------------------------------------
# SQS consumer
# ---------------------------------------------------------------------------


class SQSConsumer:
    """Long-polls an SQS queue and dispatches each message to HubProcessor."""

    def __init__(
        self,
        queue_url: str,
        handler: HubProcessor,
        boto3_session: Any | None = None,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self._queue_url = queue_url
        self._handler = handler
        # Watched so a SIGTERM (via HubApp.request_shutdown) stops the long-poll
        # loop and the task drains cleanly. None → runs until the process exits.
        self._shutdown = shutdown_event
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        # Pin the region explicitly. The SQS URL is region-qualified, but a
        # client created without a region (no AWS_REGION in the container) can
        # resolve to the wrong endpoint and raise QueueDoesNotExist. Derive the
        # region from the queue URL host (sqs.<region>.amazonaws.com), falling
        # back to env / session.
        region = self._region_from_queue_url(queue_url) or os.environ.get(
            "AWS_REGION"
        ) or os.environ.get("AWS_DEFAULT_REGION") or session.region_name
        self._sqs = session.client("sqs", region_name=region)

    @staticmethod
    def _region_from_queue_url(queue_url: str) -> str | None:
        """Extract the region from an SQS queue URL host, if present."""
        # https://sqs.us-east-1.amazonaws.com/<acct>/<name>
        try:
            host = queue_url.split("//", 1)[1].split("/", 1)[0]
            parts = host.split(".")
            if len(parts) >= 3 and parts[0] == "sqs":
                return parts[1]
        except (IndexError, AttributeError):
            pass
        return None

    def run_forever(self) -> None:
        """Long-poll SQS in a loop. Each message is an EventBridge event forwarded by the bus->SQS rule."""
        logger.info("SQSConsumer starting long-poll loop on %s", self._queue_url)
        while self._shutdown is None or not self._shutdown.is_set():
            try:
                response = self._sqs.receive_message(
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=20,
                )
            except ClientError:
                logger.error("SQS receive_message failed; will retry", exc_info=True)
                time.sleep(5)
                continue

            messages = response.get("Messages", [])
            for message in messages:
                receipt_handle = message["ReceiptHandle"]
                try:
                    self._process_message(message)
                    # Delete on success
                    self._sqs.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=receipt_handle,
                    )
                except Exception:
                    # TODO: add dead-letter queue routing for poison messages.
                    logger.error(
                        "Failed to process SQS message %s; leaving in queue for DLQ",
                        message.get("MessageId"),
                        exc_info=True,
                    )
        logger.info("SQSConsumer long-poll loop stopped")

    def _process_message(self, message: dict[str, Any]) -> None:
        """Parse message body (JSON), extract the event from EventBridge envelope, call handler."""
        raw_body = message.get("Body", "{}")
        body: dict[str, Any] = json.loads(raw_body)
        # EventBridge events arrive wrapped in an SNS notification when the bus
        # targets an SQS queue via an SNS subscription, or directly when the bus
        # rule targets SQS.  Handle both shapes.
        if "Message" in body:
            # SNS-wrapped EventBridge event
            inner: dict[str, Any] = json.loads(body["Message"])
        else:
            inner = body
        self._handler.handle_event(inner)


# ---------------------------------------------------------------------------
# Hub application
# ---------------------------------------------------------------------------


def _fetch_secret(secret_name: str) -> str:
    """Retrieve a plaintext secret from AWS Secrets Manager."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    secret: str = response["SecretString"]
    return secret


# ---------------------------------------------------------------------------
# Ignore-rule helpers (module-level so they're testable without a full HubApp)
# ---------------------------------------------------------------------------


def _seed_ignore_rules(
    store: DynamoIgnoreRuleStore,
    config: Any | None,
) -> tuple[list[Any], bool]:
    """Seed ignore rules from config into DynamoDB on first boot.

    Returns ``(baseline_rules, seeded)`` where *seeded* is True when the store
    was empty and rules were written, False when the store already had data
    (DB wins — no overwrite).  The baseline list is always the rules from
    config (empty if no ignore config block) regardless of whether seeding
    happened; it is used for deviation detection at runtime.
    """
    from relay.config.schema import IgnoreConfig  # local to avoid circulars

    # Collect config's ignore rules for the baseline (empty is fine).
    baseline: list[Any] = []
    if (
        config is not None
        and getattr(config, "routing", None) is not None
        and getattr(config.routing, "ignore", None) is not None
        and isinstance(config.routing.ignore, IgnoreConfig)
    ):
        baseline = list(config.routing.ignore.rules)

    # DB already has rules → skip seeding (runtime truth wins).
    existing = store.list_rules()
    if existing:
        return baseline, False

    # Store is empty — seed from config.
    for rule in baseline:
        store.put_rule(rule)
    return baseline, bool(baseline)


def _seed_routing_rules(
    store: DynamoRoutingRuleStore,
    config: Any | None,
) -> tuple[list[Any], bool]:
    """Seed routing rules from config into DynamoDB on first boot.

    Returns ``(baseline_rules, seeded)`` where *seeded* is True when the store
    was empty and rules were written, False when the store already had data
    (DB wins — no overwrite).  The baseline list is always the rules from
    config (empty if no routing config block) regardless of whether seeding
    happened; it is used for deviation detection at runtime.
    """
    # Collect config's routing rules for the baseline (empty is fine).
    baseline: list[Any] = []
    if (
        config is not None
        and getattr(config, "routing", None) is not None
        and getattr(config.routing, "rules", None) is not None
    ):
        baseline = list(config.routing.rules)

    # DB already has rules → skip seeding (runtime truth wins).
    existing = store.list_rules()
    if existing:
        return baseline, False

    # Store is empty — seed from config.
    for rule in baseline:
        store.put_rule(rule, rule_id=rule.rule_id)
    return baseline, bool(baseline)


def _emit_rule_change(
    action: str,
    rule_id: str,
    *,
    actor: str | None = None,
    rule: Any | None = None,
) -> None:
    """Emit a structured log line for a rule-change event.

    Provides a single, named seam that log-based consumers (and future adapters)
    can subscribe to.  The stable marker ``relay.rule-change`` in every line lets
    a CloudWatch Logs filter or GitLab adapter detect changes without parsing
    free-form text.

    Args:
        action:  One of "created", "updated", "deleted", "ignored".
        rule_id: The affected rule's ID.
        actor:   Identity subject that performed the action (may be None).
        rule:    The :class:`~relay.config.schema.IgnoreRule` being acted on
                 (may be None for deletes).

    # future: GitLab adapter consumes rule-change events to auto-PR routing.yaml
    """
    logger.info(
        "relay.rule-change action=%s rule_id=%s actor=%s",
        action,
        rule_id,
        actor,
    )


class HubApp:
    """Fargate long-running process: SQS consumer + optional FastAPI server."""

    def __init__(self) -> None:
        # --- Read environment variables ---
        # Required for any Hub: the fleet table. Everything else is optional so a
        # standalone/local Hub (no ServiceNow/GitLab, no SQS yet) still boots and
        # serves the dashboard. Integrations activate only when configured.
        # Accept both RELAY_FLEET_TABLE_NAME (set by hub_stack) and the older
        # RELAY_DYNAMO_INCIDENTS_TABLE for compatibility.
        incidents_table: str = (
            os.environ.get("RELAY_FLEET_TABLE_NAME")
            or os.environ.get("RELAY_DYNAMO_INCIDENTS_TABLE")
            or "relay-hub-fleet"
        )
        queue_url: str = os.environ.get("RELAY_SQS_QUEUE_URL", "").strip()
        sns_topic_arn: str = os.environ.get(
            "RELAY_CENTRAL_PAGING_TOPIC_ARN",
            os.environ.get("RELAY_SNS_TOPIC_ARN", ""),
        ).strip()
        # Secret fetcher injected into adapter from_env() — keeps boto3 out of
        # the adapter modules. Returns "" (integration disabled) on any failure.
        def _safe_secret(name: str) -> str:
            if not name:
                return ""
            try:
                return _fetch_secret(name)
            except Exception:
                logger.warning("Could not fetch secret %r; integration disabled", name)
                return ""

        # --- Config (escalation policies + routing rules + federation policy) ---
        # Loaded before the forwarder so the federation gate can be config-driven
        # (routing.yaml `federation:` block) rather than env-var-only. Best-effort:
        # a local Hub may run without a config source. On-call resolution is
        # schedule-backed (DynamoDB), independent of this config.
        from relay.config.schema import FederationConfig
        self._config = _load_hub_config()
        _fed_from_config = (
            self._config.routing.federation
            if self._config is not None and self._config.routing is not None
            else None
        )
        # The gate is always a real FederationConfig: the routing.yaml block when
        # present, else model defaults (min_severity SEV2, all states, no overrides).
        federation = _fed_from_config if _fed_from_config is not None else FederationConfig()

        # --- Hub scope + forwarder ---
        # The forwarding gate is config-driven (routing.yaml `federation:` block,
        # resolved into `federation` above, or FederationConfig() defaults). There
        # is no env-var gate.
        scope = HubScope.from_env()
        central_bus_arn = os.environ.get("RELAY_CENTRAL_HUB_BUS_ARN", "").strip()

        from relay.adapters.aws.eventbridge_forwarder import (
            EventBridgeForwarder,
            NoOpForwarder,
        )
        forwarder: EventBridgeForwarder | NoOpForwarder
        if scope == HubScope.LOCAL_FEDERATED and central_bus_arn:
            # Get this account's ID for the forwarded_from marker.
            try:
                sts = boto3.client("sts")
                source_account_id = sts.get_caller_identity()["Account"]
            except Exception:
                logger.warning("Could not retrieve account ID via STS; using empty string")
                source_account_id = ""
            forwarder = EventBridgeForwarder(
                central_bus_arn=central_bus_arn,
                source_account_id=source_account_id,
                hub_scope=scope.value,
            )
            logger.info(
                "HubApp: scope=%s; forwarding to %s (min_severity=%s, source=%s)",
                scope,
                central_bus_arn,
                federation.min_severity,
                "routing.yaml federation" if self._config is not None else "defaults",
            )
        else:
            forwarder = NoOpForwarder()
            logger.info("HubApp: scope=%s; forwarding disabled (NoOpForwarder)", scope)

        # --- Instantiate stores and adapters ---
        incident_store = DynamoIncidentStore(incidents_table)
        notifier = SNSNotifier(topic_arn=sns_topic_arn or None)
        # Keep notifier + paging topic on the app for the UI test-page endpoint.
        self._notifier = notifier
        self._paging_topic_arn = sns_topic_arn or None
        # Per-Hub settings (e.g. Teams webhook URL), editable via the UI.
        self._settings_store = DynamoSettingsStore(incidents_table)
        # On-call availability + generated schedules.
        self._schedule_store = DynamoScheduleStore(incidents_table)
        # Ignore rules — DB is runtime truth; config seeds on first boot only.
        self._ignore_rule_store: DynamoIgnoreRuleStore | None = None
        self._ignore_baseline: list[Any] = []
        try:
            self._ignore_rule_store = DynamoIgnoreRuleStore(incidents_table)
            self._ignore_baseline, _seeded = _seed_ignore_rules(
                self._ignore_rule_store, self._config
            )
            if _seeded:
                logger.info(
                    "HubApp: seeded %d ignore rules from config into DynamoDB",
                    len(self._ignore_baseline),
                )
            else:
                logger.info(
                    "HubApp: DynamoDB already has ignore rules — skipping seed "
                    "(baseline=%d rules from config)",
                    len(self._ignore_baseline),
                )
        except Exception:
            logger.warning(
                "HubApp: failed to initialise DynamoIgnoreRuleStore — "
                "ignore-rule endpoints will return 503",
                exc_info=True,
            )

        # Routing rules — DB is runtime truth; config seeds on first boot only.
        self._routing_rule_store: DynamoRoutingRuleStore | None = None
        self._routing_baseline: list[Any] = []
        try:
            self._routing_rule_store = DynamoRoutingRuleStore(incidents_table)
            self._routing_baseline, _rt_seeded = _seed_routing_rules(
                self._routing_rule_store, self._config
            )
            if _rt_seeded:
                logger.info(
                    "HubApp: seeded %d routing rules from config into DynamoDB",
                    len(self._routing_baseline),
                )
            else:
                logger.info(
                    "HubApp: DynamoDB already has routing rules — skipping seed "
                    "(baseline=%d rules from config)",
                    len(self._routing_baseline),
                )
        except Exception:
            logger.warning(
                "HubApp: failed to initialise DynamoRoutingRuleStore — "
                "routing-rule endpoints will return 503",
                exc_info=True,
            )

        # Integration adapters (ServiceNow/GitLab/Teams) are no longer wired
        # here — the adapter registry discovers each package's MANIFEST and
        # builds its listener from the AdapterContext (HubProcessor below). The
        # Hub injects only a secret fetcher so the adapter modules carry no cloud
        # SDK dependency.

        # --- Fleet store + hub state ---
        fleet_store = FleetStore(table_name=incidents_table)
        # Keep stores on the app so the UI endpoints can read/write incident
        # records, contacts, and resolve on-call.
        self._incident_store = incident_store
        self._contact_store = DynamoContactStore(incidents_table)
        # self._config was loaded earlier (before the forwarder) so the
        # federation gate can be config-driven. Used here for the On-Call view.
        self._hub_state = HubState(fleet_store=fleet_store)
        self._shutdown = threading.Event()
        self._sse_publisher = SSEPublisher()
        self._processor = HubProcessor(
            incident_store=incident_store,
            notifier=notifier,
            hub_state=self._hub_state,
            sse_publisher=self._sse_publisher,
            forwarder=forwarder,
            federation=federation,
            settings_store=self._settings_store,
            dashboard_url=os.environ.get("RELAY_DASHBOARD_URL", ""),
            secret_fetcher=_safe_secret,
        )
        self._queue_url = queue_url
        self._consumer = SQSConsumer(
            queue_url=queue_url,
            handler=self._processor,
            shutdown_event=self._shutdown,
        )

        # RELAY_RUNTIME: "fargate" (default) | "local-aws" | "local-mock".
        # Controls whether the /ingest/alarm endpoint is open (see build_fastapi_app).
        self._runtime: str = os.environ.get("RELAY_RUNTIME", "fargate").strip().lower()

        # --- Detection pipeline (in-process Node+Hub composition) ---
        # Build a NodeHandler wired to the HubProcessor's on_local_incident sink.
        # This is what makes an injected alarm turn a tile red locally in the
        # always-on container without any EventBridge round-trip (§8 no-drift seam).
        # Defensive: a Hub missing Node env vars (e.g. SNS topic, table name) must
        # still boot and serve the dashboard; only the ingest endpoint is degraded.
        #
        # Escalation timers use the DynamoDB-deadline store swept by SweepTimer
        # below (collapsed-single-container plan §3 / Step 2) instead of EventBridge
        # Scheduler. We build the timer + engine here and inject them so both the
        # alarm path (start → write deadline) and the sweep (fire → on_timeout)
        # share one deadline store on the Hub's table.
        self._pipeline = None
        deadline_sweeper: DeadlineSweeper | None = None
        try:
            from relay.adapters.aws.dynamo_stores import (
                DynamoDeadlineTimer,
                DynamoEscalationStateStore,
            )
            from relay.core.escalation import EscalationEngine
            from relay.node.handler import NodeHandler
            from relay.node.pipeline import DetectionPipeline

            deadline_timer = DynamoDeadlineTimer(incidents_table)
            esc_state_store = DynamoEscalationStateStore(incidents_table)
            esc_engine = EscalationEngine(
                timer=deadline_timer, state_store=esc_state_store
            )
            node_handler = NodeHandler(
                _incident_store=incident_store,
                _escalation_state_store=esc_state_store,
                _escalation_engine=esc_engine,
                _on_incident=self._processor.on_local_incident,
                _ignore_rule_store=self._ignore_rule_store,
                _routing_rule_store=self._routing_rule_store,
            )
            self._pipeline = DetectionPipeline(node_handler)
            # Step 3: the SQS consumer routes raw CloudWatch alarms here too.
            self._processor.set_pipeline(self._pipeline)
            deadline_sweeper = DeadlineSweeper(
                timer=deadline_timer, fire=self._pipeline.handle_timeout
            )
            logger.info(
                "HubApp: DetectionPipeline wired (in-process Node+Hub, "
                "DynamoDB-deadline escalation timers)"
            )
        except Exception:
            logger.warning(
                "HubApp: could not build DetectionPipeline — "
                "/ingest/alarm will return 503; escalation auto-advance disabled; "
                "dashboard unaffected",
                exc_info=True,
            )

        self._sweep_timer = SweepTimer(
            hub_state=self._hub_state,
            sse_publisher=self._sse_publisher,
            shutdown_event=self._shutdown,
            deadline_sweeper=deadline_sweeper,
        )

        logger.info("HubApp initialised; queue=%s runtime=%s", queue_url, self._runtime)

    def request_shutdown(self) -> None:
        """Signal the background loops (sweep, SQS consumer) to stop.

        Wired to the SIGTERM handler in ``main()`` so a Fargate task roll drains
        cleanly instead of being killed mid-message.
        """
        self._shutdown.set()

    def start(self) -> None:
        """Start SQS consumer + sweep timer as daemon threads, then run the HTTP server."""
        # Hydrate in-memory cache from DynamoDB.
        try:
            self._hub_state.hydrate()
        except Exception:
            logger.warning("HubState.hydrate() failed; starting with empty cache", exc_info=True)

        # Only run the SQS consumer when a queue is actually configured. The
        # collapsed local runtimes (RELAY_RUNTIME=local-aws/local-mock) take
        # alarms via POST /ingest/alarm and have no ingest queue, so starting a
        # consumer against an empty URL would crash-loop on QueueDoesNotExist.
        if self._queue_url:
            consumer_thread = threading.Thread(
                target=self._consumer.run_forever,
                name="sqs-consumer",
                daemon=True,
            )
            consumer_thread.start()
            logger.info("SQS consumer thread started")
        else:
            logger.info(
                "No RELAY_SQS_QUEUE_URL configured; SQS consumer not started "
                "(runtime=%s — alarms arrive via POST /ingest/alarm)",
                self._runtime,
            )

        self._sweep_timer.start()
        logger.info("Sweep timer thread started")

        if _HAS_FASTAPI:
            app = self.build_fastapi_app()
            uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
        else:
            logger.warning(
                "fastapi/uvicorn not installed; running heartbeat loop only. "
                "Install with: pip install fastapi uvicorn"
            )
            while not self._shutdown.is_set():
                time.sleep(60)
                logger.info("Hub alive (no HTTP server; install fastapi+uvicorn)")

    def build_fastapi_app(self) -> FastAPI:
        """Build and return the FastAPI application. Does not start uvicorn."""
        app = FastAPI(title="Relay Hub", version="0.2.0")

        # Serve the dashboard's ES modules as read-only static files. The browser
        # loads them directly via <script type="module"> — no build step, no
        # bundler, no CDN; the wheel ships them under dashboard_modules/.
        if _DASHBOARD_MODULES_DIR.is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount(
                "/static/dashboard",
                StaticFiles(directory=str(_DASHBOARD_MODULES_DIR)),
                name="dashboard-modules",
            )

        hub_state = self._hub_state
        sse_publisher = self._sse_publisher
        incident_store = getattr(self, "_incident_store", None)
        contact_store = getattr(self, "_contact_store", None)
        notifier = getattr(self, "_notifier", None)
        paging_topic_arn = getattr(self, "_paging_topic_arn", None)
        settings_store = getattr(self, "_settings_store", None)
        schedule_store = getattr(self, "_schedule_store", None)
        ignore_rule_store = getattr(self, "_ignore_rule_store", None)
        ignore_baseline = getattr(self, "_ignore_baseline", [])
        routing_rule_store = getattr(self, "_routing_rule_store", None)
        routing_baseline = getattr(self, "_routing_baseline", [])
        hub_config = getattr(self, "_config", None)
        # Capture pipeline + runtime for the /ingest/alarm route closure.
        pipeline = getattr(self, "_pipeline", None)
        runtime = getattr(self, "_runtime", "fargate")

        # ----------------------------------------------------------------
        # GET / — self-contained dashboard HTML
        # ----------------------------------------------------------------
        @app.get("/", response_class=HTMLResponse)
        def dashboard() -> HTMLResponse:
            try:
                html = _render_dashboard_html()
            except FileNotFoundError:
                html = "<html><body><h1>Dashboard HTML missing</h1></body></html>"
            return HTMLResponse(content=html)

        # ----------------------------------------------------------------
        # GET /stream — SSE endpoint
        # ----------------------------------------------------------------
        @app.get("/stream")
        def stream() -> StreamingResponse:
            """SSE stream: full fleet snapshot on connect, then tile-change deltas.

            Also sends a comment ping (~15s) for browser-side freeze detection.
            The sweep timer publishes pings and deltas via SSEPublisher.
            """
            q = sse_publisher.subscribe()

            # Build initial snapshot event.
            tiles = hub_state.get_tiles()
            snapshot_data = json.dumps([t.to_dict() for t in tiles])
            snapshot_msg = f"event: snapshot\ndata: {snapshot_data}\n\n"

            def event_generator() -> Iterator[str]:
                try:
                    yield snapshot_msg
                    while True:
                        try:
                            msg = q.get(timeout=1.0)
                            yield msg
                        except queue.Empty:
                            continue
                finally:
                    sse_publisher.unsubscribe(q)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        # ----------------------------------------------------------------
        # GET /health
        # ----------------------------------------------------------------
        @app.get("/health")
        def health() -> dict[str, Any]:
            fleet_size = len(hub_state.fleet)
            return {"status": "ok", "role": "hub", "fleet_size": fleet_size}

        # ----------------------------------------------------------------
        # GET /fleet — full snapshot JSON
        # ----------------------------------------------------------------
        @app.get("/fleet")
        def list_fleet() -> list[dict[str, Any]]:
            tiles = hub_state.get_tiles()
            return [t.to_dict() for t in tiles]

        # ----------------------------------------------------------------
        # GET /fleet/rollup — rollup tree (requires org_tree)
        # ----------------------------------------------------------------
        @app.get("/fleet/rollup")
        def fleet_rollup() -> Any:
            # The org tree is built dynamically from node registrations
            # (heartbeat org_path) — the Hub stores no static catalog. An empty
            # tree (no registrations yet) yields an empty rollup, not a 404.
            org_tree = hub_state.get_org_tree()
            if org_tree is None or not org_tree.all_nodes():
                return []
            from relay.hub.health import compute_rollup
            tiles = hub_state.get_tiles()
            return compute_rollup(tiles, org_tree)

        # ----------------------------------------------------------------
        # GET /fleet/{account_id}/{app_name}
        # ----------------------------------------------------------------
        @app.get("/fleet/{account_id}/{app_name}")
        def get_fleet_tile(account_id: str, app_name: str) -> dict[str, Any]:
            tile = hub_state.get_tile(account_id, app_name)
            if tile is None:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=404,
                    detail=f"No fleet tile found for account_id={account_id!r} app_name={app_name!r}",
                )
            payload = tile.to_dict()
            # On-call resolution stays the Hub's authority. On a team Hub the
            # local schedule store can answer live, which is always current and
            # correct for the (single) team — prefer it. On a federated Hub there
            # is no schedule for a remote app, so fall back to the on_call
            # snapshot the owning team pushed up its heartbeat. Identical shape
            # either way, so the drawer never forks on topology.
            if schedule_store is not None:
                try:
                    names: dict[str, str] = {}
                    if contact_store is not None:
                        names = {c.contact_id: c.name for c in contact_store.list_contacts()}
                    live = _resolve_now_on_call(schedule_store, datetime.now(UTC), names)
                    if live is not None:
                        payload["on_call"] = live
                except Exception:
                    logger.warning("live on-call resolution for tile failed", exc_info=True)
            return payload

        # ----------------------------------------------------------------
        # GET /incidents — open incidents (read-only)
        # Optional ?account_id= filter. Newest first.
        # ----------------------------------------------------------------
        @app.get("/incidents")
        def list_incidents(account_id: str | None = None) -> list[dict[str, Any]]:
            if incident_store is None:
                return []
            try:
                incidents = incident_store.list_open_incidents(account_id=account_id)
            except Exception:
                logger.warning("list_open_incidents failed", exc_info=True)
                return []
            incidents.sort(key=lambda i: i.created_at, reverse=True)
            # Compact summaries for the list view; full detail via /incidents/{id}.
            return [
                {
                    "correlation_id": i.correlation_id,
                    "app_name": i.app_name,
                    "account_id": i.account_id,
                    "environment": i.environment,
                    "deployment_id": i.deployment_id,
                    "severity": i.severity,
                    "state": i.state,
                    "alarm_name": i.alarm_name,
                    "created_at": i.created_at.isoformat() if i.created_at else None,
                    "updated_at": i.updated_at.isoformat() if i.updated_at else None,
                    "acknowledged_by": i.acknowledged_by,
                }
                for i in incidents
            ]

        # ----------------------------------------------------------------
        # GET /incidents/history — terminal-state incidents only (RESOLVED,
        # CLOSED). Open incidents (TRIGGERED/ACKNOWLEDGED/ESCALATED) live on the
        # Open tab and are excluded here. MUST be declared before
        # /incidents/{correlation_id} so "history" isn't matched as a correlation_id.
        # ----------------------------------------------------------------
        @app.get("/incidents/history")
        def incidents_history() -> list[dict[str, Any]]:
            if incident_store is None or not hasattr(incident_store, "list_incidents"):
                return []
            try:
                incidents = incident_store.list_incidents()
            except Exception:
                logger.warning("list_incidents failed", exc_info=True)
                return []
            terminal = {IncidentState.RESOLVED, IncidentState.CLOSED}
            incidents = [i for i in incidents if i.state in terminal]
            incidents.sort(key=lambda i: i.created_at, reverse=True)
            return [
                {
                    "correlation_id": i.correlation_id,
                    "app_name": i.app_name,
                    "environment": i.environment,
                    "severity": i.severity,
                    "state": i.state,
                    "alarm_name": i.alarm_name,
                    "created_at": i.created_at.isoformat() if i.created_at else None,
                    "acknowledged_by": i.acknowledged_by,
                }
                for i in incidents
            ]

        # ----------------------------------------------------------------
        # GET /metrics — incident KPIs (counts, MTTR, time-to-ack). Read-only.
        # Declared BEFORE /incidents/{id} so the literal path isn't shadowed.
        # ----------------------------------------------------------------
        @app.get("/metrics")
        def incident_metrics() -> dict[str, Any]:
            from relay.core.metrics import compute_metrics
            if incident_store is None or not hasattr(incident_store, "list_incidents"):
                return compute_metrics([]).as_dict()
            try:
                incidents = incident_store.list_incidents()
            except Exception:
                logger.warning("list_incidents failed for /metrics", exc_info=True)
                incidents = []
            return compute_metrics(incidents).as_dict()

        # ----------------------------------------------------------------
        # GET /incidents/{correlation_id} — full incident + timeline (read-only)
        # ----------------------------------------------------------------
        @app.get("/incidents/{correlation_id}")
        def get_incident(correlation_id: str) -> dict[str, Any]:
            from fastapi import HTTPException
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            try:
                incident = incident_store.get_incident(correlation_id)
            except Exception:
                logger.warning("get_incident failed for %s", correlation_id, exc_info=True)
                incident = None
            if incident is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No incident found for correlation_id={correlation_id!r}",
                )
            dumped: dict[str, Any] = incident.model_dump(mode="json")
            return dumped

        # ----------------------------------------------------------------
        # GET /incidents/{id}/brief and /aar — AI-augmented drafts (read-only).
        # Both degrade gracefully: deterministic output when no model wired.
        # ----------------------------------------------------------------
        def _ai_assistant() -> Any:
            # The factory selects the provider (RELAY_AI_PROVIDER) and honors the
            # RELAY_AI_ENABLED opt-in; None -> deterministic fallback (AI
            # augments, never gates).
            try:
                from relay.adapters.ai import make_assistant
                return make_assistant()
            except Exception:
                logger.warning("AI assistant init failed; using fallback", exc_info=True)
                return None

        def _incident_or_404(correlation_id: str) -> Incident:
            from fastapi import HTTPException
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            raw_incident = incident_store.get_incident(correlation_id)
            if raw_incident is None:
                raise HTTPException(status_code=404, detail="incident not found")
            fetched: Incident = raw_incident
            return fetched

        @app.get("/incidents/{correlation_id}/brief")
        def incident_brief(correlation_id: str) -> dict[str, Any]:
            from relay.core.analysis import generate_brief
            incident = _incident_or_404(correlation_id)
            return generate_brief(incident, _ai_assistant())

        @app.get("/incidents/{correlation_id}/aar")
        def incident_aar(correlation_id: str) -> dict[str, Any]:
            from relay.core.analysis import generate_aar
            incident = _incident_or_404(correlation_id)
            return generate_aar(incident, _ai_assistant())

        # ----------------------------------------------------------------
        # GET /auth — tell the UI the auth mode + current identity (if any)
        # ----------------------------------------------------------------
        from relay.hub import auth as _auth

        @app.get("/auth")
        def auth_info(request: Request) -> dict[str, Any]:
            headers = dict(request.headers)
            ident = _auth.identfrom_headers(headers)
            return {
                "mode": _auth.auth_mode(),
                # can_write honors the fine-grained access-control allowlist, not
                # just "is there an identity" — an authenticated user who isn't on
                # the write allowlist sees can_write=false (matches require_writer).
                "can_write": _auth.can_write(headers),
                "subject": ident.subject if ident else None,
                # Team wall-clock zone (RELAY_TZ) so the UI renders the schedule
                # grid and "now" highlight in the same zone the server resolves.
                "timezone": os.environ.get("RELAY_TZ", "UTC").strip() or "UTC",
                # Deployment scope so the UI can label which kind of Hub this is.
                # Per the two-topology model this collapses to a binary in the UI:
                # 'central' is the org-wide aggregator (Central Hub); 'local' and
                # 'local-federated' both serve a single team (Team Hub).
                "hub_scope": HubScope.from_env().value,
            }

        # ----------------------------------------------------------------
        # POST /incidents/{id}/acknowledge — write (requires authenticated writer)
        # ----------------------------------------------------------------
        @app.post("/incidents/{correlation_id}/acknowledge")
        def acknowledge_incident(correlation_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            ident = _auth.require_writer(dict(request.headers))
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            incident = incident_store.get_incident(correlation_id)
            if incident is None:
                raise HTTPException(status_code=404, detail="incident not found")
            if incident.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
                raise HTTPException(status_code=409, detail=f"incident is {incident.state}")
            now = datetime.now(UTC)
            incident.state = IncidentState.ACKNOWLEDGED
            incident.acknowledged_by = ident.subject
            incident.acknowledged_at = now
            incident.updated_at = now
            incident.timeline.append(
                TimelineEvent(
                    event_id=f"ack-{int(now.timestamp())}",
                    incident_id=correlation_id,
                    stream=Stream.CENTRAL,
                    occurred_at=now,
                    actor=ident.subject,
                    event_type="acknowledged",
                    detail={"via": "hub-ui", "source": ident.source},
                )
            )
            incident_store.put_incident(incident)
            # Fan ACKNOWLEDGED out to lifecycle listeners (e.g. update the Teams
            # card, annotate the external ticket). Failure-isolated; never blocks
            # the ack from succeeding in Relay.
            processor = getattr(self, "_processor", None)
            if processor is not None:
                try:
                    processor.dispatch_event(
                        IncidentLifecycleEvent.ACKNOWLEDGED, incident
                    )
                except Exception:
                    logger.warning(
                        "ACKNOWLEDGED dispatch failed for %s", correlation_id, exc_info=True
                    )
            logger.info("Incident %s acknowledged by %s via UI", correlation_id, ident.subject)
            return {"ok": True, "state": incident.state, "acknowledged_by": ident.subject}

        # ----------------------------------------------------------------
        # Contacts — list/create/update/delete (writes require a writer)
        # ----------------------------------------------------------------
        @app.get("/contacts")
        def list_contacts() -> list[dict[str, Any]]:
            if contact_store is None:
                return []
            try:
                return [c.model_dump(mode="json") for c in contact_store.list_contacts()]
            except Exception:
                logger.warning("list_contacts failed", exc_info=True)
                return []

        @app.post("/contacts")
        def upsert_contact(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if contact_store is None:
                raise HTTPException(status_code=503, detail="contact store unavailable")
            try:
                contact = Contact.model_validate(payload)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"invalid contact: {exc}")
            contact_store.put_contact(contact)
            return {"ok": True, "contact_id": contact.contact_id}

        @app.delete("/contacts/{contact_id}")
        def delete_contact(contact_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if contact_store is None:
                raise HTTPException(status_code=503, detail="contact store unavailable")
            contact_store.delete_contact(contact_id)
            return {"ok": True, "deleted": contact_id}

        # ----------------------------------------------------------------
        # GET /oncall — who's on call now per schedule (read-only)
        # ----------------------------------------------------------------
        @app.get("/oncall")
        def oncall() -> dict[str, Any]:
            now = datetime.now(UTC)
            # Build a contact_id -> name lookup if we can.
            names: dict[str, str] = {}
            if contact_store is not None:
                try:
                    names = {c.contact_id: c.name for c in contact_store.list_contacts()}
                except Exception:
                    names = {}

            now_on_call = _resolve_now_on_call(schedule_store, now, names)
            return {"now_on_call": now_on_call}

        # ----------------------------------------------------------------
        # POST /incidents/{id}/resolve — write (writer-gated)
        # ----------------------------------------------------------------
        @app.post("/incidents/{correlation_id}/resolve")
        def resolve_incident(correlation_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            ident = _auth.require_writer(dict(request.headers))
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            incident = incident_store.get_incident(correlation_id)
            if incident is None:
                raise HTTPException(status_code=404, detail="incident not found")
            now = datetime.now(UTC)
            incident.state = IncidentState.RESOLVED
            incident.updated_at = now
            incident.timeline.append(
                TimelineEvent(
                    event_id=f"res-{int(now.timestamp())}",
                    incident_id=correlation_id,
                    stream=Stream.CENTRAL,
                    occurred_at=now,
                    actor=ident.subject,
                    event_type="resolved",
                    detail={"via": "hub-ui", "note": "Relay incident resolved (does not "
                            "clear the underlying CloudWatch alarm)"},
                )
            )
            incident_store.put_incident(incident)
            # Fan RESOLVED out to lifecycle listeners so external tickets close
            # (GitLab issue, ServiceNow record). Failure-isolated; never blocks
            # the resolve from succeeding in Relay.
            processor = getattr(self, "_processor", None)
            if processor is not None:
                try:
                    processor.dispatch_event(IncidentLifecycleEvent.RESOLVED, incident)
                except Exception:
                    logger.warning(
                        "RESOLVED dispatch failed for %s", correlation_id, exc_info=True
                    )
            logger.info("Incident %s resolved by %s via UI", correlation_id, ident.subject)
            return {"ok": True, "state": incident.state}

        # ----------------------------------------------------------------
        # POST /contacts/{id}/test — send a test page (writer-gated)
        # ----------------------------------------------------------------
        @app.post("/contacts/{contact_id}/test")
        def test_page(contact_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if contact_store is None or notifier is None:
                raise HTTPException(status_code=503, detail="contacts/notifier unavailable")
            contact = contact_store.get_contact(contact_id)
            if contact is None:
                raise HTTPException(status_code=404, detail="contact not found")
            msg = (
                f"Relay TEST PAGE for {contact.name} — this is a test, no action needed. "
                "If you received this, your paging channel works."
            )
            try:
                result = notifier.publish_test(
                    phone=contact.phone,
                    email_topic_arn=paging_topic_arn,
                    message=msg,
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"test page failed: {exc}")
            return {"ok": any(result.values()), "channels": result}

        # ----------------------------------------------------------------
        # Settings — per-Hub config (Teams webhook URL). Read shows whether a
        # webhook is set (masked); writes are writer-gated.
        # ----------------------------------------------------------------
        @app.get("/settings")
        def get_settings() -> dict[str, Any]:
            if settings_store is None:
                return {
                    "teams_webhook_configured": False,
                    "gitlab_token_configured": False,
                    "servicenow_configured": False,
                }
            try:
                hook = settings_store.get(SettingsKey.TEAMS_WEBHOOK_URL) or ""
            except Exception:
                hook = ""
            try:
                gitlab_token = settings_store.get(SettingsKey.GITLAB_TOKEN) or ""
            except Exception:
                gitlab_token = ""
            try:
                sn_instance = settings_store.get(SettingsKey.SERVICENOW_INSTANCE_URL) or ""
                sn_username = settings_store.get(SettingsKey.SERVICENOW_USERNAME) or ""
                sn_password = settings_store.get(SettingsKey.SERVICENOW_PASSWORD) or ""
            except Exception:
                sn_instance = sn_username = sn_password = ""
            # Mask: never return the full URL.
            masked = ""
            if hook:
                masked = hook[:30] + "…" if len(hook) > 30 else hook
            # Mask the token: show only a short suffix so a writer can tell which
            # token is set without exposing it.
            token_masked = ""
            if gitlab_token:
                token_masked = "…" + gitlab_token[-4:] if len(gitlab_token) > 4 else "…"
            # ServiceNow is "configured" when the instance URL + password are set
            # (username may be embedded in some auth setups). Echo the instance
            # URL + username (not secrets) and a masked password suffix.
            sn_configured = bool(sn_instance and sn_password)
            sn_password_masked = ""
            if sn_password:
                sn_password_masked = (
                    "…" + sn_password[-4:] if len(sn_password) > 4 else "…"
                )
            return {
                "teams_webhook_configured": bool(hook),
                "teams_webhook_masked": masked,
                "gitlab_token_configured": bool(gitlab_token),
                "gitlab_token_masked": token_masked,
                "servicenow_configured": sn_configured,
                "servicenow_instance_url": sn_instance,
                "servicenow_username": sn_username,
                "servicenow_password_masked": sn_password_masked,
            }

        @app.get("/config")
        def get_config() -> dict[str, Any]:
            """Non-sensitive runtime config + build provenance for the UI.

            Reports only flags/locations and presence-of-secret booleans — never
            secret values, ARNs of sensitive resources, or credentials.
            """
            from relay import __version__

            def _present(name: str) -> bool:
                return bool(os.environ.get(name))

            teams_configured = False
            gitlab_token_configured = False
            if settings_store is not None:
                try:
                    teams_configured = bool(settings_store.get(SettingsKey.TEAMS_WEBHOOK_URL))
                except Exception:
                    teams_configured = False
                try:
                    gitlab_token_configured = bool(settings_store.get(SettingsKey.GITLAB_TOKEN))
                except Exception:
                    gitlab_token_configured = False

            scope = os.environ.get("RELAY_HUB_SCOPE", "local")
            return {
                "build": {
                    "version": __version__,
                    "git_sha": os.environ.get("RELAY_BUILD_SHA", "unknown"),
                    "built_at": os.environ.get("RELAY_BUILD_TIME", "unknown"),
                },
                "runtime": {
                    "role": os.environ.get("RELAY_ROLE", "hub"),
                    "hub_scope": scope,
                    "scaling": os.environ.get("RELAY_HUB_SCALING", "always"),
                    "region": os.environ.get("AWS_REGION", ""),
                    "timezone": os.environ.get("RELAY_TZ", "UTC").strip() or "UTC",
                    "auth_mode": _auth.auth_mode(),
                    "config_source": os.environ.get(
                        "RELAY_CONFIG_SOURCE",
                        "local" if os.environ.get("RELAY_CONFIG_DIR") else "none",
                    ),
                    "log_level": os.environ.get("LOG_LEVEL", "WARNING"),
                },
                "features": {
                    "ai_enabled": os.environ.get("RELAY_AI_ENABLED", "").lower() == "true",
                    "ai_provider": (
                        (os.environ.get("RELAY_AI_PROVIDER", "").strip().lower() or "bedrock")
                        if os.environ.get("RELAY_AI_ENABLED", "").lower() == "true"
                        else ""
                    ),
                    "ai_model": (
                        os.environ.get("RELAY_AI_MODEL_ID", "")
                        if os.environ.get("RELAY_AI_ENABLED", "").lower() == "true"
                        else ""
                    ),
                    "teams_webhook_configured": teams_configured,
                    "servicenow_configured": _present("RELAY_SERVICENOW_INSTANCE_URL"),
                    "gitlab_configured": _present("RELAY_GITLAB_REPO")
                    or _present("RELAY_GITLAB_PROJECT_ID")
                    or gitlab_token_configured,
                    "forwarding": scope == "local-federated",
                    "integrations_locked": os.environ.get("RELAY_INTEGRATIONS_LOCKED", "").lower() == "true",
                },
            }

        @app.put("/settings/teams-webhook")
        def set_teams_webhook(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            url = (payload.get("webhook_url") or "").strip()
            if url and not url.lower().startswith("https://"):
                raise HTTPException(status_code=422, detail="webhook_url must be https")
            if url:
                settings_store.set(SettingsKey.TEAMS_WEBHOOK_URL, url)
            else:
                settings_store.delete(SettingsKey.TEAMS_WEBHOOK_URL)
            return {"ok": True, "configured": bool(url)}

        @app.post("/settings/teams-webhook/test")
        def test_teams_webhook(request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            hook = settings_store.get(SettingsKey.TEAMS_WEBHOOK_URL)
            if not hook:
                raise HTTPException(status_code=404, detail="no webhook configured")
            # Teams MessageCard knowledge lives in the adapter; endpoint delegates.
            from relay.adapters.integrations.teams import TeamsWebhookNotifier

            ok = TeamsWebhookNotifier(hook).send_test()
            return {"ok": ok}

        @app.put("/settings/gitlab-token")
        def set_gitlab_token(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            token = (payload.get("token") or "").strip()
            if token:
                if os.environ.get("RELAY_INTEGRATIONS_LOCKED", "").lower() == "true":
                    raise HTTPException(
                        status_code=403,
                        detail="Integration configuration is temporarily locked (pending validation). Contact the Relay maintainer.",
                    )
                settings_store.set(SettingsKey.GITLAB_TOKEN, token)
            else:
                settings_store.delete(SettingsKey.GITLAB_TOKEN)
            return {"ok": True, "configured": bool(token)}

        @app.post("/settings/gitlab-token/test")
        def test_gitlab_token(request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            token = settings_store.get(SettingsKey.GITLAB_TOKEN)
            if not token:
                raise HTTPException(status_code=404, detail="no token configured")
            # GitLab API knowledge lives in the adapter; the endpoint just delegates.
            # Verify against a concrete project when one is supplied (?project=…)
            # or configured (RELAY_GITLAB_PROJECT_ID) so the test exercises the
            # same create-issue capability Relay needs at incident time, not just
            # that the token authenticates.
            base_url = os.environ.get("RELAY_GITLAB_BASE_URL", "https://gitlab.com")
            project = (
                request.query_params.get("project")
                or os.environ.get("RELAY_GITLAB_PROJECT_ID", "").strip()
                or None
            )
            result = GitLabSink.test_token(token, base_url=base_url, project=project)
            if result.get("error") and not result["ok"]:
                raise HTTPException(
                    status_code=502, detail=f"gitlab token check failed: {result['error']}"
                )
            return {
                "ok": result["ok"],
                "username": result.get("username", ""),
                "scopes": result.get("scopes", []),
                "project": result.get("project"),
                "access_level": result.get("access_level"),
            }

        @app.put("/settings/servicenow-credentials")
        def set_servicenow_credentials(
            payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            instance_url = (payload.get("instance_url") or "").strip().rstrip("/")
            username = (payload.get("username") or "").strip()
            password = (payload.get("password") or "").strip()
            # Treat an instance URL + password as "configured". Saving with all
            # fields blank clears the whole ServiceNow config (parallels the
            # GitLab empty-token clear).
            if instance_url and password:
                if os.environ.get("RELAY_INTEGRATIONS_LOCKED", "").lower() == "true":
                    raise HTTPException(
                        status_code=403,
                        detail="Integration configuration is temporarily locked (pending validation). Contact the Relay maintainer.",
                    )
                settings_store.set(SettingsKey.SERVICENOW_INSTANCE_URL, instance_url)
                settings_store.set(SettingsKey.SERVICENOW_USERNAME, username)
                settings_store.set(SettingsKey.SERVICENOW_PASSWORD, password)
                return {"ok": True, "configured": True}
            if not instance_url and not username and not password:
                settings_store.delete(SettingsKey.SERVICENOW_INSTANCE_URL)
                settings_store.delete(SettingsKey.SERVICENOW_USERNAME)
                settings_store.delete(SettingsKey.SERVICENOW_PASSWORD)
                return {"ok": True, "configured": False}
            raise HTTPException(
                status_code=400,
                detail="instance_url and password are required (or clear all fields to remove)",
            )

        @app.post("/settings/servicenow-credentials/test")
        def test_servicenow_credentials(request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if settings_store is None:
                raise HTTPException(status_code=503, detail="settings store unavailable")
            instance_url = settings_store.get(SettingsKey.SERVICENOW_INSTANCE_URL) or ""
            username = settings_store.get(SettingsKey.SERVICENOW_USERNAME) or ""
            password = settings_store.get(SettingsKey.SERVICENOW_PASSWORD) or ""
            if not instance_url or not password:
                raise HTTPException(status_code=404, detail="no ServiceNow credentials configured")
            # ServiceNow API knowledge lives in the adapter; the endpoint delegates.
            result = ServiceNowSink.test_connection(instance_url, username, password)
            if result.get("error") and not result["ok"]:
                raise HTTPException(
                    status_code=502,
                    detail=f"servicenow check failed: {result['error']}",
                )
            return {
                "ok": result["ok"],
                "instance_url": result.get("instance_url", ""),
                "username": result.get("username", ""),
            }

        # ----------------------------------------------------------------
        # Scheduling — per-contact availability + generated week schedule.
        # ----------------------------------------------------------------
        @app.get("/availability")
        def list_availability() -> list[dict[str, Any]]:
            if schedule_store is None:
                return []
            try:
                avail: list[dict[str, Any]] = schedule_store.list_availability()
                return avail
            except Exception:
                logger.warning("list_availability failed", exc_info=True)
                return []

        @app.put("/availability/{contact_id}")
        def put_availability(contact_id: str, payload: dict[str, Any],
                             request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            _auth.require_writer(dict(request.headers))
            if schedule_store is None:
                raise HTTPException(status_code=503, detail="schedule store unavailable")
            # Expected payload: {available: bool, slots: {weekday: [shift,...]},
            #                    ooo: {start,end}|null, roles: [role,...]}
            from relay.core.scheduling import Role
            valid_roles = {r.value for r in Role}
            raw_roles = payload.get("roles")
            if isinstance(raw_roles, list) and raw_roles:
                roles = [r for r in raw_roles if r in valid_roles]
            else:
                # Default: eligible for primary + secondary (manager is opt-in).
                roles = [Role.PRIMARY.value, Role.SECONDARY.value]
            data = {
                "available": bool(payload.get("available", False)),
                "slots": payload.get("slots", {}) or {},
                "ooo": payload.get("ooo"),
                "roles": roles,
            }
            schedule_store.put_availability(contact_id, data)
            return {"ok": True, "contact_id": contact_id}

        @app.get("/schedule")
        def get_schedule(week: str | None = None) -> dict[str, Any]:
            from datetime import date as _date

            from relay.core.scheduling import monday_of
            if schedule_store is None:
                return {"week_start": None, "slots": [], "coverage": [0, 0], "gaps": 0}
            if week:
                try:
                    ws = monday_of(_date.fromisoformat(week))
                except ValueError:
                    ws = monday_of(datetime.now(UTC).date())
            else:
                ws = monday_of(datetime.now(UTC).date())
            existing = schedule_store.get_schedule(ws.isoformat())
            if existing:
                # Overlay ad-hoc overrides (cover-me) onto the generated schedule.
                try:
                    from relay.core.scheduling import apply_overrides
                    ov = schedule_store.get_overrides(ws.isoformat())
                    if ov:
                        existing = apply_overrides(existing, ov)
                except Exception:
                    logger.warning("applying overrides failed in /schedule", exc_info=True)
                slots = existing.get("slots", [])
                gaps = sum(1 for s in slots if not s.get("contact_id"))
                # Per-role coverage (covered, total) for the UI's role tabs.
                cov_by_role: dict[str, list[int]] = {}
                for s in slots:
                    role = s.get("role", "primary")
                    entry = cov_by_role.setdefault(role, [0, 0])
                    entry[1] += 1
                    if s.get("contact_id"):
                        entry[0] += 1
                return {
                    "week_start": ws.isoformat(),
                    "slots": slots,
                    "coverage": [len(slots) - gaps, len(slots)],
                    "coverage_by_role": cov_by_role,
                    "roles": existing.get("roles", []),
                    "gaps": gaps,
                    "counts": existing.get("counts", {}),
                }
            return {"week_start": ws.isoformat(), "slots": [], "coverage": [0, 0], "gaps": 0}

        @app.post("/schedule/auto")
        def auto_schedule_week(request: Request, week: str | None = None) -> dict[str, Any]:
            from datetime import date as _date

            from fastapi import HTTPException

            from relay.core.scheduling import (
                Availability,
                OutOfOffice,
                Role,
                Shift,
                auto_schedule,
                monday_of,
            )
            _auth.require_writer(dict(request.headers))
            if schedule_store is None:
                raise HTTPException(status_code=503, detail="schedule store unavailable")
            ws = (
                monday_of(_date.fromisoformat(week)) if week
                else monday_of(datetime.now(UTC).date())
            )
            # Build Availability objects from stored records.
            avails = []
            for rec in schedule_store.list_availability():
                ooo = None
                raw_ooo = rec.get("ooo")
                if raw_ooo and raw_ooo.get("start") and raw_ooo.get("end"):
                    try:
                        ooo = OutOfOffice(
                            start=_date.fromisoformat(raw_ooo["start"]),
                            end=_date.fromisoformat(raw_ooo["end"]),
                        )
                    except ValueError:
                        ooo = None
                slots = {
                    day: {Shift(s) for s in shifts if s in {x.value for x in Shift}}
                    for day, shifts in (rec.get("slots") or {}).items()
                }
                valid_roles = {r.value for r in Role}
                rec_roles = rec.get("roles")
                if isinstance(rec_roles, list) and rec_roles:
                    roles_set = {Role(r) for r in rec_roles if r in valid_roles}
                else:
                    roles_set = {Role.PRIMARY, Role.SECONDARY}
                avails.append(Availability(
                    contact_id=rec["contact_id"],
                    available=bool(rec.get("available", False)),
                    slots=slots,
                    ooo=ooo,
                    roles=roles_set,
                ))
            sched = auto_schedule(ws, avails)
            slots_json = [
                {
                    "date": s.date.isoformat(),
                    "shift": str(s.shift),
                    "role": str(s.role),
                    "contact_id": s.contact_id,
                }
                for s in sched.slots
            ]
            counts = sched.counts_by_contact()
            covered, total = sched.coverage
            cov_by_role = {str(r): list(ct) for r, ct in sched.coverage_by_role().items()}
            roles_json = [str(r) for r in sched.roles]
            schedule_store.put_schedule(ws.isoformat(), {
                "week_start": ws.isoformat(), "slots": slots_json, "counts": counts,
                "roles": roles_json,
            })
            return {
                "week_start": ws.isoformat(),
                "slots": slots_json,
                "coverage": [covered, total],
                "coverage_by_role": cov_by_role,
                "roles": roles_json,
                "gaps": len(sched.gaps),
                "counts": counts,
            }

        # --- Ad-hoc schedule overrides (cover-me) ---
        @app.get("/schedule/overrides")
        def list_overrides(week: str) -> dict[str, Any]:
            from datetime import date as _date

            from relay.core.scheduling import monday_of
            if schedule_store is None or not hasattr(schedule_store, "get_overrides"):
                return {"week_start": week, "overrides": []}
            try:
                ws = monday_of(_date.fromisoformat(week))
            except ValueError:
                ws = monday_of(datetime.now(UTC).date())
            return {"week_start": ws.isoformat(),
                    "overrides": schedule_store.get_overrides(ws.isoformat())}

        @app.put("/schedule/override")
        def put_override(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from datetime import date as _date

            from fastapi import HTTPException

            from relay.core.scheduling import Role, Shift, monday_of
            ident = _auth.require_writer(dict(request.headers))
            if schedule_store is None or not hasattr(schedule_store, "put_override"):
                raise HTTPException(status_code=503, detail="schedule store unavailable")
            # Required: date (ISO), shift, role. contact_id None => clear coverage.
            try:
                d = _date.fromisoformat(payload["date"])
                shift = Shift(payload["shift"])
                role = Role(payload["role"])
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"invalid override: {exc}")
            cid = payload.get("contact_id") or None
            ws = monday_of(d)
            override = {
                "date": d.isoformat(), "shift": str(shift), "role": str(role),
                "contact_id": cid, "by": ident.subject,
            }
            schedule_store.put_override(ws.isoformat(), override)
            return {"ok": True, "week_start": ws.isoformat(), "override": override}

        @app.delete("/schedule/override")
        def delete_override(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from datetime import date as _date

            from fastapi import HTTPException

            from relay.core.scheduling import monday_of
            _auth.require_writer(dict(request.headers))
            if schedule_store is None or not hasattr(schedule_store, "delete_override"):
                raise HTTPException(status_code=503, detail="schedule store unavailable")
            try:
                d = _date.fromisoformat(payload["date"])
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"invalid override: {exc}")
            ws = monday_of(d)
            schedule_store.delete_override(
                ws.isoformat(), d.isoformat(),
                payload.get("shift", ""), payload.get("role", ""),
            )
            return {"ok": True, "week_start": ws.isoformat()}

        # ----------------------------------------------------------------
        # Ignore rules — CRUD + ignore action + deviation + download
        # ----------------------------------------------------------------
        @app.get("/rules")
        def list_rules() -> dict[str, Any]:
            if ignore_rule_store is None:
                return {"rules": []}
            try:
                rows = ignore_rule_store.list_rules()
            except Exception:
                logger.warning("list_rules failed", exc_info=True)
                return {"rules": []}
            return {
                "rules": [
                    {"rule_id": rid, "trigger_count": n, **rule.model_dump(mode="json")}
                    for (rid, rule, n) in rows
                ]
            }

        @app.get("/rules/deviation")
        def rules_deviation() -> dict[str, Any]:
            """Report whether the live DB rule set deviates from the config baseline."""
            if ignore_rule_store is None:
                return {
                    "deviates": False,
                    "db_count": 0,
                    "baseline_count": len(ignore_baseline),
                    "added": [],
                    "removed": [],
                }
            try:
                db_rows = ignore_rule_store.list_rules()
            except Exception:
                logger.warning("list_rules failed in /rules/deviation", exc_info=True)
                db_rows = []

            def _rule_key(r: Any) -> str:
                """Canonical key for a rule: matcher+name+note+enabled only."""
                import json as _json

                return _json.dumps(
                    {
                        "name": r.name,
                        "account_id": r.account_id,
                        "app_name": r.app_name,
                        "alarm_name": r.alarm_name,
                        "alarm_name_prefix": r.alarm_name_prefix,
                        "environment": (
                            sorted(r.environment)
                            if isinstance(r.environment, list)
                            else r.environment
                        ),
                        "tags": dict(sorted((r.tags or {}).items())),
                        "note": r.note,
                        "enabled": r.enabled,
                    },
                    sort_keys=True,
                )

            db_keys = {_rule_key(rule): rule for (_, rule, _) in db_rows}
            baseline_keys = {_rule_key(r): r for r in ignore_baseline}

            added_keys = set(db_keys) - set(baseline_keys)
            removed_keys = set(baseline_keys) - set(db_keys)

            def _summary(r: Any) -> dict[str, Any]:
                return {
                    "name": r.name,
                    "account_id": r.account_id,
                    "app_name": r.app_name,
                    "alarm_name": r.alarm_name,
                    "alarm_name_prefix": r.alarm_name_prefix,
                    "environment": r.environment,
                    "enabled": r.enabled,
                }

            return {
                "deviates": bool(added_keys or removed_keys),
                "db_count": len(db_rows),
                "baseline_count": len(ignore_baseline),
                "added": [_summary(db_keys[k]) for k in sorted(added_keys)],
                "removed": [_summary(baseline_keys[k]) for k in sorted(removed_keys)],
            }

        @app.get("/rules/download")
        def download_rules() -> Any:
            """Download current DB rules as a routing.yaml ignore block."""
            from fastapi.responses import Response as _Response

            if ignore_rule_store is None:
                rules_list: list[dict[str, Any]] = []
            else:
                try:
                    db_rows = ignore_rule_store.list_rules()
                    rules_list = [
                        rule.model_dump(mode="json", exclude_none=True)
                        for (_, rule, _) in db_rows
                    ]
                except Exception:
                    logger.warning("list_rules failed in /rules/download", exc_info=True)
                    rules_list = []

            block = {"ignore": {"enabled": True, "rules": rules_list}}
            header = (
                "# Relay ignore rules — regenerated from DynamoDB.\n"
                "# Paste this block into your routing.yaml under the top-level key.\n"
                "# Remove rules you no longer need, then redeploy.\n\n"
            )
            yaml_text = header + yaml.safe_dump(block, sort_keys=False, allow_unicode=True)
            return _Response(
                content=yaml_text,
                media_type="application/yaml",
                headers={"Content-Disposition": "attachment; filename=routing.yaml"},
            )

        @app.post("/rules")
        def create_rule(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            from pydantic import ValidationError

            from relay.config.schema import IgnoreRule

            ident = _auth.require_writer(dict(request.headers))
            if ignore_rule_store is None:
                raise HTTPException(status_code=503, detail="ignore rule store unavailable")
            # At least one matcher field is required; a rule with no matchers
            # would drop every incident — reject it here so the DB never stores
            # a catch-all by accident.
            _matcher_fields = (
                "account_id", "app_name", "alarm_name",
                "alarm_name_prefix", "environment", "tags",
            )
            has_matcher = any(
                payload.get(f) not in (None, "", {}, [])
                for f in _matcher_fields
            )
            if not has_matcher:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "at least one matcher field is required "
                        "(account_id, app_name, alarm_name, alarm_name_prefix, "
                        "environment, or tags)"
                    ),
                )
            now = datetime.now(UTC)
            try:
                rule = IgnoreRule(
                    **{k: v for k, v in payload.items()
                       if k not in ("created_by", "created_at")},
                    created_by=ident.subject,
                    created_at=now,
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            rule_id = ignore_rule_store.put_rule(rule)
            _emit_rule_change("created", rule_id, actor=ident.subject, rule=rule)
            logger.info(
                "Ignore rule %s created by %s via UI", rule_id, ident.subject
            )
            return {"ok": True, "rule_id": rule_id}

        @app.put("/rules/{rule_id}")
        def update_rule(
            rule_id: str, payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            from fastapi import HTTPException
            from pydantic import ValidationError

            ident = _auth.require_writer(dict(request.headers))
            if ignore_rule_store is None:
                raise HTTPException(status_code=503, detail="ignore rule store unavailable")
            existing = ignore_rule_store.get_rule(rule_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="rule not found")
            # Merge: start from existing fields, overlay caller's payload.
            merged = existing.model_dump()
            for key, value in payload.items():
                if key not in ("created_by", "created_at"):
                    merged[key] = value
            try:
                updated = existing.__class__(**merged)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            ignore_rule_store.put_rule(updated, rule_id=rule_id)
            _emit_rule_change("updated", rule_id, actor=ident.subject, rule=updated)
            logger.info(
                "Ignore rule %s updated by %s via UI", rule_id, ident.subject
            )
            return {"ok": True, "rule_id": rule_id}

        @app.delete("/rules/{rule_id}")
        def delete_rule(rule_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException

            ident = _auth.require_writer(dict(request.headers))
            if ignore_rule_store is None:
                raise HTTPException(status_code=503, detail="ignore rule store unavailable")
            if ignore_rule_store.get_rule(rule_id) is None:
                raise HTTPException(status_code=404, detail="rule not found")
            ignore_rule_store.delete_rule(rule_id)
            _emit_rule_change("deleted", rule_id, actor=ident.subject)
            logger.info(
                "Ignore rule %s deleted by %s via UI", rule_id, ident.subject
            )
            return {"ok": True, "deleted": rule_id}

        @app.post("/incidents/{correlation_id}/ignore")
        def ignore_incident(
            correlation_id: str, payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            """Create an ignore rule from an incident and auto-resolve it."""
            from fastapi import HTTPException

            from relay.config.schema import IgnoreRule

            ident = _auth.require_writer(dict(request.headers))
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            if ignore_rule_store is None:
                raise HTTPException(status_code=503, detail="ignore rule store unavailable")
            incident = incident_store.get_incident(correlation_id)
            if incident is None:
                raise HTTPException(status_code=404, detail="incident not found")

            now = datetime.now(UTC)
            # Build the ignore rule: defaults give a precise match on this
            # specific incident; body overrides allow broadening (prefix, note…).
            rule_kwargs: dict[str, Any] = {
                "account_id": incident.account_id,
                "app_name": incident.app_name,
                "alarm_name": incident.alarm_name,
                "environment": incident.environment,
                "enabled": True,
                "created_by": ident.subject,
                "created_at": now,
            }
            # Allow body to broaden / override individual fields.
            for field in (
                "name", "note", "tags",
                "alarm_name_prefix", "account_id", "app_name",
                "alarm_name", "environment",
            ):
                if field in payload and payload[field] is not None:
                    rule_kwargs[field] = payload[field]
            # If caller provided alarm_name_prefix, drop the exact alarm_name
            # so the rule doesn't over-constrain (prefix is the broader match).
            if payload.get("alarm_name_prefix"):
                rule_kwargs.pop("alarm_name", None)

            rule = IgnoreRule(**rule_kwargs)
            rule_id = ignore_rule_store.put_rule(rule)

            # Auto-resolve the incident so it leaves the active list.
            incident.state = IncidentState.RESOLVED
            incident.updated_at = now
            incident.timeline.append(
                TimelineEvent(
                    event_id=f"ign-{int(now.timestamp())}",
                    incident_id=correlation_id,
                    stream=Stream.CENTRAL,
                    occurred_at=now,
                    actor=ident.subject,
                    event_type="ignored",
                    detail={"via": "hub-ui", "ignore_rule_id": rule_id},
                )
            )
            incident_store.put_incident(incident)

            # Dispatch RESOLVED so external tickets (GitLab, ServiceNow) close.
            processor = getattr(self, "_processor", None)
            if processor is not None:
                try:
                    processor.dispatch_event(
                        IncidentLifecycleEvent.RESOLVED, incident
                    )
                except Exception:
                    logger.warning(
                        "RESOLVED dispatch failed for ignored incident %s",
                        correlation_id,
                        exc_info=True,
                    )

            _emit_rule_change("ignored", rule_id, actor=ident.subject, rule=rule)
            logger.info(
                "Incident %s ignored by %s via UI; rule_id=%s",
                correlation_id,
                ident.subject,
                rule_id,
            )
            return {"ok": True, "rule_id": rule_id, "state": incident.state}

        # ----------------------------------------------------------------
        # Routing rules — CRUD + route action + deviation + download
        # ----------------------------------------------------------------
        @app.get("/routing-rules")
        def list_routing_rules() -> dict[str, Any]:
            if routing_rule_store is None:
                return {"rules": []}
            try:
                rows = routing_rule_store.list_rules()
            except Exception:
                logger.warning("list_routing_rules failed", exc_info=True)
                return {"rules": []}
            return {
                "rules": [
                    {"rule_id": rid, "match_count": n, "enabled": en, **rule.model_dump(mode="json")}
                    for (rid, rule, n, en) in rows
                ]
            }

        @app.get("/escalation-policies")
        def list_escalation_policies() -> dict[str, Any]:
            if hub_config is None or getattr(hub_config, "escalation", None) is None:
                return {"policies": []}
            try:
                return {
                    "policies": [
                        {
                            "policy_id": p.policy_id,
                            "name": getattr(p, "name", None) or p.policy_id,
                        }
                        for p in hub_config.escalation.policies
                    ]
                }
            except Exception:
                logger.warning("list_escalation_policies failed", exc_info=True)
                return {"policies": []}

        @app.get("/routing-rules/deviation")
        def routing_rules_deviation() -> dict[str, Any]:
            """Report whether the live DB routing rule set deviates from the config baseline."""
            if routing_rule_store is None:
                return {
                    "deviates": False,
                    "db_count": 0,
                    "baseline_count": len(routing_baseline),
                    "added": [],
                    "removed": [],
                }
            try:
                db_rows = routing_rule_store.list_rules()
            except Exception:
                logger.warning("list_rules failed in /routing-rules/deviation", exc_info=True)
                db_rows = []

            def _routing_rule_key(r: Any) -> str:
                """Canonical key for a routing rule."""
                import json as _json

                return _json.dumps(
                    {
                        "rule_id": r.rule_id,
                        "priority": r.priority,
                        "alarm_name_prefix": r.alarm_name_prefix,
                        "alarm_name_regex": r.alarm_name_regex,
                        "namespace_prefix": r.namespace_prefix,
                        "tag_filters": dict(sorted((r.tag_filters or {}).items())),
                        "severity_override": (
                            r.severity_override.value
                            if r.severity_override is not None
                            else None
                        ),
                        "escalation_policy_id": r.escalation_policy_id,
                        "streams": sorted(s.value for s in (r.streams or [])),
                    },
                    sort_keys=True,
                )

            db_keys = {_routing_rule_key(rule): rule for (_, rule, _, _) in db_rows}
            baseline_keys = {_routing_rule_key(r): r for r in routing_baseline}

            added_keys = set(db_keys) - set(baseline_keys)
            removed_keys = set(baseline_keys) - set(db_keys)

            def _routing_summary(r: Any) -> dict[str, Any]:
                return {
                    "rule_id": r.rule_id,
                    "priority": r.priority,
                    "alarm_name_prefix": r.alarm_name_prefix,
                    "alarm_name_regex": r.alarm_name_regex,
                    "namespace_prefix": r.namespace_prefix,
                    "severity_override": (
                        r.severity_override.value if r.severity_override is not None else None
                    ),
                    "escalation_policy_id": r.escalation_policy_id,
                }

            return {
                "deviates": bool(added_keys or removed_keys),
                "db_count": len(db_rows),
                "baseline_count": len(routing_baseline),
                "added": [_routing_summary(db_keys[k]) for k in sorted(added_keys)],
                "removed": [_routing_summary(baseline_keys[k]) for k in sorted(removed_keys)],
            }

        @app.get("/routing-rules/download")
        def download_routing_rules() -> Any:
            """Download current DB routing rules as a routing.yaml rules block."""
            from fastapi.responses import Response as _Response

            if routing_rule_store is None:
                rules_list: list[dict[str, Any]] = []
            else:
                try:
                    db_rows = routing_rule_store.list_rules()
                    rules_list = [
                        rule.model_dump(mode="json", exclude_none=True)
                        for (_, rule, _, _) in db_rows
                    ]
                except Exception:
                    logger.warning("list_rules failed in /routing-rules/download", exc_info=True)
                    rules_list = []

            block = {"rules": rules_list}
            header = (
                "# Relay routing rules — regenerated from DynamoDB.\n"
                "# Paste this block into your routing.yaml under the top-level key.\n"
                "# Remove rules you no longer need, then redeploy.\n\n"
            )
            yaml_text = header + yaml.safe_dump(block, sort_keys=False, allow_unicode=True)
            return _Response(
                content=yaml_text,
                media_type="application/yaml",
                headers={"Content-Disposition": "attachment; filename=routing-rules.yaml"},
            )

        @app.post("/routing-rules")
        def create_routing_rule(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from fastapi import HTTPException
            from pydantic import ValidationError

            from relay.core.model import RoutingRule as _RoutingRule

            ident = _auth.require_writer(dict(request.headers))
            if routing_rule_store is None:
                raise HTTPException(status_code=503, detail="routing rule store unavailable")
            # escalation_policy_id is required.
            if not payload.get("escalation_policy_id"):
                raise HTTPException(
                    status_code=422,
                    detail="escalation_policy_id is required",
                )
            # priority is required.
            if payload.get("priority") is None:
                raise HTTPException(
                    status_code=422,
                    detail="priority is required (int >= 0)",
                )
            # Generate rule_id if not provided.
            import uuid as _uuid
            rule_id = payload.get("rule_id") or _uuid.uuid4().hex
            enabled = bool(payload.get("enabled", True))
            try:
                rule = _RoutingRule(
                    **{k: v for k, v in payload.items() if k not in ("enabled", "rule_id")},
                    rule_id=rule_id,
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            stored_id = routing_rule_store.put_rule(rule, rule_id=rule_id, enabled=enabled)
            _emit_rule_change("created", stored_id, actor=ident.subject, rule=rule)
            logger.info(
                "Routing rule %s created by %s via UI", stored_id, ident.subject
            )
            return {"ok": True, "rule_id": stored_id}

        @app.put("/routing-rules/{rule_id}")
        def update_routing_rule(
            rule_id: str, payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            from fastapi import HTTPException
            from pydantic import ValidationError

            from relay.core.model import RoutingRule as _RoutingRule

            ident = _auth.require_writer(dict(request.headers))
            if routing_rule_store is None:
                raise HTTPException(status_code=503, detail="routing rule store unavailable")
            existing = routing_rule_store.get_rule(rule_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="rule not found")
            # Determine enabled from payload (keep existing if not provided).
            existing_rows = routing_rule_store.list_rules()
            existing_enabled = next(
                (en for (rid, _, _, en) in existing_rows if rid == rule_id), True
            )
            enabled = bool(payload.pop("enabled", existing_enabled))
            # Merge: start from existing fields, overlay caller's payload.
            merged = existing.model_dump()
            for key, value in payload.items():
                merged[key] = value
            # Preserve rule_id.
            merged["rule_id"] = rule_id
            try:
                updated = _RoutingRule(**merged)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            routing_rule_store.put_rule(updated, rule_id=rule_id, enabled=enabled)
            _emit_rule_change("updated", rule_id, actor=ident.subject, rule=updated)
            logger.info(
                "Routing rule %s updated by %s via UI", rule_id, ident.subject
            )
            return {"ok": True, "rule_id": rule_id}

        @app.delete("/routing-rules/{rule_id}")
        def delete_routing_rule(rule_id: str, request: Request) -> dict[str, Any]:
            from fastapi import HTTPException

            ident = _auth.require_writer(dict(request.headers))
            if routing_rule_store is None:
                raise HTTPException(status_code=503, detail="routing rule store unavailable")
            if routing_rule_store.get_rule(rule_id) is None:
                raise HTTPException(status_code=404, detail="rule not found")
            routing_rule_store.delete_rule(rule_id)
            _emit_rule_change("deleted", rule_id, actor=ident.subject)
            logger.info(
                "Routing rule %s deleted by %s via UI", rule_id, ident.subject
            )
            return {"ok": True, "deleted": rule_id}

        @app.post("/incidents/{correlation_id}/route")
        def route_incident(
            correlation_id: str, payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            """Create a routing rule prefilled from an incident.

            Does NOT resolve the incident — routing rules only affect FUTURE alarms.
            The current incident keeps its already-assigned severity and state.

            Body:
              escalation_policy_id (required)
              priority              (optional, default 50)
              severity_override     (optional)
              streams               (optional, default [TEAM, CENTRAL])
              alarm_name_prefix     (optional; if provided, overrides exact match)
              alarm_name_regex      (optional)
              namespace_prefix      (optional)
              tag_filters           (optional)
              rule_id               (optional; generated if absent)
            """
            from fastapi import HTTPException
            from pydantic import ValidationError

            from relay.core.model import RoutingRule as _RoutingRule
            from relay.core.model import Stream as _Stream

            ident = _auth.require_writer(dict(request.headers))
            if incident_store is None:
                raise HTTPException(status_code=404, detail="incident store unavailable")
            if routing_rule_store is None:
                raise HTTPException(status_code=503, detail="routing rule store unavailable")
            incident = incident_store.get_incident(correlation_id)
            if incident is None:
                raise HTTPException(status_code=404, detail="incident not found")
            if not payload.get("escalation_policy_id"):
                raise HTTPException(
                    status_code=422,
                    detail="escalation_policy_id is required",
                )

            import uuid as _uuid
            rule_id = payload.get("rule_id") or _uuid.uuid4().hex
            priority = int(payload.get("priority", 50))
            enabled = bool(payload.get("enabled", True))

            # Build rule kwargs: default to exact alarm_name match from incident.
            rule_kwargs: dict[str, Any] = {
                "rule_id": rule_id,
                "priority": priority,
                "alarm_name_prefix": incident.alarm_name,  # exact-ish default
                "escalation_policy_id": payload["escalation_policy_id"],
                "streams": payload.get("streams", [_Stream.TEAM, _Stream.CENTRAL]),
            }
            # Allow body to override/broaden individual matcher fields.
            for field in (
                "alarm_name_prefix", "alarm_name_regex",
                "namespace_prefix", "tag_filters",
                "severity_override",
            ):
                if field in payload and payload[field] is not None:
                    rule_kwargs[field] = payload[field]

            try:
                rule = _RoutingRule(**rule_kwargs)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            stored_id = routing_rule_store.put_rule(rule, rule_id=rule_id, enabled=enabled)
            _emit_rule_change("created", stored_id, actor=ident.subject, rule=rule)
            logger.info(
                "Routing rule %s created from incident %s by %s via UI",
                stored_id, correlation_id, ident.subject,
            )
            # NOTE: incident state is NOT modified — routing rules only affect future alarms.
            return {"ok": True, "rule_id": stored_id}

        # ----------------------------------------------------------------
        # POST /ingest/alarm — in-process alarm injection (local runtimes only)
        #
        # Accepts a raw CloudWatch Alarm State Change event dict and runs it
        # through the DetectionPipeline (Node→Hub in-process).  Gated to
        # local-aws / local-mock runtimes, or when RELAY_ALLOW_INGEST=true,
        # so the endpoint is never reachable in production Fargate deployments.
        # ----------------------------------------------------------------
        @app.post("/ingest/alarm")
        def ingest_alarm(payload: dict[str, Any]) -> dict[str, Any]:
            allow_ingest = os.environ.get("RELAY_ALLOW_INGEST", "").lower() == "true"
            if runtime not in {"local-aws", "local-mock"} and not allow_ingest:
                raise HTTPException(
                    status_code=403,
                    detail="ingest disabled in this runtime",
                )
            if pipeline is None:
                raise HTTPException(
                    status_code=503,
                    detail="detection pipeline unavailable",
                )
            try:
                alarm_result: dict[str, Any] = pipeline.handle_alarm(payload)
                return alarm_result
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        # ----------------------------------------------------------------
        # POST /ingest/heartbeat — in-process heartbeat injection (local runtimes)
        #
        # Accepts a relay.heartbeat detail dict (the same shape a Node emits over
        # EventBridge) and feeds it straight to HubProcessor._handle_heartbeat,
        # which records it in the fleet store + in-memory cache and merges the
        # org_path into the registration-derived tree. Gated exactly like
        # /ingest/alarm (local-aws / local-mock, or RELAY_ALLOW_INGEST=true) so it
        # is never reachable in production Fargate.
        #
        # This is what lets a collapsed single-container runtime (no SQS, no
        # separate Node) keep its big-board tiles LIVE between incidents, and what
        # the test-environment bootstrap loops to populate a realistic fleet.
        # ----------------------------------------------------------------
        @app.post("/ingest/heartbeat")
        def ingest_heartbeat(payload: dict[str, Any]) -> dict[str, Any]:
            allow_ingest = os.environ.get("RELAY_ALLOW_INGEST", "").lower() == "true"
            if runtime not in {"local-aws", "local-mock"} and not allow_ingest:
                raise HTTPException(
                    status_code=403,
                    detail="ingest disabled in this runtime",
                )
            processor = getattr(self, "_processor", None)
            if processor is None:
                raise HTTPException(
                    status_code=503,
                    detail="hub processor unavailable",
                )
            # Accept either a bare heartbeat detail or an EventBridge-style
            # envelope ({"detail": {...}}), mirroring handle_event's tolerance.
            detail = payload.get("detail", payload)
            if not detail.get("account_id") or not detail.get("app_name"):
                raise HTTPException(
                    status_code=400,
                    detail="heartbeat requires account_id and app_name",
                )
            processor._handle_heartbeat(detail)
            return {
                "ok": True,
                "account_id": detail.get("account_id"),
                "app_name": detail.get("app_name"),
            }

        # ----------------------------------------------------------------
        # POST /synthetic/incident — fire a synthetic smoke-test incident
        #
        # Writer-gated.  Builds a synthetic CloudWatch Alarm State Change
        # EventBridge event and runs it through the REAL pipeline so that
        # paging, tile updates, adapter sinks and federation are all exercised
        # end-to-end.  The pipeline stamps Incident.synthetic=True because
        # relay_synthetic=True is present at the top level and in the detail.
        # Unlike /ingest/alarm this route is intentionally available in ALL
        # runtimes — it is gated by the writer auth check instead.
        # ----------------------------------------------------------------
        @app.post("/synthetic/incident")
        def trigger_synthetic_incident(
            payload: dict[str, Any], request: Request
        ) -> dict[str, Any]:
            from fastapi import HTTPException

            _auth.require_writer(dict(request.headers))
            if pipeline is None:
                raise HTTPException(
                    status_code=503,
                    detail="detection pipeline unavailable",
                )

            # ---- resolve overrides with sensible defaults ----
            app_name = str(payload.get("app_name") or "synthetic-test")
            account_id = str(
                payload.get("account_id")
                or os.environ.get("RELAY_ACCOUNT_ID", "000000000000")
            )
            region = str(payload.get("region") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            severity = str(payload.get("severity") or "SEV3").upper()
            alarm_name = str(payload.get("alarm_name") or "synthetic-smoke-test")
            environment = str(payload.get("environment") or "")
            deployment_id = str(payload.get("deployment_id") or "")

            # Namespace encodes severity so the downstream parser can use it.
            namespace = f"Relay/Synthetic/{severity}"

            # ---- build the EventBridge envelope ----
            # Shape mirrors what CloudWatchAlarmSource.parse_event() expects.
            # relay_synthetic=True at the top level AND inside detail ensures
            # the detection pipeline stamps Incident.synthetic=True regardless
            # of where it checks the flag.
            event: dict[str, Any] = {
                "source": "aws.cloudwatch",
                "detail-type": "CloudWatch Alarm State Change",
                "account": account_id,
                "region": region,
                "relay_synthetic": True,
                "detail": {
                    "alarmName": alarm_name,
                    "alarmArn": (
                        f"arn:aws:cloudwatch:{region}:{account_id}:alarm:{alarm_name}"
                    ),
                    "relay_synthetic": True,
                    "state": {
                        "value": "ALARM",
                        "reason": f"Synthetic smoke-test triggered by operator (severity={severity})",
                        "reasonData": "{}",
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    "previousState": {
                        "value": "OK",
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    "configuration": {
                        "description": f"Relay synthetic incident — {app_name}",
                        "metrics": [
                            {
                                "metricStat": {
                                    "metric": {
                                        "namespace": namespace,
                                        "name": "SyntheticCheck",
                                        "dimensions": {
                                            "app": app_name,
                                            **({"environment": environment} if environment else {}),
                                            **({"deployment_id": deployment_id} if deployment_id else {}),
                                        },
                                    },
                                    "period": 60,
                                    "stat": "Average",
                                }
                            }
                        ],
                    },
                    # Relay-specific hints carried in the detail so tag-less
                    # pipelines can still derive app_name / environment.
                    "relay_app": app_name,
                    **({"relay_environment": environment} if environment else {}),
                    **({"relay_deployment_id": deployment_id} if deployment_id else {}),
                },
            }

            try:
                event_result: dict[str, Any] = pipeline.handle_alarm(event)
                return event_result
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        # ----------------------------------------------------------------
        # POST /admin/purge — temporal / synthetic-only incident purge
        #
        # Writer-gated.  Delegates to DynamoIncidentStore.purge_incidents().
        # Safety rule: refuses to purge ALL incidents (no bounds, not
        # synthetic_only) unless dry_run=True.
        # ----------------------------------------------------------------
        @app.post("/admin/purge")
        def admin_purge(payload: dict[str, Any], request: Request) -> dict[str, Any]:
            from datetime import UTC
            from datetime import datetime as _dt

            from fastapi import HTTPException

            _auth.require_writer(dict(request.headers))

            if incident_store is None or not hasattr(incident_store, "purge_incidents"):
                raise HTTPException(
                    status_code=503,
                    detail="purge_incidents not available on this store",
                )

            # ---- parse temporal bounds ----
            def _parse_dt(key: str) -> _dt | None:
                raw = payload.get(key)
                if raw is None:
                    return None
                if not isinstance(raw, str):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{key}' must be an ISO-8601 string or null",
                    )
                try:
                    dt = _dt.fromisoformat(raw)
                except ValueError:
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{key}' is not a valid ISO-8601 datetime: {raw!r}",
                    )
                # Attach UTC if naive.
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt

            before = _parse_dt("before")
            after = _parse_dt("after")
            synthetic_only: bool = bool(payload.get("synthetic_only", False))
            dry_run: bool = bool(payload.get("dry_run", False))

            # ---- safety gate ----
            if not dry_run and before is None and after is None and not synthetic_only:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "refusing to purge ALL incidents without a temporal bound or "
                        "synthetic_only; pass dry_run=true to preview or set a bound"
                    ),
                )

            purge_result: dict[str, Any] = incident_store.purge_incidents(
                before=before,
                after=after,
                synthetic_only=synthetic_only,
                dry_run=dry_run,
            )

            # Purge deletes incident rows directly, bypassing the per-event
            # fleet decrement (FleetStore.apply_incident on RESOLVED/CLOSED), so
            # affected FLEET# tiles keep stale open counts / worst_severity and
            # the big board stays red until the next heartbeat or restart. Repair
            # them now: recompute each touched tile from the surviving open
            # incidents and push an SSE delta so connected boards clear live.
            # Skipped on dry_run (nothing was deleted) and when the store can't
            # list incidents to recompute from.
            affected = purge_result.get("affected_tiles") or []
            if (
                not dry_run
                and affected
                and incident_store is not None
                and hasattr(incident_store, "list_open_incidents")
            ):
                try:
                    survivors = incident_store.list_open_incidents()
                except Exception:
                    logger.warning(
                        "list_open_incidents failed during purge fleet recompute",
                        exc_info=True,
                    )
                    survivors = []
                repaired = 0
                for tkey in affected:
                    account_id = tkey.get("account_id")
                    app_name = tkey.get("app_name")
                    environment = tkey.get("environment") or "unrouted"
                    deployment_id = tkey.get("deployment_id")
                    if account_id is None or app_name is None:
                        continue
                    tile_open = [
                        i
                        for i in survivors
                        if i.account_id == account_id
                        and i.app_name == app_name
                        and (i.environment or "unrouted") == environment
                        and i.deployment_id == deployment_id
                    ]
                    try:
                        tile = hub_state.recompute_tile(
                            account_id,
                            app_name,
                            tile_open,
                            environment,
                            deployment_id,
                        )
                    except Exception:
                        logger.warning(
                            "fleet tile recompute failed for %s/%s after purge",
                            account_id,
                            app_name,
                            exc_info=True,
                        )
                        continue
                    if tile is not None:
                        sse_publisher.publish_delta(tile)
                        repaired += 1
                purge_result["tiles_recomputed"] = repaired

            return purge_result

        return app


# ---------------------------------------------------------------------------
# Fargate entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Fargate entrypoint."""
    configure_logging()

    app = HubApp()

    # Wire SIGTERM to the *app's* shutdown event — the one the sweep + SQS
    # consumer loops actually watch. (Previously the handler set a local Event
    # disconnected from HubApp._shutdown, so the threads never saw the signal.)
    def _sigterm_handler(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM — shutting down gracefully")
        app.request_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    app.start()
