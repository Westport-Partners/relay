"""Relay Node — in-process detection library for the team container.

Composition root: wires CloudWatchAlarmSource -> classifier -> on-call resolver ->
EscalationEngine -> DualStreamDispatcher.

Since the collapse (docs/plans/collapsed-single-container.md) the Node no longer
runs as a Lambda. The always-on container builds a NodeHandler and drives it
in-process via DetectionPipeline (node/pipeline.py): a CloudWatch alarm arrives
over SQS or POST /ingest/alarm and is handled by ``process()`` in the same
process as the dashboard, with no EventBridge round-trip. ``main()`` keeps a
``relay-node < alarm.json`` CLI for local dev.

``process()`` dispatches on event shape:
  CloudWatch 'Alarm State Change' — the detection hot path (parse→…→dispatch).
  relay_event=escalation_timeout — fired by the container's DynamoDB-deadline
                                    sweep (DeadlineSweeper); advance escalation.
  relay_event=ack               — acknowledgement received; cancel the deadline.
  relay_event=heartbeat         — emit a liveness heartbeat.

Environment variables (set by RelayComputeStack on the container):
  RELAY_SNS_TOPIC_ARN: team SNS topic for outbound pages
  RELAY_TABLE_NAME: DynamoDB single-table name for contacts, incidents, escalation
  RELAY_GITLAB_REPO / RELAY_GITLAB_SECRET_NAME: optional config-as-code source
  RELAY_NODE_*: node self-identity for heartbeats (app_name/deployment_id/…)
  AWS_REGION / AWS_DEFAULT_REGION
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from relay.adapters.aws.cloudwatch_source import CloudWatchAlarmSource
from relay.adapters.aws.dynamo_stores import (
    DynamoContactStore,
    DynamoEscalationStateStore,
    DynamoIncidentStore,
    DynamoScheduleStore,
)
from relay.adapters.aws.eventbridge_transport import EventBridgeTransport
from relay.adapters.aws.sns_notifier import SNSNotifier
from relay.config.loader import GitLabConfigLoader
from relay.config.schema import RelayConfig, RoutingConfig
from relay.core.classifier import classify_alarm
from relay.core.dispatcher import DualStreamDispatcher
from relay.core.escalation import (
    EscalationEngine,
    EscalationPhase,
)
from relay.core.logging_config import configure_logging
from relay.core.model import Incident, IncidentState, Stream, TimelineEvent

logger = logging.getLogger(__name__)


def _account_id_from_env() -> str:
    """Best-effort AWS account id inside a Lambda, without an STS call.

    Lambda does not expose AWS_ACCOUNT_ID. We can carry it explicitly via
    RELAY_ACCOUNT_ID (set by the stack), otherwise fall back to empty string;
    the account id is non-critical for the alarm-handling path.
    """
    return os.environ.get("RELAY_ACCOUNT_ID", "")


# ---------------------------------------------------------------------------
# NodeHandler
# ---------------------------------------------------------------------------


class NodeHandler:
    """Composition root for the Relay node Lambda.

    Instantiated once per cold-start; all subsequent invocations reuse the
    same instance to avoid re-initialising AWS SDK clients.
    """

    # Default TTL for the in-memory config cache (seconds).  Override via
    # the RELAY_CONFIG_TTL_SECONDS environment variable.
    _DEFAULT_CONFIG_TTL_SECONDS = 300

    # Default TTL for the in-memory ignore-rules cache (seconds).  Short so
    # UI-authored rule changes propagate quickly without a restart.  Override
    # via RELAY_IGNORE_RULES_TTL_SECONDS.
    _DEFAULT_IGNORE_RULES_TTL_SECONDS = 30

    # Default TTL for the in-memory routing-rules cache (seconds).  Short so
    # UI-authored rule changes propagate quickly without a restart.  Override
    # via RELAY_ROUTING_RULES_TTL_SECONDS.
    _DEFAULT_ROUTING_RULES_TTL_SECONDS = 30

    def __init__(
        self,
        *,
        # Injected collaborators for testing.  All default to None; production
        # code leaves them unset and the real adapters are created below.
        _alarm_source: Any = None,
        _config_loader: Any = None,
        _notifier: Any = None,
        _transport: Any = None,
        _contact_store: Any = None,
        _incident_store: Any = None,
        _escalation_state_store: Any = None,
        _suppression_store: Any = None,
        _ignore_rule_store: Any = None,
        _routing_rule_store: Any = None,
        _escalation_engine: Any = None,
        _role_resolver: Any = None,
        _schedule_store: Any = None,
        _tag_enricher: Any = None,
        _clock: Callable[[], float] | None = None,
        _on_incident: Callable[[Incident], None] | None = None,
    ) -> None:
        # --- Read environment variables ---
        # Names MUST match what infra/stacks/node_stack.py sets on the function.
        sns_topic_arn: str = os.environ.get("RELAY_SNS_TOPIC_ARN", "")
        hub_bus_arn: str = os.environ.get("RELAY_HUB_EVENT_BUS_ARN", "")
        # GitLab config source. RELAY_GITLAB_REPO is the project path/id; the
        # secret name is RELAY_GITLAB_SECRET_NAME. Both optional — when the repo
        # is unset the Node runs with empty config (graceful degradation).
        gitlab_project_id: str = os.environ.get("RELAY_GITLAB_REPO", "")
        gitlab_secret: str = os.environ.get("RELAY_GITLAB_SECRET_NAME", "relay/gitlab-token")
        # RELAY_TABLE_NAME — single table for contacts, incidents, and escalation state.
        # Set by the NodeStack CDK construct (see infra/stacks/node_stack.py).
        table_name: str = os.environ.get("RELAY_TABLE_NAME", "")
        # AWS_REGION is always present in the Lambda runtime; AWS_DEFAULT_REGION
        # is not guaranteed. Account id is parsed from the function ARN context
        # rather than a (non-existent) AWS_ACCOUNT_ID env var.
        region: str = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
        account_id: str = _account_id_from_env()

        # Node self-identity for liveness heartbeats. The Node represents one
        # deployment (one team app); these populate the relay.heartbeat event so
        # the Hub fleet catalog registers this app on deploy and keeps its tile
        # LIVE between incidents. Default to the team name when unset.
        self._account_id: str = account_id
        self._node_app_name: str = (
            os.environ.get("RELAY_NODE_APP_NAME")
            or os.environ.get("RELAY_TEAM_NAME")
            or "unnamed-team"
        )
        self._node_deployment_id: str = (
            os.environ.get("RELAY_NODE_DEPLOYMENT_ID") or self._node_app_name
        )
        self._node_environment: str = os.environ.get("RELAY_NODE_ENVIRONMENT", "unrouted")
        _service_path_raw: str = os.environ.get("RELAY_NODE_SERVICE_PATH", "")
        self._node_service_path: list[str] = [
            p.strip() for p in _service_path_raw.split(",") if p.strip()
        ]
        # Optional explicit org ancestry (root→leaf node dicts) as JSON. When set
        # by the stack (RELAY_NODE_ORG_PATH) it wins; otherwise the path is
        # derived from the loaded catalog's org tree at heartbeat time. Carried on
        # the heartbeat so the federated Hub rebuilds the hierarchy from
        # registrations and need not store any catalog of its own.
        self._node_org_path_override: list[dict[str, Any]] = []
        _org_path_raw: str = os.environ.get("RELAY_NODE_ORG_PATH", "")
        if _org_path_raw:
            try:
                parsed = json.loads(_org_path_raw)
                if isinstance(parsed, list):
                    self._node_org_path_override = parsed
            except (ValueError, TypeError):
                logger.warning(
                    "RELAY_NODE_ORG_PATH is not valid JSON list; ignoring", exc_info=True
                )

        # Config TTL — how long (seconds) the in-memory config is considered fresh.
        self._config_ttl: float = float(
            os.environ.get("RELAY_CONFIG_TTL_SECONDS", self._DEFAULT_CONFIG_TTL_SECONDS)
        )

        # Ignore-rules TTL — how long (seconds) the in-memory rule list is fresh.
        # Short so UI-authored changes propagate without a restart.
        self._ignore_rules_ttl: float = float(
            os.environ.get("RELAY_IGNORE_RULES_TTL_SECONDS", self._DEFAULT_IGNORE_RULES_TTL_SECONDS)
        )

        # Routing-rules TTL — how long (seconds) the in-memory rule list is fresh.
        # Short so UI-authored changes propagate without a restart.
        self._routing_rules_ttl: float = float(
            os.environ.get("RELAY_ROUTING_RULES_TTL_SECONDS", self._DEFAULT_ROUTING_RULES_TTL_SECONDS)
        )

        # Monotonic clock — injected for tests, defaults to time.monotonic.
        self._clock: Callable[[], float] = _clock if _clock is not None else time.monotonic

        # --- Instantiate adapters (use injected versions in tests) ---
        if _alarm_source is not None:
            self.alarm_source = _alarm_source
        else:
            # Skip the live AWS alarm/resource tag resolver in the offline
            # local-mock runtime (RELAY_AWS_ENDPOINT_URL set) — there are no real
            # CloudWatch/tag APIs at a DynamoDB-Local endpoint, so attempting it
            # only logs noise. The resolver degrades gracefully either way; this
            # just avoids the doomed call when we already know AWS isn't there.
            from relay.adapters.aws.endpoint import aws_endpoint_url
            from relay.adapters.aws.tag_resolver import AlarmTagResolver

            tag_resolver = (
                None
                if aws_endpoint_url()
                else AlarmTagResolver(account_id=account_id, region=region)
            )
            self.alarm_source = CloudWatchAlarmSource(
                account_id, region, tag_resolver=tag_resolver,
            )
        # Config source selector: "local" reads bundled YAML from RELAY_CONFIG_DIR
        # (default config/ in the asset) — for teams without GitLab; "gitlab"
        # reads from a GitLab repo. Defaults to local when RELAY_GITLAB_REPO is
        # unset, else gitlab.
        config_source = os.environ.get(
            "RELAY_CONFIG_SOURCE", "gitlab" if gitlab_project_id else "local"
        )
        if _config_loader is not None:
            self.config_loader = _config_loader
        elif config_source == "local":
            from relay.config.local_loader import LocalConfigLoader

            config_dir = os.environ.get("RELAY_CONFIG_DIR", "config")
            self.config_loader = LocalConfigLoader(config_dir)
        else:
            self.config_loader = GitLabConfigLoader(
                gitlab_project_id,
                secrets_manager_secret_name=gitlab_secret,
            )
        self.notifier = _notifier or SNSNotifier(topic_arn=sns_topic_arn)
        self.transport = _transport or EventBridgeTransport(hub_event_bus_arn=hub_bus_arn)
        self.contact_store = _contact_store or DynamoContactStore(table_name)
        self.incident_store = _incident_store or DynamoIncidentStore(table_name)
        self.escalation_state_store = (
            _escalation_state_store or DynamoEscalationStateStore(table_name)
        )
        # Suppression counter store (dedup / rate-limit / flapping). Only used
        # when routing.yaml carries an enabled `suppression:` block; constructed
        # unconditionally so the gate is a pure config check at dispatch time.
        if _suppression_store is not None:
            self.suppression_store = _suppression_store
        else:
            from relay.adapters.aws.dynamo_stores import DynamoSuppressionStore

            self.suppression_store = DynamoSuppressionStore(table_name)
        # Ignore-rule store — persistent DynamoDB-backed list of rules that
        # permanently drop matching alarms before persist + page. Evaluated on
        # every alarm on the hot path via a short-TTL in-memory cache so that
        # UI-authored changes propagate quickly without a container restart.
        if _ignore_rule_store is not None:
            self.ignore_rule_store = _ignore_rule_store
        else:
            from relay.adapters.aws.dynamo_stores import DynamoIgnoreRuleStore

            self.ignore_rule_store = DynamoIgnoreRuleStore(table_name)
        # Routing-rule store — persistent DynamoDB-backed list of routing rules
        # that override config-file rules on the hot path. Evaluated on every
        # alarm via a short-TTL in-memory cache so UI-authored changes propagate
        # quickly without a container restart. Fail-open: an empty store or error
        # falls back to self.config.routing unchanged.
        if _routing_rule_store is not None:
            self.routing_rule_store = _routing_rule_store
        else:
            from relay.adapters.aws.dynamo_stores import DynamoRoutingRuleStore

            self.routing_rule_store = DynamoRoutingRuleStore(table_name)
        # Role resolver: callable (roles: list[str], when: datetime) ->
        # list[contact_id], backed by the generated schedule. At the team level
        # the Node and the local Hub share one DynamoDB table (RELAY_TABLE_NAME),
        # so the Node reads the schedule the Hub maintains and resolves
        # escalation roles (primary/secondary/manager) to people itself — the
        # team Hub may be scaled to zero, so the Node must page without it.
        # Falls back to each step's explicit contact_ids when no schedule is
        # available. Injected for tests; built from the shared table otherwise.
        # Schedule store (shared team table). Kept as its own handle so the
        # heartbeat can read a read-only on-call snapshot to push to a federated
        # Hub — that Hub has no access to this team's schedule, so the team that
        # owns the app resolves its own on-call and ships it up. This is a
        # display read only; paging authority stays Node→Hub→escalation.
        self._schedule_store = _schedule_store or DynamoScheduleStore(table_name)
        # Metadata enricher (catalog facts + optional in-account AWS tags). Live
        # tag fetching is gated by RELAY_ENRICH_TAGS (default off); when off this
        # only folds in catalog-derived owner/gitlab/metadata. Best-effort.
        if _tag_enricher is not None:
            self._tag_enricher = _tag_enricher
        else:
            from relay.node.enrichment import TagEnricher

            self._tag_enricher = TagEnricher(account_id=account_id, region=region)
        if _role_resolver is not None:
            self.role_resolver = _role_resolver
        else:
            from relay.core.role_resolver import ScheduleRoleResolver

            self.role_resolver = ScheduleRoleResolver(self._schedule_store)

        if _escalation_engine is not None:
            self.escalation_engine = _escalation_engine
        else:
            # Collapsed single-container runtime: escalation timers are DynamoDB
            # deadlines swept by the container's 30s loop (plan §3 / Step 2), not
            # EventBridge Scheduler. The container normally injects the engine
            # (sharing one DynamoDeadlineTimer with its DeadlineSweeper); when it
            # doesn't, default to a deadline timer on the same table so escalation
            # still records deadlines (a standalone NodeHandler with no sweeper
            # pages step 0; auto-advance needs the container's sweep).
            from relay.adapters.aws.dynamo_stores import DynamoDeadlineTimer

            self.escalation_engine = EscalationEngine(
                timer=DynamoDeadlineTimer(table_name),
                state_store=self.escalation_state_store,
            )

        # --- Load config (network call; happens once per cold-start) ---
        # Graceful degradation: a missing/unreachable config source must NOT
        # crash cold-start. Fall back to an empty (but valid) config so the Node
        # can still ingest alarms and page via fallbacks; config refreshes later
        # via the TTL path once a source is reachable.
        self.config: RelayConfig
        try:
            self.config = self.config_loader.get()
        except Exception:
            logger.error(
                "Config load failed at cold-start (%s source); continuing with "
                "empty config — routing/escalation lookups will use fallbacks",
                config_source,
                exc_info=True,
            )
            self.config = RelayConfig.empty()
        # Record the monotonic time at which the config was (last) loaded.
        self._config_loaded_at: float = self._clock()

        # In-memory ignore-rules cache.  Populated lazily on the first alarm;
        # refreshed when _ignore_rules_ttl seconds have elapsed since the last
        # successful load.  Use -inf so that the very first clock reading always
        # satisfies ``now - loaded_at >= ttl``, forcing a DynamoDB read on the
        # first alarm regardless of clock epoch.
        self._ignore_rules_cache: list[tuple[str, Any, int]] = []
        self._ignore_rules_loaded_at: float = float("-inf")  # forces load on first alarm

        # In-memory routing-rules cache. Populated lazily on the first alarm;
        # refreshed when _routing_rules_ttl seconds have elapsed. Use -inf so
        # that the very first clock reading always forces a DynamoDB read.
        self._routing_rules_cache: list[tuple[str, Any, int, bool]] = []
        self._routing_rules_loaded_at: float = float("-inf")  # forces load on first alarm
        # Track whether the last _effective_routing_config() call used DB rules
        # so record_match only fires when DB rules were actually in use.
        self._used_db_routing: bool = False

        # §8 in-process sink: called after Step 7 dispatch with the persisted
        # Incident so the embedded Hub can apply tile + lifecycle effects without
        # a cross-process EventBridge hop. None in standalone Lambda mode.
        self._on_incident: Callable[[Incident], None] | None = _on_incident

        logger.info("NodeHandler initialised for account=%s region=%s", account_id, region)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _maybe_refresh_config(self) -> None:
        """Refresh the cached config if the TTL has elapsed.

        Uses a monotonic clock so Lambda container clock skew cannot cause
        spurious back-to-back refreshes.  A refresh failure is intentionally
        swallowed — we keep serving the last-good config so that a transient
        GitLab outage never blocks paging.
        """
        now = self._clock()
        if now - self._config_loaded_at < self._config_ttl:
            return

        logger.info(
            "Config TTL elapsed (%.0fs); refreshing from GitLab", self._config_ttl
        )
        try:
            self.config = self.config_loader.refresh()
            self._config_loaded_at = self._clock()
            logger.info("Config refreshed successfully at %s", self.config.loaded_at)
        except Exception:
            logger.warning(
                "Config refresh failed; continuing with cached config loaded at %s",
                self.config.loaded_at,
                exc_info=True,
            )

    def _refresh_ignore_rules(self) -> None:
        """Reload the ignore-rule list from DynamoDB if the TTL has elapsed.

        Uses the same monotonic-clock TTL guard as ``_maybe_refresh_config``.
        On error the last-good cache is preserved and a warning is logged —
        fail-open: a transient DynamoDB outage must never block a real page.
        """
        now = self._clock()
        if now - self._ignore_rules_loaded_at < self._ignore_rules_ttl:
            return

        try:
            self._ignore_rules_cache = self.ignore_rule_store.list_rules()
            self._ignore_rules_loaded_at = self._clock()
            logger.debug(
                "Ignore-rules cache refreshed: %d rule(s) loaded",
                len(self._ignore_rules_cache),
            )
        except Exception:
            logger.warning(
                "Ignore-rules refresh failed; keeping %d cached rule(s)",
                len(self._ignore_rules_cache),
                exc_info=True,
            )

    def _refresh_routing_rules(self) -> None:
        """Reload the routing-rule list from DynamoDB if the TTL has elapsed.

        Uses the same monotonic-clock TTL guard as ``_refresh_ignore_rules``.
        On error the last-good cache is preserved and a warning is logged —
        fail-open: a transient DynamoDB outage must never block a real page.
        """
        now = self._clock()
        if now - self._routing_rules_loaded_at < self._routing_rules_ttl:
            return

        try:
            self._routing_rules_cache = self.routing_rule_store.list_rules()
            self._routing_rules_loaded_at = self._clock()
            logger.debug(
                "Routing-rules cache refreshed: %d rule(s) loaded",
                len(self._routing_rules_cache),
            )
        except Exception:
            logger.warning(
                "Routing-rules refresh failed; keeping %d cached rule(s)",
                len(self._routing_rules_cache),
                exc_info=True,
            )

    def _effective_routing_config(self) -> RoutingConfig:
        """Return the RoutingConfig the classifier should use.

        If the DB cache is non-empty (at least one enabled rule), builds a new
        RoutingConfig with DB rules substituted for config-file rules. All other
        fields (default_escalation_policy_id, default_streams, federation,
        suppression, ignore) come from self.config.routing unchanged.

        If the DB cache is empty OR anything raises, returns self.config.routing
        unchanged (fail-open). This guarantees that a fresh deploy with an empty
        routing-rule table behaves exactly as today (config-driven), and that a
        DynamoDB outage can never break classification/paging.

        Sets self._used_db_routing = True when DB rules were applied.
        """

        try:
            self._refresh_routing_rules()
            # Filter to enabled rules only, then extract RoutingRule objects
            # sorted by priority ascending (defensive — the validator requires it).
            enabled_db_rules = [
                rule
                for (_rule_id, rule, _count, enabled) in self._routing_rules_cache
                if enabled
            ]
            if not enabled_db_rules:
                # Empty DB (or all disabled) → fall back to config.
                self._used_db_routing = False
                logger.debug(
                    "Routing-rules DB cache empty; using config routing rules (%d rule(s))",
                    len(self.config.routing.rules),
                )
                return self.config.routing

            # Sort by priority ascending (defensive pre-sort before model_copy,
            # since model_copy does NOT re-run validators by default in Pydantic v2).
            db_rules_sorted = sorted(enabled_db_rules, key=lambda r: r.priority)
            # model_copy preserves all non-rules fields automatically.
            effective = self.config.routing.model_copy(update={"rules": db_rules_sorted})
            self._used_db_routing = True
            logger.debug(
                "Using %d DB routing rule(s) (config has %d rule(s))",
                len(db_rules_sorted),
                len(self.config.routing.rules),
            )
            return effective
        except Exception:
            logger.warning(
                "Routing-rules effective-config build failed; falling back to config routing",
                exc_info=True,
            )
            self._used_db_routing = False
            return self.config.routing

    def _matched_ignore_rule(
        self, incident: Incident
    ) -> tuple[str, Any] | None:
        """Return the first enabled ignore rule that matches *incident*, or None.

        Refreshes the cache (subject to TTL) then iterates the cached rules.
        Fail-open: on any error a warning is logged and None is returned so the
        alarm is never dropped due to a rule-evaluation failure.
        """
        try:
            self._refresh_ignore_rules()
            for rule_id, rule, _count in self._ignore_rules_cache:
                if rule.enabled and rule.matches(incident):
                    return rule_id, rule
            return None
        except Exception:
            logger.warning(
                "Ignore-rule evaluation failed for incident %s — failing open (not ignored)",
                incident.correlation_id,
                exc_info=True,
            )
            return None

    def _record_escalation_event(
        self,
        incident: Incident,
        event_type: str,
        detail: dict[str, Any],
    ) -> None:
        """Append a single escalation-related timeline event to *incident*.

        All four escalation events share the same shape: actor="system",
        stream=Stream.TEAM. This helper is the single chokepoint so every
        call site stays consistent.  The caller is responsible for persisting
        the updated incident when required.
        """
        incident.add_event(
            TimelineEvent(
                incident_id=incident.correlation_id,
                actor="system",
                stream=Stream.TEAM,
                event_type=event_type,
                detail=detail,
            )
        )

    def _contacts_for_transition(self, transition: Any) -> list[str]:
        """Resolve a transition's paging targets to a deduped contact_id list.

        Combines the step's explicit ``contact_ids_to_page`` with any
        ``roles_to_page`` resolved via the injected role resolver (schedule-
        backed). When no resolver is wired, roles contribute nothing and only
        the explicit contact_ids are paged (graceful fallback).
        """
        contacts: list[str] = list(transition.contact_ids_to_page)
        roles = getattr(transition, "roles_to_page", None) or []
        if roles and self.role_resolver is not None:
            try:
                resolved = self.role_resolver(roles, datetime.now(UTC))
            except Exception:
                logger.warning(
                    "role resolution failed for roles=%s; using explicit contacts only",
                    roles,
                    exc_info=True,
                )
                resolved = []
            for cid in resolved:
                if cid not in contacts:
                    contacts.append(cid)
        elif roles and self.role_resolver is None:
            logger.info(
                "step pages roles=%s but no role resolver is configured; "
                "falling back to explicit contact_ids=%s",
                roles,
                contacts,
            )
        return contacts

    def process(self, event: dict[str, Any]) -> dict[str, Any]:
        """Main processing pipeline. Returns a status dict for Lambda response."""
        self._maybe_refresh_config()
        try:
            # ------------------------------------------------------------------
            # Relay internal control events (injected by EventBridge Scheduler
            # or an inbound ack webhook) are handled before the CloudWatch path.
            # ------------------------------------------------------------------
            relay_event = event.get("relay_event")

            if relay_event == "escalation_timeout":
                return self._handle_timeout(event)

            if relay_event == "ack":
                # TODO: wire inbound ack source (SMS reply / webhook) once
                # the acknowledgement ingestion path is implemented.
                return self._handle_ack(event)

            if relay_event == "heartbeat":
                # Periodic liveness ping injected by an EventBridge scheduled
                # rule; emit a relay.heartbeat to the Hub fleet catalog.
                return self._emit_heartbeat()

            # ------------------------------------------------------------------
            # Default path: CloudWatch Alarm State Change event
            # ------------------------------------------------------------------
            return self._handle_alarm(event)

        except ValueError as exc:
            logger.warning("Bad event shape, skipping: %s", exc)
            return {"statusCode": 400, "error": str(exc)}

        except Exception:
            logger.error(
                "Unhandled exception processing event; Lambda will mark invocation failed",
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    def _resolve_org_path(self) -> list[dict[str, Any]]:
        """Resolve this node's org ancestry (root→leaf) for the heartbeat.

        Precedence: the explicit RELAY_NODE_ORG_PATH override (if set) wins;
        otherwise derive it from the loaded catalog's org tree keyed on this
        node's deployment_id; otherwise fall back to a single synthetic node
        built from the node's own identity so the Hub still registers a tile.
        The federated Hub rebuilds the catalog from these payloads, so org data
        always originates here on the team side.
        """
        if self._node_org_path_override:
            return self._node_org_path_override

        org_tree = getattr(self.config, "org_tree", None)
        if org_tree is not None:
            try:
                path: list[dict[str, Any]] = org_tree.org_path(self._node_deployment_id)
            except Exception:
                logger.warning("org_path derivation failed", exc_info=True)
                path = []
            if path:
                return path

        # Fallback: a single deployment-level node from our own identity.
        return [
            {
                "id": self._node_deployment_id,
                "name": self._node_app_name,
                "level": "deployment",
                "parent": None,
            }
        ]

    def _resolve_oncall_snapshot(self, org_path: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Resolve a read-only on-call snapshot to push on the heartbeat.

        A federated Hub has no access to this team's schedule table, so the team
        that owns the app resolves its own on-call here and ships it up. This is
        a *display* read only — paging still flows Node→Hub→escalation, so the
        Hub remains the paging authority. Best-effort: any failure ⇒ None ⇒ the
        drawer simply hides the on-call section (a team Hub fills it live anyway).
        """
        try:
            from relay.core.scheduling import (
                Role,
                apply_overrides,
                monday_of,
                schedule_from_stored,
                shift_for_hour,
            )

            now = datetime.now(UTC)
            ws = monday_of(now.date())
            stored = self._schedule_store.get_schedule(ws.isoformat())
            if not stored:
                return None
            try:
                overrides = self._schedule_store.get_overrides(ws.isoformat())
                if overrides:
                    stored = apply_overrides(stored, overrides)
            except Exception:
                logger.debug("override overlay failed for snapshot", exc_info=True)
            sched = schedule_from_stored(stored)
            shift = shift_for_hour(now.hour)
            naive = now.replace(tzinfo=None)
            by_role = sched.assignments_at(naive)
            names: dict[str, str] = {}
            try:
                names = {c.contact_id: c.name for c in self.contact_store.list_contacts()}
            except Exception:
                names = {}
            roles_out: dict[str, Any] = {}
            for role, rcid in by_role.items():
                roles_out[str(role)] = (
                    {"contact_id": rcid, "name": names.get(rcid, rcid)} if rcid
                    else {"contact_id": None, "name": None, "gap": True}
                )
            if not roles_out:
                return None
            snapshot: dict[str, Any] = {
                "as_of": now.isoformat(),
                "shift": str(shift),
                "source": "team_snapshot",
                "roles": roles_out,
            }
            primary = by_role.get(Role.PRIMARY)
            if primary:
                snapshot["contact_id"] = primary
                snapshot["name"] = names.get(primary, primary)
            return snapshot
        except Exception:
            logger.warning("on-call snapshot resolution failed", exc_info=True)
            return None

    def _emit_heartbeat(self) -> dict[str, Any]:
        """Emit a liveness heartbeat to the Hub.

        Registers this node's app in the Hub fleet catalog on deploy and keeps
        its big-board tile LIVE between incidents. Invoked on a fixed cadence by
        an EventBridge scheduled rule (relay_event=heartbeat). The heartbeat
        carries this node's org ancestry so the federated Hub builds the
        hierarchy from registrations (no Hub-side catalog), plus best-effort
        ``metadata`` (catalog facts + optional AWS tags) and an ``on_call``
        snapshot so a federated Hub's tile-detail drawer can show who owns the
        app without reaching this team's schedule. Best-effort — a heartbeat
        failure never fails the invocation.
        """
        ts = datetime.now(UTC).isoformat()
        org_path = self._resolve_org_path() or []
        # Best-effort enrichment — neither must ever break the heartbeat.
        try:
            leaf = org_path[-1] if org_path else None
            # Resolve tag_map from hierarchy config, same as the Stage 2 alarm path.
            hierarchy = getattr(self.config, "hierarchy", None)
            _tag_map: dict[str, str] = (
                hierarchy.deployment_defaults.tag_map
                if hierarchy is not None and getattr(hierarchy, "deployment_defaults", None)
                else {}
            )
            metadata = self._tag_enricher.build_metadata(
                deployment_id=self._node_deployment_id,
                app_name=self._node_app_name,
                org_node=leaf,
                tag_map=_tag_map,
            )
        except Exception:
            logger.warning("metadata enrichment failed", exc_info=True)
            metadata = {}
        on_call = self._resolve_oncall_snapshot(org_path)
        try:
            self.transport.emit_heartbeat(
                account_id=self._account_id,
                app_name=self._node_app_name,
                timestamp=ts,
                environment=self._node_environment,
                deployment_id=self._node_deployment_id,
                service_path=self._node_service_path or None,
                org_path=org_path or None,
                metadata=metadata or None,
                on_call=on_call,
            )
        except Exception:
            logger.warning("heartbeat emit failed", exc_info=True)
            return {"statusCode": 200, "note": "heartbeat_failed"}
        return {
            "statusCode": 200,
            "note": "heartbeat_ok",
            "app_name": self._node_app_name,
        }

    def _handle_timeout(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle an escalation_timeout callback from EventBridge Scheduler.

        Loads the escalation policy for the incident, calls
        EscalationEngine.on_timeout(), and dispatches any resulting contacts
        via the existing dual-stream dispatcher.

        Args:
            event: Lambda event dict containing ``incident_id`` and
                   ``step_index`` keys.

        Returns:
            Status dict with ``statusCode`` and escalation result metadata.
        """
        incident_id: str = event["incident_id"]
        step_index: int = int(event["step_index"])
        logger.info(
            "Handling escalation_timeout for incident=%s step=%s",
            incident_id,
            step_index,
        )

        # Resolve the escalation policy for this incident.
        # Load the persisted context to retrieve the policy_id that was active
        # when escalation started, then look up the full policy object.
        policy = None
        esc_ctx = self.escalation_state_store.load(incident_id)
        if esc_ctx is not None:
            for p in self.config.escalation.policies:
                if p.policy_id == esc_ctx.policy_id:
                    policy = p
                    break

        # Fallback to the first configured policy if the context is missing or
        # the stored policy_id no longer matches any configured policy.
        if policy is None and self.config.escalation.policies:
            logger.warning(
                "Could not resolve policy_id from escalation context for incident=%s; "
                "falling back to first configured policy",
                incident_id,
            )
            policy = self.config.escalation.policies[0]

        if policy is None:
            logger.warning(
                "No escalation policy available for timeout on incident=%s; ignoring",
                incident_id,
            )
            return {"statusCode": 200, "note": "no_policy"}

        transition = self.escalation_engine.on_timeout(incident_id, step_index, policy)
        step_contacts = self._contacts_for_transition(transition)
        logger.info(
            "Timeout processed: phase %s -> %s, roles=%s contacts=%s",
            transition.old_phase,
            transition.new_phase,
            getattr(transition, "roles_to_page", []),
            step_contacts,
        )

        if step_contacts:
            # Fetch the incident record so the dispatcher has the full context.
            incident = self.incident_store.get_incident(incident_id)
            if incident is not None:
                # A timeout that advances/exhausts escalation transitions the
                # incident into ESCALATED so the Hub (and its lifecycle listeners)
                # see a real state change, not another TRIGGERED. Acknowledged or
                # already-resolved incidents are left untouched.
                if incident.state in (
                    IncidentState.TRIGGERED,
                    IncidentState.ESCALATED,
                ):
                    incident.state = IncidentState.ESCALATED
                    incident.updated_at = datetime.now(UTC)
                # Record timeline events for the real advance (T3).
                # step_advanced: from the event's step_index to the new current step.
                if transition.new_phase == EscalationPhase.ESCALATING:
                    new_step_index = step_index + 1
                    self._record_escalation_event(
                        incident,
                        "escalation.step_advanced",
                        {"from_step": step_index, "to_step": new_step_index},
                    )
                    self._record_escalation_event(
                        incident,
                        "escalation.page_sent",
                        {
                            "step_index": new_step_index,
                            "roles": list(
                                getattr(transition, "roles_to_page", [])
                            ),
                            "contact_ids": step_contacts,
                            "streams": [s for s in getattr(transition, "streams", [])],
                            "timeout_minutes": getattr(transition, "timeout_minutes", None),
                        },
                    )
                self.incident_store.put_incident(incident)
                DualStreamDispatcher(
                    notifier=self.notifier,
                    transport=self.transport,
                    contact_ids=step_contacts,
                ).dispatch(incident)

                # §8 no-drift seam: in the collapsed container the central leg is
                # an in-process call, not an EventBridge emit. Hand the now-ESCALATED
                # incident to the Hub so the big-board tile + lifecycle listeners
                # reflect the escalation without a bus round-trip. Failure here must
                # never break the timeout path — paging already happened above.
                if self._on_incident is not None:
                    try:
                        self._on_incident(incident)
                    except Exception:
                        logger.warning(
                            "on_incident sink raised for escalation timeout incident=%s — "
                            "ignoring sink failure",
                            incident_id,
                            exc_info=True,
                        )

        # T4: EXHAUSTED branch — no contacts are paged but we must record the
        # ladder exhausted event.  The if step_contacts: block above is skipped
        # when the transition is EXHAUSTED (contact_ids_to_page == []), so we
        # fetch the incident here, guard against re-emit, and persist.
        if transition.new_phase == EscalationPhase.EXHAUSTED:
            incident = self.incident_store.get_incident(incident_id)
            if incident is not None:
                already_exhausted = any(
                    ev.event_type == "escalation.exhausted"
                    for ev in incident.timeline
                )
                if not already_exhausted:
                    self._record_escalation_event(
                        incident,
                        "escalation.exhausted",
                        {"last_step_index": step_index},
                    )
                    self.incident_store.put_incident(incident)

        return {
            "statusCode": 200,
            "incident_id": incident_id,
            "old_phase": transition.old_phase,
            "new_phase": transition.new_phase,
            "note": transition.note,
        }

    def _handle_ack(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle an acknowledgement event.

        Args:
            event: Lambda event dict containing ``incident_id`` and
                   ``contact_id`` keys.

        Returns:
            Status dict with ``statusCode`` and ack result metadata.
        """
        incident_id: str = event["incident_id"]
        contact_id: str = event["contact_id"]
        logger.info(
            "Handling ack for incident=%s contact=%s", incident_id, contact_id
        )

        # TODO: load the correct policy for this incident from DynamoDB once
        #       the state port is wired.  First policy used as placeholder.
        policy = None
        if self.config.escalation.policies:
            policy = self.config.escalation.policies[0]

        if policy is None:
            logger.warning(
                "No escalation policy available for ack on incident=%s; ignoring",
                incident_id,
            )
            return {"statusCode": 200, "note": "no_policy"}

        transition = self.escalation_engine.acknowledge(incident_id, contact_id, policy)
        logger.info(
            "Ack processed: phase %s -> %s note=%r",
            transition.old_phase,
            transition.new_phase,
            transition.note,
        )
        return {
            "statusCode": 200,
            "incident_id": incident_id,
            "old_phase": transition.old_phase,
            "new_phase": transition.new_phase,
            "note": transition.note,
        }

    def _is_suppressed(self, incident: Incident) -> bool:
        """Return True if *incident* should be suppressed as noise.

        Reads the (optional) ``suppression:`` block from routing.yaml. When the
        block is absent, disabled, or the severity is exempt, this is a cheap
        no-op returning False. Otherwise it records one fire in the alarm's
        current time window and asks the policy whether the post-increment count
        exceeds the allowed ``max_per_window``.

        Fail-open: any error resolving config or touching the counter store is
        swallowed and treated as "not suppressed", so noise control can never
        block a genuine page.
        """
        suppression = getattr(self.config.routing, "suppression", None)
        if suppression is None or not suppression.enabled:
            return False
        if suppression.is_exempt(incident):
            return False
        try:
            window_seconds, _ = suppression.limits_for(incident)
            dedup_key = f"{incident.account_id}#{incident.app_name}#{incident.alarm_name}"
            count = self.suppression_store.increment_and_count(dedup_key, window_seconds)
            result: bool = suppression.is_suppressed(incident, count)
            return result
        except Exception:
            logger.warning(
                "Suppression check failed for incident %s — failing open (not suppressed)",
                incident.correlation_id,
                exc_info=True,
            )
            return False

    def _handle_alarm(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a CloudWatch Alarm State Change event.

        This is the original 7-step pipeline unchanged from the initial
        implementation.

        Args:
            event: CloudWatch alarm state change event delivered by EventBridge.

        Returns:
            Status dict with ``statusCode``, ``correlation_id``, ``severity``,
            ``team_ok``, and ``central_ok``.
        """
        # Step 1: Parse alarm event into domain Incident
        logger.info("Step 1: parsing CloudWatch alarm event")
        _bind = getattr(self.alarm_source, "bind_config", None)
        if _bind is not None:
            _bind(
                org_tree=getattr(self.config, "org_tree", None),
                environments_config=getattr(self.config, "environments", None),
            )
        incident: Incident = self.alarm_source.parse_event(event)

        # Step 2: Classify alarm — derive severity, streams, escalation policy
        logger.info("Step 2: classifying alarm %r", incident.alarm_name)
        # namespace is not stored on Incident; extract from event detail for routing
        namespace: str = (
            event.get("detail", {})
            .get("configuration", {})
            .get("metrics", [{}])[0]
            .get("metricStat", {})
            .get("metric", {})
            .get("namespace", "")
        )
        classification = classify_alarm(
            alarm_name=incident.alarm_name,
            alarm_arn=incident.alarm_arn or "",
            namespace=namespace,
            tags=incident.tags,
            routing_config=self._effective_routing_config(),
        )

        # Step 3: Apply classification results back onto the incident
        logger.info(
            "Step 3: applying classification — severity=%s source=%s rule=%r",
            classification.severity,
            classification.signal_source,
            classification.matched_rule_id,
        )
        incident.severity = classification.severity
        incident.signal_source = classification.signal_source
        incident.routing_rule_id = classification.matched_rule_id
        incident.routing_reason = classification.reasoning
        # Capture which policy drove this incident so the flow view can rebuild
        # the expected ladder later, even if the policy is edited afterwards.
        incident.escalation_policy_id = classification.escalation_policy_id

        # Record DB routing-rule match count (best-effort: must never affect paging).
        # Only record when DB rules were actually used — avoids recording against
        # config-only rule_ids that aren't in the DB.
        if classification.matched_rule_id is not None and self._used_db_routing:
            try:
                self.routing_rule_store.record_match(classification.matched_rule_id)
            except Exception:
                logger.warning(
                    "Routing-rule match count update failed for %s — continuing",
                    classification.matched_rule_id,
                    exc_info=True,
                )

        # Step 3a: Ignore rules — explicit, permanent per-team drop list (UI-authored).
        # Runs BEFORE suppression and BEFORE persist + page: a matched alarm creates no
        # incident row, no page, no ticket, no federated event, and is therefore absent
        # from all metric rollups. Distinct from suppression (rate-limit): ignore is an
        # explicit "never care about this alarm" decision. Fail-open like suppression.
        matched = self._matched_ignore_rule(incident)
        if matched is not None:
            rule_id, rule = matched
            try:
                self.ignore_rule_store.record_trigger(rule_id)
            except Exception:
                logger.warning(
                    "Ignore-rule trigger count update failed for %s — continuing",
                    rule_id,
                    exc_info=True,
                )
            logger.info(
                "Ignored incident %s (alarm=%r) via ignore rule %s (%s)",
                incident.correlation_id,
                incident.alarm_name,
                rule_id,
                rule.name or "unnamed",
            )
            return {
                "statusCode": 200,
                "correlation_id": incident.correlation_id,
                "severity": incident.severity,
                "ignored": True,
                "ignore_rule_id": rule_id,
                "team_ok": False,
                "central_ok": False,
            }

        # Step 3b: Noise suppression (dedup / rate-limit / flapping).
        # Runs BEFORE persist + page so a suppressed re-fire creates no incident
        # row, no page, no ticket, and no federated event — the whole point is to
        # stop noise at the source. Exempt severities (default SEV1) always pass.
        # Fail-open: any store error means "do not suppress" so noise control can
        # never block a real page.
        if self._is_suppressed(incident):
            logger.info(
                "Suppressed incident %s (alarm=%r severity=%s) — noise window exceeded",
                incident.correlation_id,
                incident.alarm_name,
                incident.severity,
            )
            return {
                "statusCode": 200,
                "correlation_id": incident.correlation_id,
                "severity": incident.severity,
                "suppressed": True,
                "team_ok": False,
                "central_ok": False,
            }

        # Step 4: Resolve on-call contacts for this incident's team.
        # Contacts come solely from the escalation policy's contact_ids (Step 6).
        logger.info("Step 4: contacts will be sourced from the escalation policy")
        contact_ids: list[str] = []

        # Resolve per-incident deployment metadata from the failing resource's tags
        # against the catalog tag-map + the deployment node's metadata templates.
        try:
            from relay.config.tag_mapping import resolve_deployment_metadata
            tag_map: dict[str, str] = {}
            hierarchy = getattr(self.config, "hierarchy", None)
            if hierarchy is not None and getattr(hierarchy, "deployment_defaults", None):
                tag_map = hierarchy.deployment_defaults.tag_map or {}
            node_meta: dict[str, object] = {}
            org_tree = getattr(self.config, "org_tree", None)
            if org_tree is not None:
                node = org_tree.get(incident.deployment_id)
                if node is not None and isinstance(node.metadata, dict):
                    node_meta = node.metadata
            resolved = resolve_deployment_metadata(node_meta, tag_map, incident.tags)
            if resolved:
                incident.deployment_metadata = resolved
        except Exception:
            logger.warning("deployment metadata resolution failed; continuing", exc_info=True)

        # Step 5: Persist incident to DynamoDB
        logger.info("Step 5: saving incident %s", incident.correlation_id)
        self.incident_store.put_incident(incident)

        # Step 6: Start escalation
        logger.info("Step 6: starting escalation for incident %s", incident.correlation_id)
        policy = None
        for p in self.config.escalation.policies:
            if p.policy_id == classification.escalation_policy_id:
                policy = p
                break

        if policy is not None:
            esc_transition = self.escalation_engine.start(incident, policy)
            # Resolve roles (via schedule) + explicit contacts for this step.
            step_contacts = self._contacts_for_transition(esc_transition)
            logger.info(
                "Escalation started: phase=%s roles=%s contacts_to_page=%s",
                esc_transition.new_phase,
                getattr(esc_transition, "roles_to_page", []),
                step_contacts,
            )
            # Record the trigger + initial page on the incident timeline.
            self._record_escalation_event(
                incident,
                "incident.triggered",
                {
                    "severity": incident.severity,
                    "signal_source": incident.signal_source,
                    "alarm_name": incident.alarm_name,
                    "policy_id": policy.policy_id,
                },
            )
            self._record_escalation_event(
                incident,
                "escalation.page_sent",
                {
                    "step_index": 0,
                    "roles": list(getattr(esc_transition, "roles_to_page", [])),
                    "contact_ids": step_contacts,
                    "streams": [s for s in getattr(esc_transition, "streams", [])],
                    "timeout_minutes": getattr(esc_transition, "timeout_minutes", None),
                },
            )
            # Persist the timeline events recorded above.
            self.incident_store.put_incident(incident)
            # Merge escalation contacts into notification contact list if not already present
            for cid in step_contacts:
                if cid not in contact_ids:
                    contact_ids.append(cid)
        else:
            logger.warning(
                "No escalation policy found for policy_id=%r; skipping escalation",
                classification.escalation_policy_id,
            )

        # Step 7: Dual-stream dispatch (SNS team page + EventBridge central event)
        logger.info("Step 7: dispatching incident %s", incident.correlation_id)
        result = DualStreamDispatcher(
            notifier=self.notifier,
            transport=self.transport,
            contact_ids=contact_ids,
        ).dispatch(incident)

        logger.info(
            "Dispatch complete: team_ok=%s central_ok=%s",
            result.team_stream_ok,
            result.central_stream_ok,
        )

        # §8 no-drift seam: in-process replacement for the cross-process central
        # hop that EventBridge used to (fail to) deliver.  When an on_incident
        # sink is injected (always-on container mode), hand the persisted Incident
        # directly to the Hub without any bus round-trip.  Failure here must NEVER
        # fail the alarm — the Node has already paged and persisted.
        if self._on_incident is not None:
            try:
                self._on_incident(incident)
            except Exception:
                logger.warning(
                    "on_incident sink raised for incident %s — local Node processing "
                    "already complete; ignoring sink failure",
                    incident.correlation_id,
                    exc_info=True,
                )

        return {
            "statusCode": 200,
            "correlation_id": incident.correlation_id,
            "severity": incident.severity,
            "team_ok": result.team_stream_ok,
            "central_ok": result.central_stream_ok,
        }


# ---------------------------------------------------------------------------
# Local dev / CLI entrypoint
# ---------------------------------------------------------------------------
#
# The Node no longer runs as a Lambda — detection is in-process in the collapsed
# container (DetectionPipeline wraps NodeHandler; see node/pipeline.py + Step 5
# of docs/plans/collapsed-single-container.md). This CLI remains as a dev tool:
#   relay-node < alarm.json
# runs one event through a freshly-built handler and prints the result dict.


def main() -> None:
    """Local dev / CLI entrypoint. Reads a test event from stdin and processes it."""
    import sys

    configure_logging()
    event = json.load(sys.stdin)
    print(json.dumps(NodeHandler().process(event), indent=2))
