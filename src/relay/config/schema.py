"""Pydantic v2 models for validating Git-stored Relay config YAML files.

These models represent the static configuration layer — escalation policies
and routing rules — that is version-controlled in GitLab and loaded at startup
(or on a webhook-triggered refresh).

No AWS SDK types appear here; this module must remain cloud-agnostic so it
can be tested locally without any AWS credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from relay.core.model import (
    EscalationPolicy,
    Incident,
    IncidentState,
    OrgNode,
    OrgTree,
    RoutingRule,
    Severity,
    Stream,
)


def _severity_rank(severity: Severity) -> int:
    """Rank a severity for threshold comparison: SEV1=0 (most severe) … SEV4=3.

    Derived from enum declaration order so it stays in sync with the model and
    never drifts from the Hub's own ordering table.
    """
    return list(Severity).index(severity)


def _environment_matches(
    spec: str | list[str] | None, incident_environment: str
) -> bool:
    """Return True if *incident_environment* satisfies an override's env *spec*.

    ``spec`` may be:

    * ``None``       — no environment constraint (matches anything).
    * a string       — exact match (e.g. ``prod``).
    * a list[str]    — membership match (e.g. ``[dev, test, preprod]`` for the
                       "nonprod" category, since environments are named
                       individually — there is no single "nonprod" name).
    """
    if spec is None:
        return True
    if isinstance(spec, str):
        return incident_environment == spec
    return incident_environment in spec


class EscalationConfig(BaseModel):
    """Root model for the escalation.yaml config file.

    Wraps a list of :class:`EscalationPolicy` objects and enforces that every
    policy_id is unique within the file.
    """

    policies: list[EscalationPolicy]

    @model_validator(mode="after")
    def policy_ids_are_unique(self) -> EscalationConfig:
        """Raise ValueError if any two policies share the same policy_id."""
        seen: set[str] = set()
        for policy in self.policies:
            if policy.policy_id in seen:
                raise ValueError(
                    f"Duplicate policy_id found: '{policy.policy_id}'. "
                    "All policy_ids must be unique within escalation.yaml."
                )
            seen.add(policy.policy_id)
        return self


class FederationOverride(BaseModel):
    """A per-app / per-tag exception to the global federation forwarding gate.

    Matched against an already-classified :class:`Incident` at the local Hub,
    just before it would forward up to a federated (central) Hub. All present
    match fields are ANDed; omitted fields match anything. The first override
    whose match wins (file order), so put the most specific overrides first.

    A match lets you either tighten or loosen forwarding for a slice of traffic:

    * ``forward: never``  — this slice never federates, regardless of severity.
    * ``forward: always`` — this slice always federates (bypasses the threshold).
    * ``min_severity``    — a slice-specific threshold replacing the global one.

    Match fields (all optional):

    * ``app_name``           — exact match on the incident's app name.
    * ``alarm_name_prefix``  — the alarm name must start with this string.
    * ``environment``        — match on the incident's environment: a single
                               name (``prod``) or a list of names
                               (``[dev, test, preprod]``) for a category.
    * ``tags``               — all key/value pairs must be present in the
                               incident's tags.
    """

    name: str | None = None
    app_name: str | None = None
    alarm_name_prefix: str | None = None
    environment: str | list[str] | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    forward: Literal["always", "never"] | None = None
    min_severity: Severity | None = None

    def matches(self, incident: Incident) -> bool:
        """Return True if this override applies to *incident* (AND logic)."""
        if self.app_name is not None and incident.app_name != self.app_name:
            return False
        if self.alarm_name_prefix is not None and not incident.alarm_name.startswith(
            self.alarm_name_prefix
        ):
            return False
        if not _environment_matches(self.environment, incident.environment):
            return False
        for key, value in self.tags.items():
            if incident.tags.get(key) != value:
                return False
        return True


class FederationConfig(BaseModel):
    """Forwarding policy for the local Hub → central (federated) Hub hop.

    Lives under the ``federation:`` key of routing.yaml so the noise budget for
    federation is version-controlled alongside routing rules. This is the only
    way to configure the gate — when the block is absent the Hub uses the model
    defaults below (``min_severity`` SEV2, all states, no overrides).

    * ``min_severity``    — global threshold; an incident at or above this
                            severity is eligible to forward (SEV1 > SEV2 > …).
    * ``forward_states``  — if set, only incidents in one of these lifecycle
                            states forward; ``None``/empty means all states.
    * ``overrides``       — ordered per-app/tag exceptions (first match wins).
    """

    min_severity: Severity = Severity.SEV2
    forward_states: list[IncidentState] = Field(default_factory=list)
    overrides: list[FederationOverride] = Field(default_factory=list)

    def decide_forward(self, incident: Incident) -> bool:
        """Return True if *incident* should be forwarded to the central Hub.

        Evaluation order:

        1. The first matching override (file order) decides, if it carries an
           explicit ``forward: always|never`` or a slice ``min_severity``.
        2. Otherwise the global ``min_severity`` gate applies.
        3. The ``forward_states`` filter (global) is ANDed on top of both —
           even an override-forced ``always`` respects the state filter so a
           RESOLVED redelivery isn't re-forwarded when states are restricted.

        Loop prevention (``relay_forwarded_from``) is handled by the caller.
        """
        if self.forward_states and incident.state not in self.forward_states:
            return False

        effective_min = self.min_severity
        for override in self.overrides:
            if not override.matches(incident):
                continue
            if override.forward == "never":
                return False
            if override.forward == "always":
                return True
            if override.min_severity is not None:
                effective_min = override.min_severity
            break

        return _severity_rank(incident.severity) <= _severity_rank(effective_min)


class SuppressionRule(BaseModel):
    """A per-app / per-tag override of the global suppression window.

    Matched against an already-classified :class:`Incident` at the Node, before
    it is persisted or paged. All present match fields are ANDed; omitted fields
    match anything. The first matching rule wins (file order).

    A match overrides ``window_seconds`` and/or ``max_per_window`` for the slice
    — e.g. a chatty health-check alarm can be throttled harder than the default.

    Match fields (all optional):

    * ``app_name``           — exact match on the incident's app name.
    * ``alarm_name_prefix``  — the alarm name must start with this string.
    * ``environment``        — match on the incident's environment: a single
                               name (``prod``) or a list of names
                               (``[dev, test, preprod]``) for a category.
    * ``tags``               — all key/value pairs must be present in the tags.
    """

    name: str | None = None
    app_name: str | None = None
    alarm_name_prefix: str | None = None
    environment: str | list[str] | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    window_seconds: int | None = Field(default=None, gt=0)
    max_per_window: int | None = Field(default=None, ge=1)

    def matches(self, incident: Incident) -> bool:
        """Return True if this rule applies to *incident* (AND logic)."""
        if self.app_name is not None and incident.app_name != self.app_name:
            return False
        if self.alarm_name_prefix is not None and not incident.alarm_name.startswith(
            self.alarm_name_prefix
        ):
            return False
        if not _environment_matches(self.environment, incident.environment):
            return False
        for key, value in self.tags.items():
            if incident.tags.get(key) != value:
                return False
        return True


class SuppressionConfig(BaseModel):
    """Node-side noise suppression: dedup, rate-limit, and flapping in one model.

    Lives under the ``suppression:`` key of routing.yaml. The three behaviours
    collapse into a single windowed-counter primitive — "how many incidents for
    this logical alarm have fired in the current window?":

    * **dedup**       — ``max_per_window: 1``: a re-fire of the same alarm inside
                        the window is suppressed (only the first pages).
    * **rate-limit**  — ``max_per_window: N``: at most N pages per window.
    * **flapping**    — the same knob tuned tight (short window, low N) tames an
                        alarm oscillating across its threshold.

    An incident is suppressed when the window's post-increment hit count exceeds
    ``max_per_window``. Suppressed incidents are not persisted, paged, or
    federated — the whole point is to stop the noise at the source.

    ``exempt_severities`` always bypass suppression (default ``[SEV1]``): a
    critical outage must page every time, even if it flaps.

    The dedup key is ``account_id + app_name + alarm_name`` — one logical alarm,
    regardless of how many raw state-changes CloudWatch emits.
    """

    enabled: bool = False
    window_seconds: int = Field(default=300, gt=0)
    max_per_window: int = Field(default=1, ge=1)
    exempt_severities: list[Severity] = Field(default_factory=lambda: [Severity.SEV1])
    rules: list[SuppressionRule] = Field(default_factory=list)

    def limits_for(self, incident: Incident) -> tuple[int, int]:
        """Return the (window_seconds, max_per_window) that apply to *incident*.

        Walks ``rules`` in file order; the first match overrides the global
        values (a rule may override either field independently). Falls back to
        the global window/max when no rule matches.
        """
        window = self.window_seconds
        maximum = self.max_per_window
        for rule in self.rules:
            if rule.matches(incident):
                if rule.window_seconds is not None:
                    window = rule.window_seconds
                if rule.max_per_window is not None:
                    maximum = rule.max_per_window
                break
        return window, maximum

    def is_exempt(self, incident: Incident) -> bool:
        """Return True if *incident*'s severity bypasses suppression entirely."""
        return incident.severity in self.exempt_severities

    def is_suppressed(self, incident: Incident, current_count: int) -> bool:
        """Decide suppression given the window's post-increment hit count.

        ``current_count`` is the count *after* recording this incident (so the
        first incident in a window is 1). Suppress once the count exceeds the
        applicable ``max_per_window``. Exempt severities are never suppressed.
        """
        if not self.enabled or self.is_exempt(incident):
            return False
        _, maximum = self.limits_for(incident)
        return current_count > maximum


class IgnoreRule(BaseModel):
    """A rule that permanently drops an incident before it is persisted or paged.

    Matched against an already-classified :class:`Incident` at the Node, before
    it is persisted or paged. All present match fields are ANDed; omitted fields
    match anything. The first matching rule wins (DynamoDB order).

    Unlike :class:`SuppressionRule`, which throttles noisy alarms, an ignore rule
    silently drops the incident entirely — it will not appear in the incident list,
    generate a page, or federate upstream.

    Match fields (all optional):

    * ``account_id``         — exact match on the incident's AWS account ID.
    * ``app_name``           — exact match on the incident's app name.
    * ``alarm_name``         — exact match on the incident's alarm name.
    * ``alarm_name_prefix``  — the alarm name must start with this string.
    * ``environment``        — match on the incident's environment: a single
                               name (``prod``) or a list of names
                               (``[dev, test, preprod]``) for a category.
    * ``tags``               — all key/value pairs must be present in the
                               incident's tags.
    """

    name: str | None = None
    account_id: str | None = None
    app_name: str | None = None
    alarm_name: str | None = None
    alarm_name_prefix: str | None = None
    environment: str | list[str] | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    note: str | None = None
    enabled: bool = True
    created_by: str | None = None
    created_at: datetime | None = None

    def matches(self, incident: Incident) -> bool:
        """Return True if this rule applies to *incident* (AND logic)."""
        if self.account_id is not None and incident.account_id != self.account_id:
            return False
        if self.app_name is not None and incident.app_name != self.app_name:
            return False
        if self.alarm_name is not None and incident.alarm_name != self.alarm_name:
            return False
        if self.alarm_name_prefix is not None and not incident.alarm_name.startswith(
            self.alarm_name_prefix
        ):
            return False
        if not _environment_matches(self.environment, incident.environment):
            return False
        for key, value in self.tags.items():
            if incident.tags.get(key) != value:
                return False
        return True


class IgnoreConfig(BaseModel):
    """Node-side ignore rules: drop incidents entirely before persist or page.

    Lives under the ``ignore:`` key of routing.yaml. When ``enabled`` is True
    and at least one :class:`IgnoreRule` matches an incoming incident, the
    incident is silently dropped — it will not be persisted, paged, or federated.

    Use this for permanent "never care about this alarm" decisions. For
    transient noise / rate-limiting use :class:`SuppressionConfig` instead.
    """

    enabled: bool = True
    rules: list[IgnoreRule] = Field(default_factory=list)

    def first_match(self, incident: Incident) -> IgnoreRule | None:
        """Return the first enabled rule that matches *incident*, or None.

        Returns None immediately when ``enabled`` is False (the whole config is
        off) or when no enabled rule matches.
        """
        if not self.enabled:
            return None
        for rule in self.rules:
            if rule.enabled and rule.matches(incident):
                return rule
        return None


class RoutingConfig(BaseModel):
    """Root model for the routing.yaml config file.

    Wraps the ordered list of :class:`RoutingRule` objects, a fallback
    escalation policy, and the default notification streams. Rules are
    evaluated in ascending priority order; the validator enforces that
    they are stored that way to make the file self-documenting.

    ``federation`` is optional: when present it drives the local Hub → central
    Hub forwarding gate; when absent the Hub falls back to its env-var knobs.
    ``suppression`` is optional: when present and enabled it drives Node-side
    dedup / rate-limit / flapping suppression before an incident is paged.
    ``ignore`` is optional: when present and enabled it drops matching incidents
    entirely before they are persisted, paged, or federated.
    """

    rules: list[RoutingRule]
    default_escalation_policy_id: str
    default_streams: list[Stream]
    federation: FederationConfig | None = None
    suppression: SuppressionConfig | None = None
    ignore: IgnoreConfig | None = None

    @model_validator(mode="after")
    def rules_are_sorted_by_priority(self) -> RoutingConfig:
        """Raise ValueError if routing rules are not in ascending priority order.

        Priority values must be strictly increasing so that the evaluation
        order in routing.yaml matches the evaluation order at runtime.
        """
        priorities = [rule.priority for rule in self.rules]
        if priorities != sorted(priorities):
            raise ValueError(
                "RoutingRule list must be sorted by priority in ascending order. "
                f"Found priorities: {priorities}"
            )
        return self


class EnvironmentDef(BaseModel):
    """A single declared environment."""
    name: str
    ou_path: str | None = None
    name_convention_regex: str | None = None  # regex matched against alarm/resource name

class DeploymentConfig(BaseModel):
    """Infrastructure/deploy knobs, consumed by the IaC scripts (not the app).

    Centralizes what used to be ad-hoc CDK context flags so a team declares its
    deploy topology once, in ``environments.yaml``, and the ``scripts/relay-*.sh``
    deploy helpers translate it into ``-c relay:*`` context. The running container
    ignores this block.

    * ``private_hosted_zone_id`` / ``private_hosted_zone_name`` — a Route53
      **private** hosted zone to publish the ALB's friendly DNS record into.
      When set, the stack issues an ACM cert for ``<alb_subdomain>.<zone_name>``
      and serves the dashboard over HTTPS at that name.
    * ``alb_subdomain`` — the left-most DNS label for the ALB record
      (default ``relay`` → ``relay.<zone_name>``).
    * ``certificate_arn`` — an explicit ACM cert ARN. Overrides the
      PHZ-derived cert when you already manage one.
    * ``internal_alb`` — internal (private-subnet) ALB by default; ``false``
      opts into a public, internet-facing ALB.
    """

    private_hosted_zone_id: str | None = None
    private_hosted_zone_name: str | None = None
    alb_subdomain: str = "relay"
    certificate_arn: str | None = None
    internal_alb: bool = True


class AccessControlConfig(BaseModel):
    """Fine-grained write-access allowlist, matched against the OIDC username.

    Deliberately simple — no roles, no groups. When ``enabled`` is true, a write
    action is permitted only if the authenticated OIDC username (subject /
    preferred_username / email, as extracted from the ALB OIDC token) appears in
    ``allowed_users``. When disabled, any authenticated identity may write (the
    pre-existing behaviour). Has no effect when auth mode is ``none``.
    """

    enabled: bool = False
    allowed_users: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    """UI authentication config (``auth:`` block of environments.yaml).

    * ``mode`` — ``none`` | ``alb`` | ``dev``. ``None`` leaves the stack's
      environment-aware default in force (prod → none, non-prod → dev). The OIDC
      setup helper flips this to ``alb`` once an ALB OIDC listener is wired.
    * ``access_control`` — the optional fine-grained write allowlist above.
    """

    mode: str | None = None
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)


class EnvironmentsConfig(BaseModel):
    """Config model for environments.yaml."""
    environments: list[EnvironmentDef]
    default_environment: str = "unrouted"
    account_environment_map: dict[str, str] = Field(default_factory=dict)  # account_id -> env name
    deployment: DeploymentConfig | None = None  # IaC-only; ignored by the running app
    auth: AuthConfig | None = None              # UI auth mode + write allowlist

    @model_validator(mode="after")
    def names_are_unique(self) -> EnvironmentsConfig:
        seen: set[str] = set()
        for env in self.environments:
            if env.name in seen:
                raise ValueError(f"Duplicate environment name: '{env.name}'")
            seen.add(env.name)
        return self


class DeploymentDefaults(BaseModel):
    """Org-wide deployment metadata conventions (declared once in hierarchy.yaml).

    ``tag_map`` maps a metadata key to the resource-tag name it is sourced from,
    applied to EVERY deployment so teams declare COMPONENT_ID/GIT_SHA/... once
    instead of on every leaf. A per-deployment ``metadata`` entry overrides it.
    """

    tag_map: dict[str, str] = Field(default_factory=dict)  # metadata_key -> RESOURCE_TAG_NAME


class HierarchyConfig(BaseModel):
    """Config model for hierarchy.yaml — declares levels, which level is leaf, etc."""

    levels: list[str]           # ordered list: e.g. [product_line, product, component, deployment]
    leaf_level: str             # which level name is the leaf (e.g. "deployment")
    deployment_defaults: DeploymentDefaults | None = None


class CatalogConfig(BaseModel):
    """Config model for catalog.yaml — flat list of nodes."""
    nodes: list[OrgNode]

    @model_validator(mode="after")
    def ids_are_unique(self) -> CatalogConfig:
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"Duplicate node id in catalog: '{node.id}'")
            seen.add(node.id)
        return self


class RelayConfig(BaseModel):
    """Top-level composite config object for the Relay system.

    Aggregates the config domains (escalation and routing) into a single
    validated object that the rest of the application depends on.
    The ``loaded_at`` timestamp records when the config was fetched from GitLab,
    which is useful for staleness checks and audit logging.

    Example usage::

        config = RelayConfig.from_yaml_files(
            escalation_yaml=open("escalation.yaml").read(),
            routing_yaml=open("routing.yaml").read(),
        )
    """

    escalation: EscalationConfig
    routing: RoutingConfig
    loaded_at: datetime
    environments: EnvironmentsConfig | None = None
    hierarchy: HierarchyConfig | None = None
    catalog: CatalogConfig | None = None
    # Built OrgTree — populated by loader after catalog is loaded
    org_tree: OrgTree | None = None

    model_config = {"arbitrary_types_allowed": True}  # OrgTree is not a BaseModel

    @classmethod
    def from_yaml_files(
        cls,
        escalation_yaml: str,
        routing_yaml: str,
    ) -> RelayConfig:
        """Parse raw YAML strings and return a validated :class:`RelayConfig`.

        Each string is expected to be the full text content of the corresponding
        config file as stored in GitLab.  Parsing uses ``yaml.safe_load`` to
        avoid arbitrary code execution from untrusted YAML.

        Args:
            escalation_yaml: Raw YAML text of ``escalation.yaml``.
            routing_yaml:    Raw YAML text of ``routing.yaml``.

        Returns:
            A fully validated :class:`RelayConfig` with ``loaded_at`` set to
            the current UTC time.

        Raises:
            yaml.YAMLError: If any of the input strings is not valid YAML.
            pydantic.ValidationError: If the parsed data does not satisfy the
                schema constraints (e.g. duplicate IDs, misordered priorities).
        """
        escalation_data = yaml.safe_load(escalation_yaml)
        routing_data = yaml.safe_load(routing_yaml)

        return cls(
            escalation=EscalationConfig.model_validate(escalation_data),
            routing=RoutingConfig.model_validate(routing_data),
            loaded_at=datetime.now(UTC),
        )

    @classmethod
    def empty(cls) -> RelayConfig:
        """Return a valid, empty config for graceful degradation.

        Used when no config source is reachable at cold-start so the Node can
        still ingest alarms and page via fallbacks (no policies, no routing
        rules). Downstream code already handles empty lists.
        """
        return cls(
            escalation=EscalationConfig(policies=[]),
            routing=RoutingConfig(
                rules=[],
                default_escalation_policy_id="",
                default_streams=[],
            ),
            loaded_at=datetime.now(UTC),
        )

    @classmethod
    def from_yaml_files_extended(
        cls,
        escalation_yaml: str,
        routing_yaml: str,
        environments_yaml: str | None = None,
        hierarchy_yaml: str | None = None,
        catalog_yaml: str | None = None,
    ) -> RelayConfig:
        """Extended version that also loads environments, hierarchy, and catalog."""
        escalation_data = yaml.safe_load(escalation_yaml)
        routing_data = yaml.safe_load(routing_yaml)

        environments: EnvironmentsConfig | None = None
        if environments_yaml:
            env_data = yaml.safe_load(environments_yaml)
            environments = EnvironmentsConfig.model_validate(env_data)

        hierarchy: HierarchyConfig | None = None
        if hierarchy_yaml:
            hier_data = yaml.safe_load(hierarchy_yaml)
            hierarchy = HierarchyConfig.model_validate(hier_data)

        catalog: CatalogConfig | None = None
        org_tree: OrgTree | None = None
        if catalog_yaml:
            cat_data = yaml.safe_load(catalog_yaml)
            catalog = CatalogConfig.model_validate(cat_data)
            leaf_level = hierarchy.leaf_level if hierarchy else "deployment"
            org_tree = OrgTree(nodes=catalog.nodes, leaf_level=leaf_level)

        return cls(
            escalation=EscalationConfig.model_validate(escalation_data),
            routing=RoutingConfig.model_validate(routing_data),
            environments=environments,
            hierarchy=hierarchy,
            catalog=catalog,
            org_tree=org_tree,
            loaded_at=datetime.now(UTC),
        )
