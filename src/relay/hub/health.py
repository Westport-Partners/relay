"""relay.hub.health — Health model for the fleet big-board dashboard.

Provides:
  - Liveness enum (LIVE, STALE, LOST, UNKNOWN)
  - worst_of() resolution -> tile status (green/degraded/red/grey)
  - FleetTile dataclass
  - liveness_from_heartbeat() helper (injected clock for testability)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from relay.core.model import OrgTree, Severity

# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

# Default heartbeat cadence in seconds (Nodes emit every 60 s by default).
DEFAULT_CADENCE_SECONDS: int = 60


class Liveness(StrEnum):
    """Liveness state derived from time-since-last-heartbeat."""

    LIVE = "live"        # <= 2× cadence — all good
    STALE = "stale"      # 2×–5× cadence — falling behind
    LOST = "lost"        # > 5× cadence — no signal
    UNKNOWN = "unknown"  # registered but never reported (first-time)


def liveness_from_heartbeat(
    last_heartbeat_at: datetime | None,
    *,
    cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> Liveness:
    """Derive liveness from the age of the last heartbeat.

    Args:
        last_heartbeat_at: When the last heartbeat was received, or None if
            the app is registered but has never reported.
        cadence_seconds: Expected heartbeat period in seconds (default 60).
        clock: Callable returning the current UTC datetime (timezone-aware).
            Defaults to datetime.now(UTC).  Inject a fake for tests.

    Returns:
        Liveness enum member.
    """
    if clock is None:
        clock = lambda: datetime.now(UTC)  # noqa: E731

    if last_heartbeat_at is None:
        return Liveness.UNKNOWN

    now = clock()
    # Ensure comparison is timezone-aware.
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=UTC)

    age_seconds = (now - last_heartbeat_at).total_seconds()

    if age_seconds <= 2 * cadence_seconds:
        return Liveness.LIVE
    elif age_seconds <= 5 * cadence_seconds:
        return Liveness.STALE
    else:
        return Liveness.LOST


# ---------------------------------------------------------------------------
# Tile status
# ---------------------------------------------------------------------------

# Canonical tile status values ordered worst-first for sort.
_STATUS_ORDER = {"red": 0, "degraded": 1, "grey": 2, "green": 3}


def worst_of(
    liveness: Liveness,
    open_incidents: int = 0,
    worst_severity: Severity | None = None,
    has_acked: bool = False,
) -> str:
    """Resolve tile status from liveness + incident dimensions.

    Resolution per §2.4 of DASHBOARD.md::

        red      if liveness==lost  OR any open SEV1/SEV2
        degraded if liveness==stale OR any open SEV3/SEV4 / acknowledged
        grey     if liveness==unknown
        green    otherwise (live AND no open incidents)

    Args:
        liveness: Derived liveness state.
        open_incidents: Count of open (non-resolved/non-closed) incidents.
        worst_severity: Worst severity among open incidents (None if no open).
        has_acked: True if the worst open incident is in ACKNOWLEDGED state.

    Returns:
        "red" | "degraded" | "grey" | "green"
    """
    # --- red conditions ---
    if liveness == Liveness.LOST:
        return "red"
    if open_incidents > 0 and worst_severity in (Severity.SEV1, Severity.SEV2):
        return "red"

    # --- degraded conditions ---
    if liveness == Liveness.STALE:
        return "degraded"
    if open_incidents > 0 and worst_severity in (Severity.SEV3, Severity.SEV4):
        return "degraded"
    if open_incidents > 0 and has_acked:
        return "degraded"

    # --- grey: registered but never reported ---
    if liveness == Liveness.UNKNOWN:
        return "grey"

    # --- green: live, no open incidents ---
    return "green"


# ---------------------------------------------------------------------------
# FleetTile
# ---------------------------------------------------------------------------


@dataclass
class FleetTile:
    """Current health tile for a single (account_id, app_name) pair."""

    account_id: str
    app_name: str
    status: str                          # green / degraded / red / grey
    liveness: Liveness
    open_incidents: int
    worst_severity: Severity | None
    last_heartbeat_at: datetime | None
    registered_at: datetime
    environment: str = "unrouted"
    deployment_id: str = "unknown"
    service_path: list[str] = field(default_factory=list)
    # Org ancestry (root→leaf node dicts) as carried on the heartbeat. Persisted
    # already by FleetStore; surfaced here so the tile-detail drawer can show the
    # hierarchy without a second fetch.
    org_path: list[dict] = field(default_factory=list)
    # Free-form deployment meta (owner, gitlab_project, runbook, region, and —
    # when Node-side tag enrichment is enabled — aws_tags). Consumed by the
    # tile-detail drawer and by the AI investigation skills.
    metadata: dict = field(default_factory=dict)
    # On-call snapshot for this deployment. On a team Hub the detail endpoint
    # fills this live from the schedule; on a federated Hub it is the snapshot
    # the owning team pushed up its heartbeat (the Hub has no remote schedule).
    on_call: dict | None = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def key(self) -> str:
        return f"{self.environment}/{self.deployment_id}"

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "app_name": self.app_name,
            "environment": self.environment,
            "deployment_id": self.deployment_id,
            "service_path": self.service_path,
            "org_path": self.org_path,
            "metadata": self.metadata,
            "on_call": self.on_call,
            "status": self.status,
            "liveness": self.liveness.value,
            "open_incidents": self.open_incidents,
            "worst_severity": self.worst_severity.value if self.worst_severity else None,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
            "registered_at": self.registered_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
        }


def status_sort_key(tile: FleetTile) -> tuple[int, datetime]:
    """Sort key: worst status first, then most-recently-changed first."""
    order = _STATUS_ORDER.get(tile.status, 99)
    # Negate last_updated timestamp so more recent = smaller value (sort asc).
    neg_ts = -tile.last_updated.timestamp()
    return (order, neg_ts)


def compute_rollup(
    tiles: list[FleetTile],
    org_tree: OrgTree,
) -> list[dict]:
    """Compute rollup status for each non-leaf node in the OrgTree.

    For each non-leaf node, status = worst_of across all descendant
    deployment tiles. Returns a list of dicts suitable for dashboard JSON.

    Each dict has: id, name, level, parent, status, child_count, red_count,
    degraded_count, grey_count, green_count, children (recursive).
    """
    # Build a lookup from deployment_id -> tile
    tile_by_deployment: dict[str, FleetTile] = {t.deployment_id: t for t in tiles}

    _STATUS_ORDER_LOCAL = {"red": 0, "degraded": 1, "grey": 2, "green": 3}

    def _worst_status(statuses: list[str]) -> str:
        if not statuses:
            return "grey"
        return min(statuses, key=lambda s: _STATUS_ORDER_LOCAL.get(s, 99))

    def _node_rollup(node_id: str) -> dict:
        node = org_tree.get(node_id)
        if node is None:
            return {}
        deployments = org_tree.descendant_deployments(node_id)
        dep_tiles = [tile_by_deployment[d.id] for d in deployments if d.id in tile_by_deployment]

        counts = {"red": 0, "degraded": 0, "grey": 0, "green": 0}
        for t in dep_tiles:
            counts[t.status] = counts.get(t.status, 0) + 1

        rollup_status = _worst_status([t.status for t in dep_tiles]) if dep_tiles else "grey"
        child_nodes = org_tree.children(node_id)

        return {
            "id": node.id,
            "name": node.name,
            "level": node.level,
            "parent": node.parent,
            "status": rollup_status,
            "deployment_count": len(deployments),
            "red_count": counts["red"],
            "degraded_count": counts["degraded"],
            "grey_count": counts["grey"],
            "green_count": counts["green"],
            "children": [_node_rollup(c.id) for c in child_nodes],
        }

    return [_node_rollup(r.id) for r in org_tree.roots()]
