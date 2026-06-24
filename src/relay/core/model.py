"""relay.core.model — Pure domain model for the Relay incident-management engine.

No AWS imports, no boto3, no network calls. All types are serialisable via
Pydantic v2.  External layers (DynamoDB adapters, SNS publishers, etc.) import
from here; this module never imports from them.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware).

    Used as a ``default_factory`` for datetime fields so that the value is
    evaluated at *instance creation* time rather than at class definition time.
    """
    return datetime.now(UTC)


def _new_uuid() -> str:
    """Return a fresh UUID4 as a lowercase hex string."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """Operational severity level for an incident.

    Mirrors the SEV1-SEV4 convention used by most on-call runbooks:

    * SEV1 — service is completely down / customer-facing outage.
    * SEV2 — service is significantly degraded; some customers affected.
    * SEV3 — warning condition; no immediate customer impact but trending bad.
    * SEV4 — informational / low-priority; handled during business hours.
    """

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"

    # Human-readable labels for display / CLI output
    _labels: dict[str, str]  # type: ignore[assignment]  # populated below

    @classmethod
    def from_label(cls, label: str) -> Severity:
        """Return the ``Severity`` that best matches *label*.

        Performs an exact case-insensitive lookup first, then falls back to the
        enum member name.

        TODO: implement fuzzy matching (e.g. ``"critical"`` → SEV1,
              ``"high"`` → SEV2, ``"medium"`` → SEV3, ``"low"`` → SEV4) so
              that alert sources that use plain-English severity strings can be
              normalised automatically without extra glue code.
        """
        normalised = label.strip().upper()
        # Direct name match (e.g. "sev1" → SEV1)
        try:
            return cls[normalised]
        except KeyError:
            pass
        # TODO: fuzzy / synonym lookup (critical→SEV1, high→SEV2, …)
        raise ValueError(
            f"Cannot convert {label!r} to Severity. "
            "Expected one of: SEV1, SEV2, SEV3, SEV4."
        )


class SignalSource(StrEnum):
    """Origin of the alert that triggered the incident.

    * CLOUDWATCH_ALARM — fired by an Amazon CloudWatch metric alarm.
    * SYNTHETIC        — fired by a CloudWatch Synthetics canary.
    * OTEL             — fired by an OpenTelemetry-based alert rule.
    * MANUAL           — opened manually by an operator via CLI or UI.
    """

    CLOUDWATCH_ALARM = "CLOUDWATCH_ALARM"
    SYNTHETIC = "SYNTHETIC"
    OTEL = "OTEL"
    MANUAL = "MANUAL"


class Stream(StrEnum):
    """Fan-out target for notifications and timeline events.

    * TEAM    — routed to the owning team's on-call rotation / chat channel.
    * CENTRAL — routed to the central NOC / war-room channel for visibility.
    """

    TEAM = "TEAM"
    CENTRAL = "CENTRAL"


class IncidentState(StrEnum):
    """Lifecycle state of an :class:`Incident`.

    State transitions (happy path)::

        TRIGGERED → ACKNOWLEDGED → RESOLVED → CLOSED
                        ↓
                    ESCALATED → ACKNOWLEDGED → …
    """

    TRIGGERED = "TRIGGERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class Contact(BaseModel):
    """A person who can be paged or notified.

    PII (email, phone) lives in DynamoDB at runtime, NOT in Git. When
    persisting a ``Contact`` to source control or config files, omit
    ``email`` and ``phone`` — use only ``contact_id`` as the foreign key.
    """

    contact_id: str
    name: str
    email: EmailStr | None = None
    phone: str | None = None

    @model_validator(mode="after")
    def _require_at_least_one_channel(self) -> Contact:
        """Ensure the contact can actually be reached."""
        if self.email is None and self.phone is None:
            raise ValueError(
                "A Contact must have at least one of 'email' or 'phone' set."
            )
        return self


class TimelineEvent(BaseModel):
    """An immutable audit record appended to an :class:`Incident` timeline.

    Timeline events are **append-only**: once written they must never be
    mutated or deleted.  This invariant enables forensic replay and SLA
    reporting.

    ``actor`` is either the string ``"system"`` (for automated actions) or a
    ``contact_id`` (for human actions).
    ``event_type`` is a free-form dot-namespaced string, e.g.
    ``"incident.acknowledged"`` or ``"escalation.step_advanced"``.
    ``detail`` carries event-specific payload; schema is defined per
    ``event_type`` in the engine layer.
    """

    event_id: str = Field(default_factory=_new_uuid)
    incident_id: str
    stream: Stream
    occurred_at: datetime = Field(default_factory=_utcnow)
    actor: str
    event_type: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Incident(BaseModel):
    """The central aggregate for a single operational incident.

    An ``Incident`` is created when an alert is received and is the unit of
    work that flows through acknowledgement, escalation, and resolution.

    ``correlation_id`` deduplicates re-fires of the same alarm within a
    configurable window; the engine uses it to suppress duplicate incidents.
    """

    correlation_id: str = Field(default_factory=_new_uuid)
    account_id: str
    region: str
    app_name: str
    severity: Severity
    signal_source: SignalSource
    state: IncidentState = IncidentState.TRIGGERED
    alarm_name: str
    alarm_arn: str | None = None
    # Synthetic ("test"/"fake") marker. A team triggers synthetic incidents to
    # smoke-test the whole pipeline (paging, tiles, adapters, federation) on a
    # fresh deployment without corrupting real operational data. Synthetic
    # incidents are flagged so they render distinctly in the UI and are EXCLUDED
    # from all metric rollups (DORA/MTTR/counts) — see relay.core.metrics.
    #
    # Distinct from ``SignalSource.SYNTHETIC``: that enum means a CloudWatch
    # Synthetics *canary* failure, which is a first-class REAL trigger. This
    # boolean means "this incident is fake — do not count it." The two are
    # orthogonal (a synthetic test could even simulate a canary signal source).
    synthetic: bool = False
    tags: dict[str, str] = Field(default_factory=dict)
    environment: str = "unrouted"       # required in prod; default helps tests
    deployment_id: str = "unknown"      # leaf node id (the tile)
    environment_inferred: bool = False  # flagged when env derived from fallback
    service_path: list[str] = Field(default_factory=list)  # root->leaf names, display only
    # Routing provenance: which routing rule classified this incident.
    # routing_rule_id is None when NO rule matched and the catch-all default
    # (default_escalation_policy_id + derived severity) was used — surfaced in
    # the UI so a responder can tell an explicit rule from the fallback and
    # decide whether to create/edit a rule. routing_reason is the classifier's
    # human-readable explanation.
    routing_rule_id: str | None = None
    routing_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    # External ticketing linkage — a generic map keyed by adapter convention so
    # the core model never grows a column per integration. Sink listeners stamp
    # the ids they need to close the external record on resolve. Conventional
    # keys: ``"gitlab_project"`` (resolved project path/id from the catalog),
    # ``"gitlab_iid"`` (project-scoped issue IID), ``"servicenow_sys_id"``.
    # A new integration adds a key here — never a field on this model.
    external_tickets: dict[str, str] = Field(default_factory=dict)
    # Per-incident deployment metadata, resolved Node-side from the failing
    # resource's tags against the catalog's tag templates/tag_map (see
    # relay.config.tag_mapping). Carried on the incident so Hub adapters read
    # resolved values incident-first (e.g. deployment_metadata["gitlab_project"])
    # without reaching the org tree. Display + routing only.
    deployment_metadata: dict[str, Any] = Field(default_factory=dict)

    # Allow mutation so add_event() can modify the model in place.
    model_config = {"frozen": False}

    def add_event(self, event: TimelineEvent) -> None:
        """Append *event* to the timeline and bump :attr:`updated_at`.

        This is the only sanctioned way to add timeline events to an incident.
        Direct mutation of ``self.timeline`` should be avoided outside of
        migration/backfill scripts.
        """
        self.timeline.append(event)
        self.updated_at = _utcnow()

    def get_ticket(self, key: str) -> str | None:
        """Return the external-ticket id stored under *key*, or None."""
        return self.external_tickets.get(key)

    def set_ticket(self, key: str, value: str) -> None:
        """Stamp an external-ticket id under *key* (e.g. ``"gitlab_iid"``)."""
        self.external_tickets[key] = value


def external_ticket_event(
    incident: Incident, system: str, external_id: str
) -> TimelineEvent:
    """Build a ``<system>.ticket_created`` timeline event for an external link.

    Building the audit record is a core (domain) concern, so the event shape is
    defined here in one place rather than inside each adapter listener. Callers
    (sink listeners) append + persist it. The event is *not* appended here.
    """
    now = _utcnow()
    return TimelineEvent(
        event_id=f"{system}-{int(now.timestamp())}",
        incident_id=incident.correlation_id,
        stream=Stream.CENTRAL,
        occurred_at=now,
        actor="system",
        event_type=f"{system}.ticket_created",
        detail={"system": system, "external_id": external_id},
    )


# ---------------------------------------------------------------------------
# Escalation models
# ---------------------------------------------------------------------------


class EscalationStep(BaseModel):
    """One rung on an escalation ladder.

    If no acknowledgement is received within ``timeout_minutes`` after this
    step fires, the escalation engine advances to the next step
    (``step_index + 1``).  When there is no next step the incident is
    marked ``ESCALATED`` and a final all-streams page is sent.

    Who is paged at this step is expressed in two ways (at least one required):

    * ``roles``       — on-call roles (e.g. ``["primary"]``) resolved to the
                        current person via the generated schedule at page time.
                        This is the preferred form: it never names people, so
                        the policy file is stable as who's-on-call changes.
    * ``contact_ids`` — explicit contact IDs, paged regardless of schedule. An
                        escape hatch for fixed responders (e.g. a vendor).

    ``notify_streams`` controls which :class:`Stream` channels are notified
    at this step; defaults to ``[Stream.TEAM]``.
    """

    step_index: int = Field(ge=0, description="0-based position in the policy steps list.")
    contact_ids: list[str] = Field(default_factory=list)
    roles: list[str] = Field(
        default_factory=list,
        description="On-call roles to page (resolved via schedule at page time).",
    )
    timeout_minutes: int = Field(gt=0, description="Minutes to wait for ack before advancing.")
    notify_streams: list[Stream] = Field(default_factory=lambda: [Stream.TEAM])

    @model_validator(mode="after")
    def _require_a_paging_target(self) -> EscalationStep:
        """A step must page someone: at least one role or one contact_id."""
        if not self.roles and not self.contact_ids:
            raise ValueError(
                f"EscalationStep (index {self.step_index}) must specify at least "
                "one of 'roles' or 'contact_ids'."
            )
        return self


class EscalationPolicy(BaseModel):
    """An ordered sequence of :class:`EscalationStep` objects for a team.

    The engine walks through ``steps`` in order whenever an incident is not
    acknowledged in time.  ``steps`` must be non-empty and the
    ``step_index`` values must be the contiguous sequence ``0, 1, 2, …``.
    """

    policy_id: str
    name: str
    team: str
    steps: list[EscalationStep]

    @field_validator("steps")
    @classmethod
    def _validate_steps(cls, steps: list[EscalationStep]) -> list[EscalationStep]:
        """Ensure steps are non-empty and have contiguous 0-based indices."""
        if not steps:
            raise ValueError("EscalationPolicy.steps must not be empty.")
        sorted_steps = sorted(steps, key=lambda s: s.step_index)
        for expected, step in enumerate(sorted_steps):
            if step.step_index != expected:
                raise ValueError(
                    f"EscalationPolicy.steps must have contiguous step_index values "
                    f"starting from 0. Expected index {expected}, got {step.step_index}."
                )
        return sorted_steps


# ---------------------------------------------------------------------------
# Routing models
# ---------------------------------------------------------------------------


class RoutingRule(BaseModel):
    """A declarative rule that maps inbound alerts to an escalation policy.

    Rules are evaluated in ascending ``priority`` order (lower number = higher
    priority).  The first rule whose filters all match the incoming alarm wins.

    Filter fields are ANDed together; omitted (``None`` / empty) filters match
    anything:

    * ``alarm_name_prefix``   — alarm name must start with this string.
    * ``alarm_name_regex``    — alarm name must fully match this regex.
    * ``tag_filters``         — all key/value pairs must be present in alarm tags.
    * ``namespace_prefix``    — CloudWatch namespace must start with this string.

    ``severity_override`` replaces the severity carried by the alarm signal
    when the rule fires.

    TODO: compile ``alarm_name_regex`` eagerly and cache to avoid per-event
    recompilation.
    """

    rule_id: str
    priority: int = Field(ge=0, description="Lower value = evaluated first.")
    alarm_name_prefix: str | None = None
    alarm_name_regex: str | None = None
    tag_filters: dict[str, str] = Field(default_factory=dict)
    namespace_prefix: str | None = None
    severity_override: Severity | None = None
    escalation_policy_id: str
    streams: list[Stream] = Field(
        default_factory=lambda: [Stream.TEAM, Stream.CENTRAL]
    )

    @field_validator("alarm_name_regex")
    @classmethod
    def _validate_regex(cls, pattern: str | None) -> str | None:
        """Fail fast if the regex is syntactically invalid."""
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"alarm_name_regex {pattern!r} is not a valid regex: {exc}"
                ) from exc
        return pattern


# ---------------------------------------------------------------------------
# Org hierarchy models
# ---------------------------------------------------------------------------

class OrgNode(BaseModel):
    """A node in the application org hierarchy tree.

    Hierarchy levels are data-driven (declared in hierarchy.yaml).
    Default levels: product_line -> product -> component -> deployment.
    Deployments are leaf nodes and map to fleet dashboard tiles.

    ``owner_ref`` on any node is inherited downward: resolution walks
    parent pointers from deployment_id up to root, most-specific wins.
    ``metadata`` is free-form; merged leaf->root (leaf wins) by OrgTree.

    Integration-specific routing keys (e.g. a GitLab project path) live in
    ``metadata`` keyed by convention (``metadata["gitlab_project"]``) rather
    than as dedicated columns, so a new integration never edits this model.
    """

    id: str
    name: str
    level: str
    parent: str | None = None
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    owner_ref: str | None = None


class OrgTree:
    """In-memory tree built from a flat list of OrgNodes.

    Validates on build: every parent reference resolves, no cycles.

    Methods:
        children(id) -> list[OrgNode]
        ancestors(id) -> list[OrgNode]   # leaf->root (excluding the node itself)
        descendant_deployments(id) -> list[OrgNode]  # all leaf descendants
        roots() -> list[OrgNode]
        resolve_service_path(deployment_id) -> list[str]  # root->leaf names
        resolve_owner_ref(deployment_id) -> str | None   # most-specific wins
        resolve_metadata(deployment_id) -> dict           # leaf wins merge
    """

    def __init__(self, nodes: list[OrgNode], leaf_level: str = "deployment") -> None:
        self.leaf_level = leaf_level
        self._nodes: dict[str, OrgNode] = {n.id: n for n in nodes}
        self._validate()

    def _validate(self) -> None:
        """Validate parent references resolve and no cycles exist."""
        for node in self._nodes.values():
            if node.parent is not None and node.parent not in self._nodes:
                raise ValueError(
                    f"OrgNode '{node.id}' references unknown parent '{node.parent}'."
                )
        # Cycle detection: for each node walk to root; if we revisit any id it's a cycle.
        for start_id in self._nodes:
            visited: set[str] = set()
            cur: str | None = start_id
            while cur is not None:
                if cur in visited:
                    raise ValueError(
                        f"Cycle detected in OrgTree involving node '{cur}'."
                    )
                visited.add(cur)
                cur = self._nodes[cur].parent if cur in self._nodes else None

    def children(self, node_id: str) -> list[OrgNode]:
        """Return direct children of node_id."""
        return [n for n in self._nodes.values() if n.parent == node_id]

    def ancestors(self, node_id: str) -> list[OrgNode]:
        """Return ancestors of node_id from parent up to root (not including node_id itself)."""
        result: list[OrgNode] = []
        cur = self._nodes.get(node_id)
        if cur is None:
            return result
        parent_id = cur.parent
        while parent_id is not None:
            parent = self._nodes.get(parent_id)
            if parent is None:
                break
            result.append(parent)
            parent_id = parent.parent
        return result

    def descendant_deployments(self, node_id: str) -> list[OrgNode]:
        """Return all leaf-level descendant OrgNodes under node_id (inclusive if node is leaf)."""
        node = self._nodes.get(node_id)
        if node is None:
            return []
        if node.level == self.leaf_level:
            return [node]
        result: list[OrgNode] = []
        stack = self.children(node_id)
        while stack:
            n = stack.pop()
            if n.level == self.leaf_level:
                result.append(n)
            else:
                stack.extend(self.children(n.id))
        return result

    def roots(self) -> list[OrgNode]:
        """Return all root nodes (parent is None)."""
        return [n for n in self._nodes.values() if n.parent is None]

    def resolve_service_path(self, deployment_id: str) -> list[str]:
        """Return node names from root to deployment_id (inclusive)."""
        node = self._nodes.get(deployment_id)
        if node is None:
            return []
        chain: list[str] = [node.name]
        for ancestor in self.ancestors(deployment_id):
            chain.append(ancestor.name)
        return list(reversed(chain))

    def resolve_owner_ref(self, deployment_id: str) -> str | None:
        """Walk leaf->root; return the first (most-specific) owner_ref found."""
        node = self._nodes.get(deployment_id)
        if node is None:
            return None
        if node.owner_ref:
            return node.owner_ref
        for ancestor in self.ancestors(deployment_id):
            if ancestor.owner_ref:
                return ancestor.owner_ref
        return None

    def resolve_metadata(self, deployment_id: str) -> dict[str, Any]:
        """Merge metadata from root->leaf; leaf values win on key conflicts."""
        node = self._nodes.get(deployment_id)
        if node is None:
            return {}
        # Collect chain root->leaf
        chain: list[OrgNode] = [node] + self.ancestors(deployment_id)
        # chain is leaf->root; reverse to get root->leaf for merge (leaf wins)
        merged: dict[str, Any] = {}
        for n in reversed(chain):
            merged.update(n.metadata)
        return merged

    def get(self, node_id: str) -> OrgNode | None:
        """Return node by id, or None."""
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[OrgNode]:
        return list(self._nodes.values())

    def org_path(self, deployment_id: str) -> list[dict[str, Any]]:
        """Return the node's ancestry root→leaf as a list of serializable dicts.

        Each entry carries id/name/level/parent plus optional metadata (which
        holds integration routing keys like gitlab_project) and owner_ref. This
        is the payload a Node attaches to its heartbeat so the
        federated Hub can rebuild the catalog/hierarchy from registrations alone
        (no Hub-side catalog needed). Returns [] if the deployment is unknown.
        """
        node = self._nodes.get(deployment_id)
        if node is None:
            return []
        chain: list[OrgNode] = [node, *self.ancestors(deployment_id)]
        # chain is leaf→root; emit root→leaf.
        return [_org_node_to_payload(n) for n in reversed(chain)]

    @classmethod
    def from_registrations(
        cls,
        org_paths: list[list[dict[str, Any]]],
        leaf_level: str = "deployment",
    ) -> OrgTree:
        """Build an OrgTree from many nodes' org_path payloads (registrations).

        Dedupes nodes by id (last writer wins on conflicting attributes), drops
        entries missing required keys, and skips dangling parent references so a
        partial/garbled registration can never crash the Hub. This is how the
        federated Hub assembles the catalog purely from team-side heartbeats.
        """
        merged: dict[str, OrgNode] = {}
        for path in org_paths:
            for entry in path:
                node = _payload_to_org_node(entry)
                if node is not None:
                    merged[node.id] = node
        # Drop parent pointers that don't resolve so OrgTree validation passes
        # even when registrations arrive out of order or incomplete.
        known = set(merged)
        for node in merged.values():
            if node.parent is not None and node.parent not in known:
                node.parent = None
        return cls(list(merged.values()), leaf_level=leaf_level)


def _org_node_to_payload(node: OrgNode) -> dict[str, Any]:
    """Serialize an OrgNode to the compact heartbeat org_path entry shape.

    Emits ``metadata`` (the home for integration routing keys such as
    ``gitlab_project``) alongside the structural fields.
    """
    payload: dict[str, Any] = {
        "id": node.id,
        "name": node.name,
        "level": node.level,
        "parent": node.parent,
    }
    if node.metadata:
        payload["metadata"] = dict(node.metadata)
    if node.owner_ref:
        payload["owner_ref"] = node.owner_ref
    return payload


def _payload_to_org_node(entry: dict[str, Any]) -> OrgNode | None:
    """Build an OrgNode from a heartbeat org_path entry, or None if invalid."""
    node_id = entry.get("id")
    level = entry.get("level")
    if not node_id or not level:
        return None
    return OrgNode(
        id=str(node_id),
        name=str(entry.get("name") or node_id),
        level=str(level),
        parent=entry.get("parent"),
        metadata=dict(entry.get("metadata") or {}),
        owner_ref=entry.get("owner_ref"),
    )
