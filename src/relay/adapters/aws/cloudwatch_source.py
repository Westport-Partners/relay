"""AlertSource implementation that parses CloudWatch Alarm State Change EventBridge events into the Incident model. Supports standard alarms and CloudWatch Synthetics canary failures via the same seam."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from relay.config.schema import EnvironmentsConfig
from relay.core.model import Incident, IncidentState, OrgTree, Severity, SignalSource

logger = logging.getLogger(__name__)

EXPECTED_SOURCE = "aws.cloudwatch"
EXPECTED_DETAIL_TYPE = "CloudWatch Alarm State Change"


class CloudWatchAlarmSource:
    """AlertSource that parses CloudWatch Alarm State Change EventBridge events.

    Implements the AlertSource protocol. Each instance is bound to a specific
    AWS account and region so that parsed Incidents carry the correct provenance.

    An optional ``tag_resolver`` (AlarmTagResolver) is called inside
    ``parse_event`` to populate the incident's ``tags`` dict from live AWS API
    calls; when absent, tags remain empty and all tag-based derivations degrade
    gracefully.
    """

    def __init__(
        self,
        account_id: str,
        region: str,
        environments_config: EnvironmentsConfig | None = None,
        org_tree: OrgTree | None = None,
        tag_resolver: Any = None,
    ) -> None:
        self.account_id = account_id
        self.region = region
        self._environments_config = environments_config
        self._org_tree = org_tree
        self._tag_resolver = tag_resolver

    def bind_config(
        self,
        *,
        org_tree: OrgTree | None = None,
        environments_config: EnvironmentsConfig | None = None,
    ) -> None:
        """Refresh context from a config reload without rebuilding the source.

        Only updates each attribute when the caller supplies a non-None value,
        so partial updates (e.g. org_tree only) leave the other field intact.
        """
        if org_tree is not None:
            self._org_tree = org_tree
        if environments_config is not None:
            self._environments_config = environments_config

    def _derive_environment(
        self, alarm_name: str, tags: dict[str, str], account_id: str
    ) -> tuple[str, bool]:
        """Return (env_name, environment_inferred)."""
        import re as _re
        cfg = self._environments_config
        if cfg is None:
            return ("unrouted", True)

        declared_names = {e.name for e in cfg.environments}

        # 1. Explicit tag
        tag_env = tags.get("relay:environment")
        if tag_env:
            if tag_env in declared_names:
                return (tag_env, False)
            return ("unrouted", True)

        # 2. Name convention regex
        for env_def in cfg.environments:
            if env_def.name_convention_regex:
                if _re.fullmatch(env_def.name_convention_regex, alarm_name):
                    if env_def.name in declared_names:
                        return (env_def.name, False)

        # 3. Account map
        mapped = cfg.account_environment_map.get(account_id)
        if mapped:
            if mapped in declared_names:
                return (mapped, False)
            return ("unrouted", True)

        # 4. Default
        default = cfg.default_environment
        if default in declared_names:
            return (default, True)
        return ("unrouted", True)

    def _derive_deployment_id(
        self, alarm_name: str, tags: dict[str, str], org_tree: OrgTree | None
    ) -> tuple[str, list[str]]:
        """Return (deployment_id, service_path)."""
        if org_tree is None:
            return ("unknown", [])

        # 1. Explicit relay:deployment tag — exact node id match.
        tag_dep = tags.get("relay:deployment")
        if tag_dep and org_tree.get(tag_dep) is not None:
            return (tag_dep, org_tree.resolve_service_path(tag_dep))

        # 2. COMPONENT_ID — the shop's standard resource tag used as the
        #    deployment join key.  Try an exact node-id lookup first, then scan
        #    for a node whose metadata["component_id"] matches.
        component_id = tags.get("COMPONENT_ID") or tags.get("relay:component_id")
        if component_id:
            node = org_tree.get(component_id)
            if node is not None:
                return (node.id, org_tree.resolve_service_path(node.id))
            for node in org_tree.all_nodes():
                if node.metadata.get("component_id") == component_id:
                    return (node.id, org_tree.resolve_service_path(node.id))

        # 3. Project tag — match against the node's project metadata.
        tag_proj = tags.get("relay:project")
        if tag_proj:
            for node in org_tree.all_nodes():
                if node.metadata.get("gitlab_project") == tag_proj:
                    return (node.id, org_tree.resolve_service_path(node.id))

        # 4. Name convention — look for node whose name matches alarm_name parts
        alarm_parts = set(alarm_name.lower().split("-"))
        best_node = None
        best_score = 0
        for node in org_tree.all_nodes():
            if node.level == org_tree.leaf_level:
                node_parts = set(node.name.lower().split("-"))
                score = len(alarm_parts & node_parts)
                if score > best_score:
                    best_score = score
                    best_node = node
        if best_node and best_score > 0:
            return (best_node.id, org_tree.resolve_service_path(best_node.id))

        return ("unknown", [])

    def parse_event(self, raw_event: dict[str, Any]) -> Incident:
        """Parse an EventBridge 'CloudWatch Alarm State Change' event.

        Validates source and detail-type.
        Raises ValueError for non-alarm events or alarms not in ALARM state.

        Args:
            raw_event: The raw EventBridge envelope dict delivered to Lambda.

        Returns:
            A neutral Incident model populated from the event detail.

        Raises:
            ValueError: If the event is not a CloudWatch alarm event, or if the
                        alarm is not transitioning into ALARM state.
        """
        source = raw_event.get("source")
        if source != EXPECTED_SOURCE:
            raise ValueError(
                f"Unexpected event source {source!r}; expected {EXPECTED_SOURCE!r}"
            )

        detail_type = raw_event.get("detail-type")
        if detail_type != EXPECTED_DETAIL_TYPE:
            raise ValueError(
                f"Unexpected detail-type {detail_type!r}; expected {EXPECTED_DETAIL_TYPE!r}"
            )

        detail = raw_event["detail"]
        alarm_name: str = detail["alarmName"]

        # alarmArn may not be present in all event shapes (older alarm configurations).
        alarm_arn: str | None = detail.get("alarmArn")

        state: str = detail["state"]["value"]
        if state != "ALARM":
            raise ValueError(
                f"Alarm {alarm_name!r} transitioned to {state!r}; only ALARM state is processed."
            )

        # Namespace lives deep in the metrics configuration tree.
        namespace: str = (
            detail.get("configuration", {})
            .get("metrics", [{}])[0]
            .get("metricStat", {})
            .get("metric", {})
            .get("namespace", "")
        )

        # Fetch alarm and resource tags via the injected resolver (Node-side only,
        # where IAM grants access to the team's account).  Gracefully degrades to
        # empty dict when no resolver is wired or the resolver encounters an error.
        tags: dict[str, str] = {}
        if self._tag_resolver is not None:
            try:
                tags = self._tag_resolver.resolve(alarm_arn=alarm_arn, detail=detail) or {}
            except Exception:
                logger.warning("alarm tag resolution failed; continuing tagless", exc_info=True)
                tags = {}

        # Detect whether this is a CloudWatch Synthetics canary alarm.
        is_synthetic = (
            "canary" in alarm_name.lower()
            or namespace.startswith("CloudWatchSynthetics")
        )
        signal_source = SignalSource.SYNTHETIC if is_synthetic else SignalSource.CLOUDWATCH_ALARM

        correlation_id = str(uuid.uuid4())

        # Prefer the relay:app tag when present; fall back to the alarm naming
        # convention (<team>-<app>-...) so teams without tags still get a short name.
        app_name_parts = alarm_name.split("-")
        derived_app_name = "-".join(app_name_parts[:2]) if len(app_name_parts) >= 2 else alarm_name
        app_name = tags.get("relay:app") or derived_app_name

        now = datetime.now(tz=UTC)

        logger.info(
            "Parsed CloudWatch alarm event",
            extra={
                "alarm_name": alarm_name,
                "correlation_id": correlation_id,
                "signal_source": signal_source,
                "account_id": self.account_id,
            },
        )

        environment, environment_inferred = self._derive_environment(alarm_name, tags, self.account_id)
        deployment_id, service_path = self._derive_deployment_id(alarm_name, tags, self._org_tree)

        # Detect a relay_synthetic marker injected by test tooling (e.g. the Hub's
        # synthetic-trigger endpoint or a manual test payload).  This flag is
        # orthogonal to signal_source=SignalSource.SYNTHETIC, which means the alarm
        # is driven by a real CloudWatch Synthetics canary — not a fake/test incident.
        incident_is_synthetic: bool = (
            raw_event.get("relay_synthetic") is True
            or detail.get("relay_synthetic") is True
        )

        return Incident(
            correlation_id=correlation_id,
            alarm_name=alarm_name,
            alarm_arn=alarm_arn,
            app_name=app_name,
            account_id=self.account_id,
            region=self.region,
            signal_source=signal_source,
            severity=Severity.SEV3,  # default; overridden by config enrichment step
            state=IncidentState.TRIGGERED,
            tags=tags,
            created_at=now,
            updated_at=now,
            environment=environment,
            deployment_id=deployment_id,
            environment_inferred=environment_inferred,
            service_path=service_path,
            synthetic=incident_is_synthetic,
        )
