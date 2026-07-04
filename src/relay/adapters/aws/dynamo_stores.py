"""ContactStore, IncidentStore, and EscalationStateStore implementations over DynamoDB.

Single-table design (PK / SK — both String):
  pk=CONTACT#<contact_id>   sk=META           — Contact PII
  pk=INCIDENT#<id>          sk=META           — Incident record (timeline embedded)
  pk=ESC#<incident_id>      sk=STATE          — EscalationContext
  pk=ESC#<incident_id>      sk=DEADLINE       — escalation timeout deadline (swept)
  pk=SUPP#<key>#<bucket>    sk=STATE          — suppression window hit counter (TTL'd)

The node stack provisions one table named ``relay-<team>`` with:
    partition_key=pk (S), sort_key=sk (S)
All three stores accept the same ``table_name`` and reuse a single boto3 resource.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from relay.adapters.aws.endpoint import aws_resource_kwargs
from relay.config.schema import IgnoreRule
from relay.core.escalation import EscalationContext, EscalationPhase
from relay.core.model import Contact, Incident, IncidentState, RoutingRule, TimelineEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_SK_META = "META"
_SENTINEL_SK_STATE = "STATE"
_SENTINEL_SK_DEADLINE = "DEADLINE"
_SENTINEL_SK_COUNTER = "COUNTER"

# Escalation deadline lifecycle states (stored on the DEADLINE item).
_DEADLINE_PENDING = "PENDING"
_DEADLINE_FIRED = "FIRED"

# Incident listing GSIs (see specs/_active/0042-incident-listing-gsi).
# Two single-partition indices serve the two read patterns with one Query each:
#   incident-status-index — SPARSE: only open incidents carry gsi_open_pk, so a
#                           resolve overwrite (which omits it) evicts the row for
#                           free. Backs list_open_incidents.
#   incident-all-index    — every incident carries gsi_all_pk. Backs list_incidents
#                           (history + metrics).
# Both sort by created_at (ISO-8601 UTC sorts lexically), queried newest-first.
_INCIDENT_OPEN_INDEX = "incident-status-index"
_INCIDENT_ALL_INDEX = "incident-all-index"
_GSI_OPEN_PK = "gsi_open_pk"
_GSI_ALL_PK = "gsi_all_pk"
_GSI_OPEN_VALUE = "OPEN"
_GSI_ALL_VALUE = "INCIDENT"

# Single home for the open/terminal rule. An incident is "open" iff its state is
# one of these; terminal (RESOLVED/CLOSED) incidents are omitted from the sparse
# open index. _to_item derives gsi_open_pk from this set.
_OPEN_STATES = frozenset(
    {
        IncidentState.TRIGGERED,
        IncidentState.ACKNOWLEDGED,
        IncidentState.ESCALATED,
    }
)


def _serialize_datetime(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO-8601 string (UTC, with Z suffix)."""
    if dt is None:
        return None
    # Ensure the value is UTC-aware before formatting.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _deserialize_datetime(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string back to a timezone-aware datetime."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# ContactStore
# ---------------------------------------------------------------------------


class DynamoContactStore:
    """ContactStore backed by a DynamoDB table (single-table design).

    Key scheme:
        pk = ``CONTACT#<contact_id>``   sk = ``META``

    Implements the ContactStore protocol from relay.adapters.base.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(contact_id: str) -> str:
        return f"CONTACT#{contact_id}"

    def _key(self, contact_id: str) -> dict[str, str]:
        return {"pk": self._pk(contact_id), "sk": _SENTINEL_SK_META}

    @staticmethod
    def _to_item(contact: Contact) -> dict[str, Any]:
        item: dict[str, Any] = contact.model_dump(mode="json")
        item["pk"] = f"CONTACT#{contact.contact_id}"
        item["sk"] = _SENTINEL_SK_META
        return item

    @staticmethod
    def _from_item(item: dict[str, Any]) -> Contact:
        # Strip DynamoDB envelope keys before validation.
        data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        return Contact.model_validate(data)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_contact(self, contact_id: str) -> Contact | None:
        """Fetch contact PII by opaque ID.  Returns None if not found."""
        try:
            response = self._table.get_item(Key=self._key(contact_id))
        except ClientError:
            logger.exception("DynamoDB get_item failed for contact %s", contact_id)
            raise

        item = response.get("Item")
        if item is None:
            return None
        return self._from_item(item)

    def put_contact(self, contact: Contact) -> None:
        """Create or update a contact record."""
        item = self._to_item(contact)
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception("DynamoDB put_item failed for contact %s", contact.contact_id)
            raise

    def delete_contact(self, contact_id: str) -> None:
        """Hard-delete a contact and its PII."""
        try:
            self._table.delete_item(Key=self._key(contact_id))
        except ClientError:
            logger.exception("DynamoDB delete_item failed for contact %s", contact_id)
            raise

    def list_contacts(self) -> list[Contact]:
        """Return all contacts. Scans for items with a CONTACT# partition key.

        Fine for the modest contact counts a single team has; if a table ever
        grows large, replace with a GSI query.
        """
        contacts: list[Contact] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": {":p": "CONTACT#"},
        }
        try:
            while True:
                resp = self._table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    contacts.append(self._from_item(item))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                scan_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("DynamoDB scan failed listing contacts")
            raise
        contacts.sort(key=lambda c: c.name.lower())
        return contacts


# ---------------------------------------------------------------------------
# IncidentStore
# ---------------------------------------------------------------------------


class DynamoIncidentStore:
    """IncidentStore backed by a DynamoDB table (single-table design).

    Key scheme:
        pk = ``INCIDENT#<correlation_id>``   sk = ``META``

    The timeline list is stored inline in the META item.  For concurrent
    append use ``append_timeline_event`` which issues a DynamoDB UpdateItem
    with ``list_append`` to avoid clobbering a concurrent write.

    Implements the IncidentStore protocol from relay.adapters.base.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(correlation_id: str) -> str:
        return f"INCIDENT#{correlation_id}"

    def _key(self, correlation_id: str) -> dict[str, str]:
        return {"pk": self._pk(correlation_id), "sk": _SENTINEL_SK_META}

    @staticmethod
    def _to_item(incident: Incident) -> dict[str, Any]:
        item: dict[str, Any] = incident.model_dump(mode="json")
        item["pk"] = f"INCIDENT#{incident.correlation_id}"
        item["sk"] = _SENTINEL_SK_META
        # GSI keys derived purely from state (created_at is already a top-level
        # ISO-8601 string from model_dump). Every incident is in the all-index;
        # only open incidents are in the sparse open index — omitting gsi_open_pk
        # for terminal states keeps them out of it, so a resolve overwrite evicts
        # the row with no explicit REMOVE.
        item[_GSI_ALL_PK] = _GSI_ALL_VALUE
        if incident.state in _OPEN_STATES:
            item[_GSI_OPEN_PK] = _GSI_OPEN_VALUE
        return item

    @staticmethod
    def _from_item(item: dict[str, Any]) -> Incident:
        data = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        return Incident.model_validate(data)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_incident(self, correlation_id: str) -> Incident | None:
        """Fetch a live incident by correlation ID.  Returns None if not found."""
        try:
            response = self._table.get_item(Key=self._key(correlation_id))
        except ClientError:
            logger.exception(
                "DynamoDB get_item failed for incident %s", correlation_id
            )
            raise

        item = response.get("Item")
        if item is None:
            return None
        return self._from_item(item)

    def put_incident(self, incident: Incident) -> None:
        """Upsert a live incident record (overwrites entire item including timeline)."""
        item = self._to_item(incident)
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception(
                "DynamoDB put_item failed for incident %s", incident.correlation_id
            )
            raise

    def append_timeline_event(
        self, correlation_id: str, event: TimelineEvent
    ) -> None:
        """Append a timeline event atomically using DynamoDB UpdateItem with list_append.

        Avoids race conditions vs. full re-write: two concurrent Lambdas can
        each append their own event without clobbering each other.

        Expression:
            SET #tl = list_append(if_not_exists(#tl, :empty), :e),
                updated_at = :ts

        Args:
            correlation_id: The incident to update.
            event:          The timeline event to append.
        """
        event_item: dict[str, Any] = event.model_dump(mode="json")
        now_str = _serialize_datetime(datetime.now(UTC))
        try:
            self._table.update_item(
                Key=self._key(correlation_id),
                UpdateExpression=(
                    "SET #tl = list_append(if_not_exists(#tl, :empty), :e),"
                    " updated_at = :ts"
                ),
                ExpressionAttributeNames={"#tl": "timeline"},
                ExpressionAttributeValues={
                    ":e": [event_item],
                    ":empty": [],
                    ":ts": now_str,
                },
            )
        except ClientError:
            logger.exception(
                "DynamoDB update_item failed for append_timeline_event incident=%s event=%s",
                correlation_id,
                event.event_id,
            )
            raise

    def _query_incident_index(
        self,
        index_name: str,
        partition_attr: str,
        partition_value: str,
        account_id: str | None,
    ) -> list[Incident]:
        """Query an incident GSI newest-first, paginating fully.

        Both listing methods share this: a single-partition Query
        (``partition_attr = partition_value``) with ``ScanIndexForward=False`` so
        the ``created_at`` sort key returns newest-first, an optional
        ``account_id`` FilterExpression, and a ``LastEvaluatedKey`` loop to drain
        every page. Reads only the index's rows — never non-incident items.
        """
        query_kwargs: dict[str, Any] = {
            "IndexName": index_name,
            "KeyConditionExpression": Key(partition_attr).eq(partition_value),
            "ScanIndexForward": False,
        }
        if account_id is not None:
            query_kwargs["FilterExpression"] = Attr("account_id").eq(account_id)
        items: list[dict[str, Any]] = []
        try:
            while True:
                response = self._table.query(**query_kwargs)
                items.extend(response.get("Items", []))
                lek = response.get("LastEvaluatedKey")
                if not lek:
                    break
                query_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception(
                "DynamoDB query failed for incident index %s", index_name
            )
            raise
        return [self._from_item(item) for item in items]

    def list_open_incidents(self, account_id: str | None = None) -> list[Incident]:
        """List open incidents (state not RESOLVED/CLOSED), newest-first.

        Queries the sparse ``incident-status-index`` (only open incidents carry
        ``gsi_open_pk``), so the read touches open incident rows only — not
        terminal incidents and not other entity types in the shared table.

        Args:
            account_id: If provided, further filter by this AWS account ID.
        """
        return self._query_incident_index(
            _INCIDENT_OPEN_INDEX, _GSI_OPEN_PK, _GSI_OPEN_VALUE, account_id
        )

    def list_incidents(self, account_id: str | None = None) -> list[Incident]:
        """List ALL incidents (open + terminal), newest-first — history + metrics.

        Queries ``incident-all-index`` (every incident carries ``gsi_all_pk``), so
        the read touches incident rows only. Callers slice terminal-only (history)
        or compute over the full set (metrics) themselves.
        """
        return self._query_incident_index(
            _INCIDENT_ALL_INDEX, _GSI_ALL_PK, _GSI_ALL_VALUE, account_id
        )

    def purge_incidents(
        self,
        *,
        before: datetime | None = None,
        after: datetime | None = None,
        synthetic_only: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete incidents matching the given temporal and/or synthetic filter.

        Args:
            before:         Delete incidents with created_at <= before (inclusive).
            after:          Delete incidents with created_at >= after (inclusive).
            synthetic_only: When True, only delete incidents flagged as synthetic.
            dry_run:        If True, count matches without deleting anything.

        Returns:
            dict with keys: matched, deleted, synthetic, dry_run, companions_deleted,
            affected_tiles. ``deleted`` and ``companions_deleted`` are 0 when
            dry_run=True. ``affected_tiles`` is the list of distinct
            ``{account_id, app_name, environment, deployment_id}`` keys whose
            fleet aggregate may now be stale (one per deployment touched by the
            purge) so the caller can recompute/repair those FLEET# tiles. It is
            populated even on dry_run (so a preview can report what would shift).

        Notes:
            - If both ``before`` and ``after`` are given the range is [after, before]
              inclusive. An inverted range (after > before) yields zero matches.
            - Items with missing or unparseable ``created_at`` are skipped (not deleted).
            - Each matched incident's companion ESC#<id>/STATE and ESC#<id>/DEADLINE
              rows are also removed (cascade delete); missing companions are harmless.
        """
        # Short-circuit: inverted range means nothing can match.
        if before is not None and after is not None:
            # Make both timezone-aware for comparison.
            _before = before if before.tzinfo else before.replace(tzinfo=UTC)
            _after = after if after.tzinfo else after.replace(tzinfo=UTC)
            if _after > _before:
                return {"matched": 0, "deleted": 0, "synthetic": 0, "dry_run": dry_run, "companions_deleted": 0, "affected_tiles": []}

        # Scan all INCIDENT#.../META items.
        filter_expr = Attr("pk").begins_with("INCIDENT#") & Attr("sk").eq(_SENTINEL_SK_META)
        raw_items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {"FilterExpression": filter_expr}
        try:
            while True:
                response = self._table.scan(**scan_kwargs)
                raw_items.extend(response.get("Items", []))
                lek = response.get("LastEvaluatedKey")
                if not lek:
                    break
                scan_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("DynamoDB scan failed for purge_incidents")
            raise

        # Select items to purge.
        to_delete: list[dict[str, Any]] = []
        matched_synthetic = 0

        for item in raw_items:
            raw_ca = item.get("created_at")
            if raw_ca is None:
                logger.debug("purge_incidents: skipping item with no created_at pk=%s", item.get("pk"))
                continue
            try:
                created = _deserialize_datetime(raw_ca)
            except (ValueError, TypeError):
                logger.debug("purge_incidents: unparseable created_at %r on pk=%s", raw_ca, item.get("pk"))
                continue

            if created is None:
                continue

            # Temporal filter.
            if before is not None:
                _before = before if before.tzinfo else before.replace(tzinfo=UTC)
                if created > _before:
                    continue
            if after is not None:
                _after = after if after.tzinfo else after.replace(tzinfo=UTC)
                if created < _after:
                    continue

            # Synthetic filter.
            item_is_synthetic = bool(item.get("synthetic", False))
            if synthetic_only and not item_is_synthetic:
                continue

            if item_is_synthetic:
                matched_synthetic += 1
            to_delete.append(item)

        matched = len(to_delete)

        # Collect the distinct fleet-tile keys touched by this purge so the caller
        # can recompute their FLEET# aggregates (open_incident_count /
        # worst_severity) — purge deletes incident rows directly and bypasses the
        # apply_incident decrement path, so those tiles would otherwise stay stale.
        affected_tiles: list[dict[str, str | None]] = []
        _seen_tiles: set[tuple[str | None, str | None, str, str | None]] = set()
        for item in to_delete:
            account_id = item.get("account_id")
            app_name = item.get("app_name")
            environment = item.get("environment") or "unrouted"
            deployment_id = item.get("deployment_id")
            tile_key = (account_id, app_name, environment, deployment_id)
            if tile_key in _seen_tiles:
                continue
            _seen_tiles.add(tile_key)
            affected_tiles.append(
                {
                    "account_id": account_id,
                    "app_name": app_name,
                    "environment": environment,
                    "deployment_id": deployment_id,
                }
            )

        if dry_run or matched == 0:
            return {
                "matched": matched,
                "deleted": 0,
                "synthetic": matched_synthetic,
                "dry_run": dry_run,
                "companions_deleted": 0,
                "affected_tiles": affected_tiles,
            }

        # Perform deletions via batch_writer for the INCIDENT items; companion
        # ESC rows are deleted individually (they have different pk prefixes so
        # batch_writer would need mixed keys — explicit delete_item is simpler).
        companions_deleted = 0
        try:
            with self._table.batch_writer() as batch:
                for item in to_delete:
                    batch.delete_item(
                        Key={"pk": item["pk"], "sk": _SENTINEL_SK_META}
                    )
        except ClientError:
            logger.exception("DynamoDB batch_writer failed during purge_incidents")
            raise

        # Cascade: remove companion ESC#<id>/STATE and ESC#<id>/DEADLINE rows.
        for item in to_delete:
            # Extract the incident id from the pk (strip "INCIDENT#" prefix).
            incident_id = item["pk"][len("INCIDENT#"):]
            esc_pk = f"ESC#{incident_id}"
            for esc_sk in (_SENTINEL_SK_STATE, _SENTINEL_SK_DEADLINE):
                try:
                    self._table.delete_item(Key={"pk": esc_pk, "sk": esc_sk})
                    companions_deleted += 1
                except ClientError:
                    logger.exception(
                        "DynamoDB delete_item failed for companion ESC row incident=%s sk=%s",
                        incident_id,
                        esc_sk,
                    )
                    raise

        return {
            "matched": matched,
            "deleted": matched,
            "synthetic": matched_synthetic,
            "dry_run": dry_run,
            "companions_deleted": companions_deleted,
            "affected_tiles": affected_tiles,
        }


# ---------------------------------------------------------------------------
# EscalationStateStore
# ---------------------------------------------------------------------------


class DynamoEscalationStateStore:
    """EscalationStatePort backed by a DynamoDB table (single-table design).

    Key scheme:
        pk = ``ESC#<incident_id>``   sk = ``STATE``

    Serialisation:
        - EscalationPhase  → stored as its string value (e.g. "WAITING_ACK")
        - datetime fields  → stored as ISO-8601 strings (UTC, timezone-aware)
        - None fields      → stored as DynamoDB NULL or omitted
        - _timer_handle    → stored under key ``timer_handle`` (str | None)

    Implements the EscalationStatePort protocol from relay.core.escalation.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(incident_id: str) -> str:
        return f"ESC#{incident_id}"

    def _key(self, incident_id: str) -> dict[str, str]:
        return {"pk": self._pk(incident_id), "sk": _SENTINEL_SK_STATE}

    @staticmethod
    def _to_item(ctx: EscalationContext) -> dict[str, Any]:
        """Serialise an EscalationContext to a DynamoDB item dict."""
        item: dict[str, Any] = {
            "pk": f"ESC#{ctx.incident_id}",
            "sk": _SENTINEL_SK_STATE,
            "incident_id": ctx.incident_id,
            "policy_id": ctx.policy_id,
            "current_step_index": ctx.current_step_index,
            "phase": str(ctx.phase),  # StrEnum → plain string
            "paged_at": _serialize_datetime(ctx.paged_at),
            "last_escalated_at": _serialize_datetime(ctx.last_escalated_at),
            "ack_by": ctx.ack_by,
            "ack_at": _serialize_datetime(ctx.ack_at),
            "timer_handle": ctx._timer_handle,
        }
        # DynamoDB does not accept Python None in string/number attributes when
        # using the high-level resource API with default type serialiser.
        # Remove None values rather than sending them as NULL, so that
        # missing-key semantics work naturally on load.
        return {k: v for k, v in item.items() if v is not None}

    @staticmethod
    def _from_item(item: dict[str, Any]) -> EscalationContext:
        """Deserialise a DynamoDB item dict to an EscalationContext."""
        ctx = EscalationContext(
            incident_id=item["incident_id"],
            policy_id=item["policy_id"],
            current_step_index=int(item["current_step_index"]),
            phase=EscalationPhase(item["phase"]),
            paged_at=_deserialize_datetime(item.get("paged_at")),
            last_escalated_at=_deserialize_datetime(item.get("last_escalated_at")),
            ack_by=item.get("ack_by"),
            ack_at=_deserialize_datetime(item.get("ack_at")),
        )
        ctx._timer_handle = item.get("timer_handle")
        return ctx

    # ------------------------------------------------------------------
    # Public API (EscalationStatePort)
    # ------------------------------------------------------------------

    def load(self, incident_id: str) -> EscalationContext | None:
        """Load escalation context for the given incident.

        Returns None if no context has been persisted yet.
        """
        try:
            response = self._table.get_item(Key=self._key(incident_id))
        except ClientError:
            logger.exception(
                "DynamoDB get_item failed for escalation context incident=%s", incident_id
            )
            raise

        item = response.get("Item")
        if item is None:
            return None
        return self._from_item(item)

    def save(self, ctx: EscalationContext) -> None:
        """Persist (upsert) an escalation context."""
        item = self._to_item(ctx)
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception(
                "DynamoDB put_item failed for escalation context incident=%s",
                ctx.incident_id,
            )
            raise


# ---------------------------------------------------------------------------
# DynamoDeadlineTimer — durable, sweep-driven escalation timer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DueDeadline:
    """A PENDING escalation deadline whose ``fire_at`` has passed."""

    incident_id: str
    step_index: int
    fire_at: datetime | None = None


class DynamoDeadlineTimer:
    """EscalationTimerPort backed by a DynamoDB deadline row swept by the container.

    Replaces :class:`~relay.adapters.aws.scheduler_timer.SchedulerTimerPort` in the
    collapsed single-container runtime (collapsed-single-container plan §3 / Step 2).
    Instead of creating an EventBridge Scheduler one-shot that calls back the Node
    Lambda, ``schedule_timeout`` writes a *deadline row*::

        pk = ESC#<incident_id>   sk = DEADLINE
        { step_index, fire_at (ISO-8601 UTC), status: PENDING }

    The container's 30s sweep loop calls :meth:`query_due_deadlines` to find rows
    whose ``fire_at`` has passed and that are still ``PENDING``, claims each one
    atomically with :meth:`claim_deadline`, and invokes the same
    ``EscalationEngine.on_timeout`` entry point the Scheduler payload used to hit.

    There is at most one deadline row per incident: each escalation step overwrites
    it with a fresh ``PENDING`` deadline for the new step, so an advance naturally
    supersedes the prior deadline. On ack/resolve the row is deleted
    (:meth:`cancel_timeout`); on the terminal (EXHAUSTED) step the claimed row
    stays ``FIRED`` so the sweep does not re-fire it.

    Why this over Scheduler: durable across container restarts (a redeploy
    mid-incident doesn't drop the timer — the next sweep catches the deadline), no
    Scheduler IAM/PassRole/group subsystem, and trivially testable (set ``fire_at``
    in the past and tick the sweep once). The 30s granularity is irrelevant for
    human escalation windows measured in minutes.

    ``TimerPort`` stays an interface and ``SchedulerTimerPort`` remains in the tree
    as re-split insurance (plan §8).
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the timer.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
            clock:         Zero-arg callable returning the current UTC datetime.
                           Injected in tests; defaults to ``datetime.now(UTC)``.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(incident_id: str) -> str:
        return f"ESC#{incident_id}"

    def _key(self, incident_id: str) -> dict[str, str]:
        return {"pk": self._pk(incident_id), "sk": _SENTINEL_SK_DEADLINE}

    @staticmethod
    def _incident_id_from_pk(pk: str) -> str:
        return pk[len("ESC#") :] if pk.startswith("ESC#") else pk

    # ------------------------------------------------------------------
    # EscalationTimerPort implementation
    # ------------------------------------------------------------------

    def schedule_timeout(
        self, incident_id: str, step_index: int, delay_minutes: int
    ) -> str:
        """Write (overwrite) the PENDING deadline row for *incident_id*.

        Args:
            incident_id:   Unique incident identifier.
            step_index:    Zero-based escalation step this deadline belongs to;
                           the sweep passes it back to ``on_timeout`` so the
                           engine can detect stale callbacks.
            delay_minutes: How far in the future the deadline fires.

        Returns:
            The timer handle (the incident_id), passed back to ``cancel_timeout``.
        """
        fire_at: datetime = self._clock() + timedelta(minutes=delay_minutes)
        item = {
            **self._key(incident_id),
            "incident_id": incident_id,
            "step_index": step_index,
            "fire_at": _serialize_datetime(fire_at),
            "status": _DEADLINE_PENDING,
        }
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception(
                "DynamoDB put_item failed for escalation deadline incident=%s step=%s",
                incident_id,
                step_index,
            )
            raise
        logger.info(
            "Escalation deadline scheduled",
            extra={
                "incident_id": incident_id,
                "step_index": step_index,
                "fire_at": item["fire_at"],
                "delay_minutes": delay_minutes,
            },
        )
        return incident_id

    def cancel_timeout(self, timer_handle: str) -> None:
        """Delete the deadline row identified by *timer_handle* (the incident_id).

        Idempotent: deleting a missing row is a no-op (the deadline may already
        have fired and been superseded, or the incident was acked/resolved).
        """
        if not timer_handle:
            logger.debug("cancel_timeout called with empty handle — skipping")
            return
        try:
            self._table.delete_item(Key=self._key(timer_handle))
        except ClientError:
            logger.exception(
                "DynamoDB delete_item failed for escalation deadline incident=%s",
                timer_handle,
            )
            raise
        logger.info("Escalation deadline cancelled", extra={"incident_id": timer_handle})

    # ------------------------------------------------------------------
    # Sweep API (called by the container's SweepTimer)
    # ------------------------------------------------------------------

    def query_due_deadlines(self, now: datetime | None = None) -> list[DueDeadline]:
        """Return all PENDING deadlines whose ``fire_at`` is at or before *now*.

        ISO-8601 UTC strings sort lexicographically, so a string comparison on
        ``fire_at`` is correct. At ~200 apps with rare concurrent incidents the
        scan is cheap; a status GSI can replace it if the table grows.
        """
        now = now or self._clock()
        now_iso = _serialize_datetime(now)
        filter_expr = (
            Attr("sk").eq(_SENTINEL_SK_DEADLINE)
            & Attr("status").eq(_DEADLINE_PENDING)
            & Attr("fire_at").lte(now_iso)
        )
        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {"FilterExpression": filter_expr}
        try:
            while True:
                response = self._table.scan(**scan_kwargs)
                items.extend(response.get("Items", []))
                lek = response.get("LastEvaluatedKey")
                if not lek:
                    break
                scan_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("DynamoDB scan failed for query_due_deadlines")
            raise
        return [
            DueDeadline(
                incident_id=item.get("incident_id") or self._incident_id_from_pk(item["pk"]),
                step_index=int(item["step_index"]),
                fire_at=_deserialize_datetime(item.get("fire_at")),
            )
            for item in items
        ]

    def claim_deadline(self, incident_id: str, step_index: int) -> bool:
        """Atomically transition a deadline from PENDING→FIRED for one sweep.

        Returns True if this caller won the claim (and should fire the timeout),
        False if the row was already claimed/superseded/cancelled (so another
        sweep tick — or an advance/ack — got there first). Guards against
        double-firing when sweeps overlap or a deadline lingers after EXHAUSTED.
        """
        try:
            self._table.update_item(
                Key=self._key(incident_id),
                UpdateExpression="SET #s = :fired",
                ConditionExpression=(
                    Attr("status").eq(_DEADLINE_PENDING)
                    & Attr("step_index").eq(step_index)
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":fired": _DEADLINE_FIRED},
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            logger.exception(
                "DynamoDB update_item failed claiming deadline incident=%s step=%s",
                incident_id,
                step_index,
            )
            raise


class DynamoSuppressionStore:
    """Windowed hit counter for Node-side noise suppression (dedup / rate-limit).

    One logical alarm maps to a *fixed time window* bucket::

        pk = SUPP#<dedup_key>#<bucket>   sk = STATE
        { count (ADD-incremented), ttl (epoch seconds) }

    where ``bucket = floor(now_epoch / window_seconds)`` aligns all fires of the
    same alarm within a window onto one row. :meth:`increment_and_count` does an
    atomic ``ADD count :1`` and returns the post-increment count, so the caller
    (:class:`~relay.config.schema.SuppressionConfig`) can decide whether this
    fire exceeds the allowed ``max_per_window``.

    Rows self-expire via the table's ``ttl`` attribute (DynamoDB TTL), so there
    is no sweep to run and old windows never accumulate. The fixed-window model
    is intentionally simple: at human-incident volumes a sliding window buys
    nothing, and a fixed bucket needs exactly one atomic write per fire.

    Mirrors :class:`DynamoDeadlineTimer`'s construction (shared table, injectable
    session + clock) so it slots into the same composition root.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the suppression store.

        Args:
            table_name:    DynamoDB table name. Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
            clock:         Zero-arg callable returning the current UTC datetime.
                           Injected in tests; defaults to ``datetime.now(UTC)``.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _key(dedup_key: str, bucket: int) -> dict[str, str]:
        return {"pk": f"SUPP#{dedup_key}#{bucket}", "sk": _SENTINEL_SK_STATE}

    def increment_and_count(self, dedup_key: str, window_seconds: int) -> int:
        """Record one fire for *dedup_key* and return the window's hit count.

        Atomically increments the counter for the current fixed window and
        returns the post-increment value (1 for the first fire in the window).
        The row's TTL is set on first write to two windows out, so DynamoDB
        reaps it well after the window closes without any sweep.

        On any DynamoDB error this raises — the caller treats a failed
        suppression check as "do not suppress" so noise control never blocks a
        page (fail-open).
        """
        now_epoch = int(self._clock().timestamp())
        bucket = now_epoch // window_seconds
        # Expire two windows past the current bucket's end — comfortably after
        # the window closes, with slack for clock skew.
        ttl_epoch = (bucket + 2) * window_seconds
        response = self._table.update_item(
            Key=self._key(dedup_key, bucket),
            UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": ttl_epoch},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["count"])


# ---------------------------------------------------------------------------
# SettingsStore — small per-Hub key/value config (e.g. Teams webhook URL)
# ---------------------------------------------------------------------------


class DynamoSettingsStore:
    """Per-Hub settings stored as a single item (pk=SETTINGS#hub, sk=CONFIG).

    Holds operator-editable config that isn't code/config-as-code — e.g. the
    Teams Incoming Webhook URL a team registers via the Hub UI. Values are a
    flat dict of strings.
    """

    _PK = "SETTINGS#hub"
    _SK = "CONFIG"

    def __init__(self, table_name: str, boto3_resource: Any | None = None) -> None:
        resource = boto3_resource or boto3.resource("dynamodb", **aws_resource_kwargs())
        self._table = resource.Table(table_name)

    def get_all(self) -> dict[str, str]:
        try:
            resp = self._table.get_item(Key={"pk": self._PK, "sk": self._SK})
        except ClientError:
            logger.exception("DynamoDB get_item failed for settings")
            return {}
        item = resp.get("Item")
        if not item:
            return {}
        return {k: v for k, v in item.items() if k not in ("pk", "sk")}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.get_all().get(key, default)

    def set(self, key: str, value: str) -> None:
        try:
            self._table.update_item(
                Key={"pk": self._PK, "sk": self._SK},
                UpdateExpression="SET #k = :v",
                ExpressionAttributeNames={"#k": key},
                ExpressionAttributeValues={":v": value},
            )
        except ClientError:
            logger.exception("DynamoDB update_item failed setting %s", key)
            raise

    def delete(self, key: str) -> None:
        try:
            self._table.update_item(
                Key={"pk": self._PK, "sk": self._SK},
                UpdateExpression="REMOVE #k",
                ExpressionAttributeNames={"#k": key},
            )
        except ClientError:
            logger.exception("DynamoDB update_item failed removing %s", key)
            raise


# ---------------------------------------------------------------------------
# ScheduleStore — per-contact on-call availability + generated week schedules
# ---------------------------------------------------------------------------


class DynamoScheduleStore:
    """On-call availability (per contact) and generated week schedules.

    Item layout (same single table):
      pk=AVAIL#<contact_id>  sk=META   — {available, slots, ooo}
      pk=SCHED#<week_start>  sk=META   — {week_start, slots:[{date,shift,contact_id}]}
    """

    def __init__(self, table_name: str, boto3_resource: Any | None = None) -> None:
        resource = boto3_resource or boto3.resource("dynamodb", **aws_resource_kwargs())
        self._table = resource.Table(table_name)

    # --- availability ---

    def get_availability(self, contact_id: str) -> dict[str, Any] | None:
        try:
            resp = self._table.get_item(
                Key={"pk": f"AVAIL#{contact_id}", "sk": _SENTINEL_SK_META}
            )
        except ClientError:
            logger.exception("get_availability failed for %s", contact_id)
            return None
        item = resp.get("Item")
        if not item:
            return None
        return {k: v for k, v in item.items() if k not in ("pk", "sk")}

    def list_availability(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"FilterExpression": Attr("pk").begins_with("AVAIL#")}
        try:
            while True:
                resp = self._table.scan(**kwargs)
                for item in resp.get("Items", []):
                    out.append({k: v for k, v in item.items() if k not in ("pk", "sk")})
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("list_availability scan failed")
        return out

    def put_availability(self, contact_id: str, data: dict[str, Any]) -> None:
        item = dict(data)
        item["pk"] = f"AVAIL#{contact_id}"
        item["sk"] = _SENTINEL_SK_META
        item["contact_id"] = contact_id
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception("put_availability failed for %s", contact_id)
            raise

    # --- generated schedule ---

    def get_schedule(self, week_start: str) -> dict[str, Any] | None:
        try:
            resp = self._table.get_item(
                Key={"pk": f"SCHED#{week_start}", "sk": _SENTINEL_SK_META}
            )
        except ClientError:
            logger.exception("get_schedule failed for %s", week_start)
            return None
        item = resp.get("Item")
        if not item:
            return None
        return {k: v for k, v in item.items() if k not in ("pk", "sk")}

    def put_schedule(self, week_start: str, data: dict[str, Any]) -> None:
        item = dict(data)
        item["pk"] = f"SCHED#{week_start}"
        item["sk"] = _SENTINEL_SK_META
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception("put_schedule failed for %s", week_start)
            raise

    # --- ad-hoc overrides (cover-me), stored separately so they survive a
    #     re-auto-schedule. One item per week holds a list of overrides. ---

    def get_overrides(self, week_start: str) -> list[dict[str, Any]]:
        try:
            resp = self._table.get_item(
                Key={"pk": f"OVERRIDE#{week_start}", "sk": _SENTINEL_SK_META}
            )
        except ClientError:
            logger.exception("get_overrides failed for %s", week_start)
            return []
        item = resp.get("Item")
        if not item:
            return []
        return list(item.get("overrides", []) or [])

    def put_override(self, week_start: str, override: dict[str, Any]) -> None:
        """Upsert one override {date, shift, role, contact_id, by?}.

        Replaces any existing override for the same (date, shift, role).
        """
        overrides = [
            o for o in self.get_overrides(week_start)
            if not (o.get("date") == override.get("date")
                    and o.get("shift") == override.get("shift")
                    and o.get("role") == override.get("role"))
        ]
        overrides.append(override)
        try:
            self._table.put_item(Item={
                "pk": f"OVERRIDE#{week_start}", "sk": _SENTINEL_SK_META,
                "week_start": week_start, "overrides": overrides,
            })
        except ClientError:
            logger.exception("put_override failed for %s", week_start)
            raise

    def delete_override(self, week_start: str, date: str, shift: str, role: str) -> None:
        overrides = [
            o for o in self.get_overrides(week_start)
            if not (o.get("date") == date and o.get("shift") == shift
                    and o.get("role") == role)
        ]
        try:
            self._table.put_item(Item={
                "pk": f"OVERRIDE#{week_start}", "sk": _SENTINEL_SK_META,
                "week_start": week_start, "overrides": overrides,
            })
        except ClientError:
            logger.exception("delete_override failed for %s", week_start)
            raise


# ---------------------------------------------------------------------------
# DynamoIgnoreRuleStore — persistent ignore rules with trigger counters
# ---------------------------------------------------------------------------


class DynamoIgnoreRuleStore:
    """Persistent store for :class:`~relay.config.schema.IgnoreRule` objects.

    Single-table design (same relay-table as the other stores):

    * ``pk = IGNORE#<rule_id>``   ``sk = META``     — rule fields as JSON + top-level ``enabled`` bool
    * ``pk = IGNORE#<rule_id>``   ``sk = COUNTER``  — ``trigger_count`` (atomic ADD) + ``last_triggered_at``

    :meth:`put_rule` creates or replaces a rule; :meth:`record_trigger` atomically
    increments the counter so concurrent Lambda invocations never lose a count.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the ignore-rule store.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
            clock:         Zero-arg callable returning the current UTC datetime.
                           Injected in tests; defaults to ``datetime.now(UTC)``.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(rule_id: str) -> str:
        return f"IGNORE#{rule_id}"

    def _meta_key(self, rule_id: str) -> dict[str, str]:
        return {"pk": self._pk(rule_id), "sk": _SENTINEL_SK_META}

    def _counter_key(self, rule_id: str) -> dict[str, str]:
        return {"pk": self._pk(rule_id), "sk": _SENTINEL_SK_COUNTER}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put_rule(self, rule: IgnoreRule, rule_id: str | None = None) -> str:
        """Create or replace an ignore rule in the store.

        Args:
            rule:    The :class:`~relay.config.schema.IgnoreRule` to persist.
            rule_id: Explicit ID to use; when ``None`` a UUID4 is generated.

        Returns:
            The ``rule_id`` under which the rule was stored.
        """
        import uuid

        if rule_id is None:
            rule_id = rule.name or str(uuid.uuid4())
        item: dict[str, Any] = {
            **self._meta_key(rule_id),
            "rule_id": rule_id,
            "rule_json": rule.model_dump_json(),
            "enabled": rule.enabled,
        }
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception("DynamoDB put_item failed for ignore rule %s", rule_id)
            raise
        logger.info("Ignore rule stored", extra={"rule_id": rule_id})
        return rule_id

    def get_rule(self, rule_id: str) -> IgnoreRule | None:
        """Fetch an ignore rule by ID.  Returns None if not found."""
        try:
            response = self._table.get_item(Key=self._meta_key(rule_id))
        except ClientError:
            logger.exception("DynamoDB get_item failed for ignore rule %s", rule_id)
            raise

        item = response.get("Item")
        if item is None:
            return None

        return IgnoreRule.model_validate_json(item["rule_json"])

    def list_rules(self) -> list[tuple[str, IgnoreRule, int]]:
        """Return all stored ignore rules with their trigger counts.

        Returns:
            A list of ``(rule_id, IgnoreRule, trigger_count)`` tuples, one per
            rule, sorted by ``rule_id``.  The ``trigger_count`` is 0 when no
            :meth:`record_trigger` call has been made for that rule.
        """

        meta_items: dict[str, dict[str, Any]] = {}
        counter_items: dict[str, int] = {}

        scan_kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("IGNORE#")
        }
        try:
            while True:
                resp = self._table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    pk: str = item["pk"]
                    sk: str = item["sk"]
                    rule_id = pk[len("IGNORE#"):]
                    if sk == _SENTINEL_SK_META:
                        meta_items[rule_id] = item
                    elif sk == _SENTINEL_SK_COUNTER:
                        counter_items[rule_id] = int(item.get("trigger_count", 0))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                scan_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("DynamoDB scan failed listing ignore rules")
            raise

        result: list[tuple[str, IgnoreRule, int]] = []
        for rule_id, item in sorted(meta_items.items()):
            rule = IgnoreRule.model_validate_json(item["rule_json"])
            count = counter_items.get(rule_id, 0)
            result.append((rule_id, rule, count))
        return result

    def delete_rule(self, rule_id: str) -> None:
        """Hard-delete a rule and its trigger-counter row.

        Both the META and COUNTER items are removed. Idempotent: deleting
        a missing row is a no-op.
        """
        for key in (self._meta_key(rule_id), self._counter_key(rule_id)):
            try:
                self._table.delete_item(Key=key)
            except ClientError:
                logger.exception(
                    "DynamoDB delete_item failed for ignore rule %s sk=%s",
                    rule_id,
                    key["sk"],
                )
                raise
        logger.info("Ignore rule deleted", extra={"rule_id": rule_id})

    def record_trigger(self, rule_id: str) -> int:
        """Atomically increment the trigger counter for *rule_id*.

        Uses DynamoDB's ``ADD`` operation so concurrent callers never lose a
        count.  Sets ``last_triggered_at`` to the current UTC time.

        Returns:
            The post-increment trigger count.
        """
        now_iso = _serialize_datetime(self._clock())
        try:
            response = self._table.update_item(
                Key=self._counter_key(rule_id),
                UpdateExpression="ADD #tc :one SET #lta = :now",
                ExpressionAttributeNames={
                    "#tc": "trigger_count",
                    "#lta": "last_triggered_at",
                },
                ExpressionAttributeValues={":one": 1, ":now": now_iso},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError:
            logger.exception(
                "DynamoDB update_item failed for ignore rule trigger %s", rule_id
            )
            raise
        return int(response["Attributes"]["trigger_count"])


# ---------------------------------------------------------------------------
# DynamoRoutingRuleStore — persistent routing rules with match counters
# ---------------------------------------------------------------------------


class DynamoRoutingRuleStore:
    """Persistent store for :class:`~relay.core.model.RoutingRule` objects.

    Single-table design (same relay-table as the other stores):

    * ``pk = ROUTING#<rule_id>``  ``sk = META``     — rule fields as JSON + top-level
      ``priority`` (int) and ``enabled`` bool (store-level toggle, not a model field)
    * ``pk = ROUTING#<rule_id>``  ``sk = COUNTER``  — ``match_count`` (atomic ADD) +
      ``last_matched_at``

    :meth:`put_rule` creates or replaces a rule; :meth:`record_match` atomically
    increments the counter so concurrent Lambda invocations never lose a count.
    :meth:`list_rules` returns tuples sorted by *priority ascending* (lower number
    = higher priority, evaluated first) — callers can rely on the returned order.
    """

    def __init__(
        self,
        table_name: str = "relay-table",
        boto3_session: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the routing-rule store.

        Args:
            table_name:    DynamoDB table name.  Defaults to ``"relay-table"``.
            boto3_session: Optional custom session for cross-account or testing.
            clock:         Zero-arg callable returning the current UTC datetime.
                           Injected in tests; defaults to ``datetime.now(UTC)``.
        """
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs()
        ).Table(table_name)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(rule_id: str) -> str:
        return f"ROUTING#{rule_id}"

    def _meta_key(self, rule_id: str) -> dict[str, str]:
        return {"pk": self._pk(rule_id), "sk": _SENTINEL_SK_META}

    def _counter_key(self, rule_id: str) -> dict[str, str]:
        return {"pk": self._pk(rule_id), "sk": _SENTINEL_SK_COUNTER}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put_rule(
        self,
        rule: RoutingRule,
        rule_id: str | None = None,
        *,
        enabled: bool = True,
    ) -> str:
        """Create or replace a routing rule in the store.

        Args:
            rule:     The :class:`~relay.core.model.RoutingRule` to persist.
            rule_id:  Explicit ID to use; when ``None`` uses ``rule.rule_id`` if
                      set, otherwise a UUID4 is generated.
            enabled:  Store-level enable/disable flag (default ``True``).
                      The rule_json is the pure RoutingRule; ``enabled`` is a
                      top-level attribute that the UI can toggle without rewriting
                      the entire rule.

        Returns:
            The ``rule_id`` under which the rule was stored.
        """
        import uuid

        if rule_id is None:
            rule_id = rule.rule_id if rule.rule_id else str(uuid.uuid4())
        item: dict[str, Any] = {
            **self._meta_key(rule_id),
            "rule_id": rule_id,
            "rule_json": rule.model_dump_json(),
            "priority": rule.priority,
            "enabled": enabled,
        }
        try:
            self._table.put_item(Item=item)
        except ClientError:
            logger.exception("DynamoDB put_item failed for routing rule %s", rule_id)
            raise
        logger.info("Routing rule stored", extra={"rule_id": rule_id})
        return rule_id

    def get_rule(self, rule_id: str) -> RoutingRule | None:
        """Fetch a routing rule by ID.  Returns None if not found."""
        try:
            response = self._table.get_item(Key=self._meta_key(rule_id))
        except ClientError:
            logger.exception("DynamoDB get_item failed for routing rule %s", rule_id)
            raise

        item = response.get("Item")
        if item is None:
            return None
        return RoutingRule.model_validate_json(item["rule_json"])

    def list_rules(self) -> list[tuple[str, RoutingRule, int, bool]]:
        """Return all stored routing rules with their match counts.

        Returns:
            A list of ``(rule_id, RoutingRule, match_count, enabled)`` tuples,
            one per rule, **sorted by priority ascending** (lower priority number
            = higher precedence, evaluated first by the classifier).  When two
            rules share the same priority they are sub-sorted by ``rule_id`` for
            deterministic ordering.  The ``match_count`` is 0 when no
            :meth:`record_match` call has been made for that rule.
        """
        meta_items: dict[str, dict[str, Any]] = {}
        counter_items: dict[str, int] = {}

        scan_kwargs: dict[str, Any] = {
            "FilterExpression": Attr("pk").begins_with("ROUTING#")
        }
        try:
            while True:
                resp = self._table.scan(**scan_kwargs)
                for item in resp.get("Items", []):
                    pk: str = item["pk"]
                    sk: str = item["sk"]
                    rule_id = pk[len("ROUTING#"):]
                    if sk == _SENTINEL_SK_META:
                        meta_items[rule_id] = item
                    elif sk == _SENTINEL_SK_COUNTER:
                        counter_items[rule_id] = int(item.get("match_count", 0))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                scan_kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.exception("DynamoDB scan failed listing routing rules")
            raise

        result: list[tuple[str, RoutingRule, int, bool]] = []
        for rule_id, item in meta_items.items():
            rule = RoutingRule.model_validate_json(item["rule_json"])
            count = counter_items.get(rule_id, 0)
            enabled = bool(item.get("enabled", True))
            result.append((rule_id, rule, count, enabled))
        # Sort by priority ascending, then rule_id for deterministic tie-breaking.
        result.sort(key=lambda t: (t[1].priority, t[0]))
        return result

    def delete_rule(self, rule_id: str) -> None:
        """Hard-delete a rule and its match-counter row.

        Both the META and COUNTER items are removed. Idempotent: deleting
        a missing row is a no-op.
        """
        for key in (self._meta_key(rule_id), self._counter_key(rule_id)):
            try:
                self._table.delete_item(Key=key)
            except ClientError:
                logger.exception(
                    "DynamoDB delete_item failed for routing rule %s sk=%s",
                    rule_id,
                    key["sk"],
                )
                raise
        logger.info("Routing rule deleted", extra={"rule_id": rule_id})

    def record_match(self, rule_id: str) -> int:
        """Atomically increment the match counter for *rule_id*.

        Uses DynamoDB's ``ADD`` operation so concurrent callers never lose a
        count.  Sets ``last_matched_at`` to the current UTC time.

        Returns:
            The post-increment match count.
        """
        now_iso = _serialize_datetime(self._clock())
        try:
            response = self._table.update_item(
                Key=self._counter_key(rule_id),
                UpdateExpression="ADD #mc :one SET #lma = :now",
                ExpressionAttributeNames={
                    "#mc": "match_count",
                    "#lma": "last_matched_at",
                },
                ExpressionAttributeValues={":one": 1, ":now": now_iso},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError:
            logger.exception(
                "DynamoDB update_item failed for routing rule match %s", rule_id
            )
            raise
        return int(response["Attributes"]["match_count"])

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        """Toggle the ``enabled`` flag on a routing rule's META item.

        Updates only the ``enabled`` attribute; the rule_json and all other
        fields are left untouched.  Useful for UI-driven enable/disable toggles
        without a full rule rewrite.

        Args:
            rule_id: The rule to update.
            enabled: ``True`` to enable the rule, ``False`` to disable it.
        """
        try:
            self._table.update_item(
                Key=self._meta_key(rule_id),
                UpdateExpression="SET #en = :val",
                ExpressionAttributeNames={"#en": "enabled"},
                ExpressionAttributeValues={":val": enabled},
            )
        except ClientError:
            logger.exception(
                "DynamoDB update_item failed setting enabled=%s for routing rule %s",
                enabled,
                rule_id,
            )
            raise
        logger.info(
            "Routing rule enabled flag updated",
            extra={"rule_id": rule_id, "enabled": enabled},
        )
