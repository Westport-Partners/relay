"""relay.hub.fleet_store — DynamoDB-backed authoritative fleet state.

Single-table design reusing the Hub's DynamoDB table.  Key scheme:

    pk = FLEET#<account_id>#<app_name>   sk = STATE

One item per (account_id, app_name) holding last_heartbeat_at,
open_incident_count, worst_severity, registered_at.

Environment variable:
    RELAY_FLEET_TABLE — name of the DynamoDB table for fleet state.
    Defaults to the value of RELAY_DYNAMO_INCIDENTS_TABLE if unset, or
    "relay-fleet" as a fallback.

TODO: hub_stack.py does not yet provision a dedicated fleet table.  Either:
    1. Add a ``relay-fleet`` table to hub_stack.py (PK=pk:S, SK=sk:S), or
    2. Reuse RELAY_DYNAMO_INCIDENTS_TABLE (single-table; FLEET# prefix avoids
       collision with INCIDENT#/CONTACT#/ESC# prefixes already in use).
    Currently defaults to RELAY_DYNAMO_INCIDENTS_TABLE with FLEET# prefixed
    keys so no extra table provisioning is required.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from relay.adapters.aws.endpoint import aws_resource_kwargs
from relay.core.model import Incident, IncidentState, OrgTree, Severity
from relay.hub.health import (
    DEFAULT_CADENCE_SECONDS,
    FleetTile,
    Liveness,
    liveness_from_heartbeat,
    worst_of,
)

logger = logging.getLogger(__name__)

_SENTINEL_SK_STATE = "STATE"

# Worst-of severity tracking relies on SEV* strings sorting lexicographically in
# severity order: "SEV1" < "SEV2" < "SEV3" < "SEV4", i.e. *more severe* sorts
# *smaller*. apply_incident exploits this with a conditional DynamoDB SET
# ("overwrite worst_severity only when stored > incoming") so concurrent writers
# converge on the most-severe value without a read-modify-write race.


def _serialize_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _deserialize_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class FleetStore:
    """DynamoDB-backed authoritative state for the fleet big-board.

    Injected boto3_session allows moto-based testing without real AWS.
    """

    def __init__(
        self,
        table_name: str | None = None,
        boto3_session: Any | None = None,
        cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            table_name:     DynamoDB table name.  Falls back to env vars (see module docstring).
            boto3_session:  Optional session for cross-account or test injection.
            cadence_seconds: Heartbeat cadence for liveness derivation.
            clock:          Callable returning current UTC datetime (injectable for tests).
        """
        if table_name is None:
            # RELAY_FLEET_TABLE_NAME is what hub_stack.py sets; keep older
            # aliases as fallbacks. Default matches the table the stack creates.
            table_name = (
                os.environ.get("RELAY_FLEET_TABLE_NAME")
                or os.environ.get("RELAY_FLEET_TABLE")
                or os.environ.get("RELAY_DYNAMO_INCIDENTS_TABLE")
                or "relay-hub-fleet"
            )
        region = (
            os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        )
        session: boto3.session.Session = boto3_session or boto3.session.Session()
        self._table = session.resource(
            "dynamodb", **aws_resource_kwargs(region)
        ).Table(table_name)
        self._cadence = cadence_seconds
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pk(account_id: str, app_name: str, environment: str = "unrouted", deployment_id: str | None = None) -> str:
        dep_id = deployment_id if deployment_id is not None else app_name
        return f"FLEET#{environment}#{dep_id}"

    def _key(self, account_id: str, app_name: str, environment: str = "unrouted", deployment_id: str | None = None) -> dict[str, str]:
        return {
            "pk": self._pk(account_id, app_name, environment, deployment_id),
            "sk": _SENTINEL_SK_STATE,
        }

    @staticmethod
    def _to_item(
        account_id: str,
        app_name: str,
        last_heartbeat_at: datetime | None,
        open_incident_count: int,
        worst_severity: Severity | None,
        registered_at: datetime,
        has_acked: bool,
        environment: str = "unrouted",
        deployment_id: str | None = None,
        service_path: list[str] | None = None,
    ) -> dict[str, Any]:
        dep_id = deployment_id if deployment_id is not None else app_name
        item: dict[str, Any] = {
            "pk": FleetStore._pk(account_id, app_name, environment, dep_id),
            "sk": _SENTINEL_SK_STATE,
            "account_id": account_id,
            "app_name": app_name,
            "environment": environment,
            "deployment_id": dep_id,
            "service_path": service_path or [],
            "open_incident_count": open_incident_count,
            "registered_at": _serialize_dt(registered_at),
            "has_acked": has_acked,
        }
        if last_heartbeat_at is not None:
            item["last_heartbeat_at"] = _serialize_dt(last_heartbeat_at)
        if worst_severity is not None:
            item["worst_severity"] = worst_severity.value
        return {k: v for k, v in item.items() if v is not None}

    def _tile_from_item(self, item: dict[str, Any]) -> FleetTile:
        account_id: str = item["account_id"]
        app_name: str = item["app_name"]
        environment: str = item.get("environment", "unrouted")
        deployment_id: str = item.get("deployment_id", app_name)
        service_path: list[str] = item.get("service_path", [])
        org_path: list[dict] = item.get("org_path", []) or []
        metadata: dict = item.get("metadata", {}) or {}
        on_call = item.get("on_call") or None
        last_heartbeat_at = _deserialize_dt(item.get("last_heartbeat_at"))
        registered_at = _deserialize_dt(item.get("registered_at")) or self._clock()
        open_count = int(item.get("open_incident_count", 0))
        sev_str = item.get("worst_severity")
        worst_sev = Severity(sev_str) if sev_str else None
        has_acked = bool(item.get("has_acked", False))

        liveness = liveness_from_heartbeat(
            last_heartbeat_at,
            cadence_seconds=self._cadence,
            clock=self._clock,
        )
        status = worst_of(
            liveness,
            open_incidents=open_count,
            worst_severity=worst_sev,
            has_acked=has_acked,
        )

        return FleetTile(
            account_id=account_id,
            app_name=app_name,
            environment=environment,
            deployment_id=deployment_id,
            service_path=service_path,
            org_path=org_path,
            metadata=metadata,
            on_call=on_call,
            status=status,
            liveness=liveness,
            open_incidents=open_count,
            worst_severity=worst_sev,
            last_heartbeat_at=last_heartbeat_at,
            registered_at=registered_at,
            last_updated=self._clock(),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """Record a heartbeat for (account_id, app_name), self-registering on first sight.

        ``org_path`` is the node's org ancestry (root→leaf node dicts); it is
        persisted on the item so the Hub can rebuild the catalog/hierarchy from
        registrations alone (see :meth:`build_org_tree`). The federated Hub thus
        stores no static catalog — org data always comes from the team side.

        ``metadata`` is free-form deployment meta (owner, gitlab_project, region,
        and — when Node tag enrichment is on — aws_tags). ``on_call`` is the
        owning team's pushed on-call snapshot, used by a federated Hub that has
        no access to the team's schedule. Both are optional; absent leaves any
        previously-stored value untouched (so a snapshot-free Node never clobbers
        a richer record).
        """
        dep_id = deployment_id if deployment_id is not None else app_name
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ts_str = _serialize_dt(ts)
        registered_str = _serialize_dt(ts)  # first heartbeat = registration time

        # metadata/on_call are only SET when the heartbeat actually carried them,
        # so an older Node (or one with enrichment off) doesn't wipe a value a
        # richer heartbeat previously stored.
        extra_set = ""
        extra_vals: dict[str, Any] = {}
        if metadata is not None:
            extra_set += " metadata = :md,"
            extra_vals[":md"] = metadata
        if on_call is not None:
            extra_set += " on_call = :oc,"
            extra_vals[":oc"] = on_call

        try:
            response = self._table.update_item(
                Key=self._key(account_id, app_name, environment, dep_id),
                UpdateExpression=(
                    "SET account_id = :aid, app_name = :aname,"
                    " environment = :env, deployment_id = :dep_id,"
                    " service_path = :sp,"
                    " org_path = :op,"
                    f"{extra_set}"
                    " last_heartbeat_at = :hb,"
                    " registered_at = if_not_exists(registered_at, :reg),"
                    " open_incident_count = if_not_exists(open_incident_count, :zero),"
                    " has_acked = if_not_exists(has_acked, :false)"
                ),
                ExpressionAttributeValues={
                    ":aid": account_id,
                    ":aname": app_name,
                    ":env": environment,
                    ":dep_id": dep_id,
                    ":sp": service_path or [],
                    ":op": org_path or [],
                    ":hb": ts_str,
                    ":reg": registered_str,
                    ":zero": 0,
                    ":false": False,
                    **extra_vals,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError:
            logger.exception(
                "DynamoDB update_item failed for record_heartbeat %s/%s",
                account_id,
                app_name,
            )
            raise

        return self._tile_from_item(response["Attributes"])

    def apply_incident(self, incident: Incident) -> FleetTile:
        """Update open_incident_count and worst_severity from an incident event.

        Increments open count on TRIGGERED/ESCALATED; decrements on RESOLVED/CLOSED.
        Sets has_acked=True on ACKNOWLEDGED.
        Self-registers the app if not yet present (using incident.updated_at as registration ts).
        """
        account_id = incident.account_id
        app_name = incident.app_name
        environment = incident.environment
        deployment_id = incident.deployment_id
        service_path = incident.service_path
        now_str = _serialize_dt(self._clock())

        if incident.state in (IncidentState.TRIGGERED, IncidentState.ESCALATED):
            # Increment open count and track worst severity.
            sev_val = incident.severity.value
            try:
                response = self._table.update_item(
                    Key=self._key(account_id, app_name, environment, deployment_id),
                    UpdateExpression=(
                        "SET account_id = :aid, app_name = :aname,"
                        " environment = :env, deployment_id = :dep_id, service_path = :sp,"
                        " open_incident_count = if_not_exists(open_incident_count, :zero) + :one,"
                        " worst_severity = if_not_exists(worst_severity, :sev),"
                        " registered_at = if_not_exists(registered_at, :reg),"
                        " has_acked = if_not_exists(has_acked, :false)"
                    ),
                    ExpressionAttributeValues={
                        ":aid": account_id,
                        ":aname": app_name,
                        ":env": environment,
                        ":dep_id": deployment_id,
                        ":sp": service_path,
                        ":zero": 0,
                        ":one": 1,
                        ":sev": sev_val,
                        ":reg": now_str,
                        ":false": False,
                    },
                    ReturnValues="ALL_NEW",
                )
                item = response["Attributes"]
                # Tighten worst_severity to the more-severe value with a *conditional*
                # SET so two concurrent TRIGGERED writers (SQS consumer + in-process
                # pipeline) can't clobber each other's recompute (the read-modify-write
                # race the collapse introduced — plan §11 Step 3). SEV strings sort
                # lexicographically in severity order (SEV1 < SEV2 < SEV3 < SEV4), so
                # "more severe" == "lexicographically smaller": only overwrite when the
                # stored value is absent or strictly greater than the incoming one.
                new_sev = incident.severity.value
                try:
                    response = self._table.update_item(
                        Key=self._key(account_id, app_name, environment, deployment_id),
                        UpdateExpression="SET worst_severity = :sev",
                        ConditionExpression=(
                            "attribute_not_exists(worst_severity) OR worst_severity > :sev"
                        ),
                        ExpressionAttributeValues={":sev": new_sev},
                        ReturnValues="ALL_NEW",
                    )
                    item = response["Attributes"]
                except ClientError as exc:
                    # ConditionalCheckFailed = the stored severity is already as/more
                    # severe; keep the item from the first update_item (current worst).
                    if (
                        exc.response.get("Error", {}).get("Code")
                        != "ConditionalCheckFailedException"
                    ):
                        raise
            except ClientError:
                logger.exception(
                    "DynamoDB update_item failed for apply_incident TRIGGERED %s/%s",
                    account_id,
                    app_name,
                )
                raise

            # Merge incident deployment_metadata + tags into persisted metadata.
            # Non-fatal: tag stamping must never fail incident ingest.
            try:
                incoming: dict[str, Any] = {}
                dm = incident.deployment_metadata if incident.deployment_metadata else {}
                if dm:
                    incoming.update(dm)
                raw_tags = incident.tags if incident.tags else {}
                if raw_tags:
                    incoming["resource_tags"] = dict(raw_tags)
                if incoming:
                    current_metadata: dict[str, Any] = item.get("metadata") or {}
                    # Heartbeat-supplied keys (owner, on_call-derived, etc.) survive;
                    # incident-supplied keys win on collision (deployment context is authoritative).
                    merged = {**current_metadata, **incoming}
                    response = self._table.update_item(
                        Key=self._key(account_id, app_name, environment, deployment_id),
                        UpdateExpression="SET metadata = :md",
                        ExpressionAttributeValues={":md": merged},
                        ReturnValues="ALL_NEW",
                    )
                    item = response["Attributes"]
            except ClientError:
                logger.exception(
                    "metadata merge failed for apply_incident TRIGGERED %s/%s; continuing",
                    account_id,
                    app_name,
                )

        elif incident.state == IncidentState.ACKNOWLEDGED:
            try:
                response = self._table.update_item(
                    Key=self._key(account_id, app_name, environment, deployment_id),
                    UpdateExpression=(
                        "SET account_id = :aid, app_name = :aname,"
                        " environment = :env, deployment_id = :dep_id, service_path = :sp,"
                        " has_acked = :true,"
                        " registered_at = if_not_exists(registered_at, :reg),"
                        " open_incident_count = if_not_exists(open_incident_count, :zero)"
                    ),
                    ExpressionAttributeValues={
                        ":aid": account_id,
                        ":aname": app_name,
                        ":env": environment,
                        ":dep_id": deployment_id,
                        ":sp": service_path,
                        ":true": True,
                        ":reg": now_str,
                        ":zero": 0,
                    },
                    ReturnValues="ALL_NEW",
                )
                item = response["Attributes"]
            except ClientError:
                logger.exception(
                    "DynamoDB update_item failed for apply_incident ACKNOWLEDGED %s/%s",
                    account_id,
                    app_name,
                )
                raise

        elif incident.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
            try:
                response = self._table.update_item(
                    Key=self._key(account_id, app_name, environment, deployment_id),
                    UpdateExpression=(
                        "SET account_id = :aid, app_name = :aname,"
                        " environment = :env, deployment_id = :dep_id, service_path = :sp,"
                        " open_incident_count = if_not_exists(open_incident_count, :zero),"
                        " registered_at = if_not_exists(registered_at, :reg),"
                        " has_acked = if_not_exists(has_acked, :false)"
                    ),
                    ExpressionAttributeValues={
                        ":aid": account_id,
                        ":aname": app_name,
                        ":env": environment,
                        ":dep_id": deployment_id,
                        ":sp": service_path,
                        ":zero": 0,
                        ":reg": now_str,
                        ":false": False,
                    },
                    ConditionExpression="attribute_exists(pk)",
                    ReturnValues="ALL_NEW",
                )
                item = response["Attributes"]
                # Decrement open count; floor at 0.
                current_count = int(item.get("open_incident_count", 0))
                new_count = max(0, current_count - 1)
                # Clear worst_severity when count reaches 0.
                if new_count == 0:
                    response = self._table.update_item(
                        Key=self._key(account_id, app_name, environment, deployment_id),
                        UpdateExpression=(
                            "SET open_incident_count = :zero, has_acked = :false"
                            " REMOVE worst_severity"
                        ),
                        ExpressionAttributeValues={
                            ":zero": 0,
                            ":false": False,
                        },
                        ReturnValues="ALL_NEW",
                    )
                else:
                    response = self._table.update_item(
                        Key=self._key(account_id, app_name, environment, deployment_id),
                        UpdateExpression="SET open_incident_count = :c",
                        ExpressionAttributeValues={":c": new_count},
                        ReturnValues="ALL_NEW",
                    )
                item = response["Attributes"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # App not in fleet table yet — nothing to resolve.
                    logger.debug(
                        "apply_incident RESOLVED for unregistered app %s/%s; skipping",
                        account_id,
                        app_name,
                    )
                    # Return a synthetic unknown tile.
                    now = self._clock()
                    return FleetTile(
                        account_id=account_id,
                        app_name=app_name,
                        environment=environment,
                        deployment_id=deployment_id,
                        service_path=service_path,
                        status="grey",
                        liveness=Liveness.UNKNOWN,
                        open_incidents=0,
                        worst_severity=None,
                        last_heartbeat_at=None,
                        registered_at=now,
                        last_updated=now,
                    )
                logger.exception(
                    "DynamoDB update_item failed for apply_incident RESOLVED %s/%s",
                    account_id,
                    app_name,
                )
                raise
        else:
            # Unknown state — just ensure the app is registered.
            now_str2 = _serialize_dt(self._clock())
            try:
                response = self._table.update_item(
                    Key=self._key(account_id, app_name, environment, deployment_id),
                    UpdateExpression=(
                        "SET account_id = :aid, app_name = :aname,"
                        " environment = :env, deployment_id = :dep_id, service_path = :sp,"
                        " registered_at = if_not_exists(registered_at, :reg),"
                        " open_incident_count = if_not_exists(open_incident_count, :zero),"
                        " has_acked = if_not_exists(has_acked, :false)"
                    ),
                    ExpressionAttributeValues={
                        ":aid": account_id,
                        ":aname": app_name,
                        ":env": environment,
                        ":dep_id": deployment_id,
                        ":sp": service_path,
                        ":reg": now_str2,
                        ":zero": 0,
                        ":false": False,
                    },
                    ReturnValues="ALL_NEW",
                )
                item = response["Attributes"]
            except ClientError:
                logger.exception(
                    "DynamoDB update_item failed for apply_incident UNKNOWN state %s/%s",
                    account_id,
                    app_name,
                )
                raise

        return self._tile_from_item(item)

    def get_tile(self, account_id: str, app_name: str) -> FleetTile | None:
        """Fetch a single tile from DynamoDB.  Returns None if not registered."""
        try:
            response = self._table.get_item(Key=self._key(account_id, app_name))
        except ClientError:
            logger.exception(
                "DynamoDB get_item failed for fleet tile %s/%s", account_id, app_name
            )
            raise
        item = response.get("Item")
        if item is None:
            return None
        return self._tile_from_item(item)

    def list_tiles(self) -> list[FleetTile]:
        """Scan all FLEET# items and return as FleetTile list."""
        from boto3.dynamodb.conditions import Attr

        try:
            response = self._table.scan(
                FilterExpression=Attr("pk").begins_with("FLEET#")
                & Attr("sk").eq(_SENTINEL_SK_STATE)
            )
        except ClientError:
            logger.exception("DynamoDB scan failed for list_tiles")
            raise

        items = response.get("Items", [])
        # Handle pagination.
        while "LastEvaluatedKey" in response:
            try:
                response = self._table.scan(
                    FilterExpression=Attr("pk").begins_with("FLEET#")
                    & Attr("sk").eq(_SENTINEL_SK_STATE),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
            except ClientError:
                logger.exception("DynamoDB scan pagination failed for list_tiles")
                raise
            items.extend(response.get("Items", []))

        return [self._tile_from_item(item) for item in items]

    def hydrate(self) -> list[FleetTile]:
        """Load all fleet tiles from DynamoDB.  Alias for list_tiles() for clarity."""
        return self.list_tiles()

    def build_org_tree(self) -> OrgTree:
        """Assemble an OrgTree from the org_path payloads on all FLEET# items.

        The Hub stores NO static catalog: every node's org ancestry arrives on
        its heartbeat (org_path) and is persisted per item. This scans those
        payloads and reconstructs the hierarchy, deduping nodes by id. A Hub
        with no registrations yet returns an empty tree (rollup is simply empty).
        """
        from boto3.dynamodb.conditions import Attr

        org_paths: list[list[dict[str, Any]]] = []
        try:
            response = self._table.scan(
                FilterExpression=Attr("pk").begins_with("FLEET#")
                & Attr("sk").eq(_SENTINEL_SK_STATE)
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    FilterExpression=Attr("pk").begins_with("FLEET#")
                    & Attr("sk").eq(_SENTINEL_SK_STATE),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
        except ClientError:
            logger.exception("DynamoDB scan failed for build_org_tree")
            raise

        for item in items:
            path = item.get("org_path")
            if isinstance(path, list) and path:
                org_paths.append(path)

        return OrgTree.from_registrations(org_paths)
