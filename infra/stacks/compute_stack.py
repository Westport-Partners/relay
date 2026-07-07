"""
RelayComputeStack — the always-on container plane (collapse Step 4)
===================================================================
The second deploy target: VPC (or BYOV import), ECS cluster, Fargate service +
task def, ALB, the CloudWatch-alarm EventBridge rule → SQS ingress queue + DLQ,
and the two IAM roles (task + execution). Imports the data stack's table + SNS
topics by name/ARN, so a compute redeploy (every image change) never touches the
data plane.

This is the collapsed single-container runtime (collapsed-single-container plan
§1, §5): one always-on container runs detection in-process and serves the
dashboard. **Gone vs. the old hub_stack:** the Node Lambda, EventBridge
Scheduler, the concierge/reaper scale-to-zero machinery, and the
amazonlinux-placeholder fallback. Net IAM surface: one task role + one exec role.

Brittleness fixes folded in (debug doc §"Deploy gotchas"):
  - Fail-fast at synth on a missing/placeholder image (no silent amazonlinux).
  - ECS deployment circuit breaker WITH rollback (a bad image rolls back instead
    of wedging CFN).

Context keys consumed (see also README):
  relay:role               — "team" (default) | "federated-hub"
  relay:hub_scope          — "local" (team default) | "local-federated" | "central"
  relay:hub_image_uri      — REQUIRED ECR image URI for the container
  relay:org_id             — org id (scope=central bus policy is in RelayFederationStack)
  relay:central_hub_bus_arn — upstream bus ARN (scope=local-federated forwarding)
  relay:vpc_id             — BYOV: import this VPC instead of creating one
  relay:ecs_execution_role_arn / relay:ecs_task_role_arn — BYOR ECS roles
  relay:certificate_arn    — ACM cert ARN for ALB HTTPS (explicit; optional)
  relay:phz_id             — Route53 private hosted zone ID (optional; used with
                             relay:phz_name to auto-mint a cert + DNS record)
  relay:phz_name           — Private hosted zone name, e.g. "corp.example.internal"
                             (optional; used with relay:phz_id)
  relay:alb_subdomain      — Left DNS label for the dashboard record (default "relay")
  relay:internal_alb       — "true" (default): internal ALB in private subnets
                             (corporate-network/VPN reachable). "false": public,
                             internet-facing ALB. Tasks stay private either way.
  relay:servicenow_instance — ServiceNow plain instance hostname (env var only)
  relay:enable_direct_sms / relay:ai_enabled / relay:ai_provider /
  relay:ai_base_url / relay:ai_model_id / relay:ai_api_key_secret — AI + SMS
  relay:auth_mode          — none | alb | dev. Unset → environment-aware default:
                             prod boards lock to "none" (read-only), non-prod
                             boards come up "dev" (write-capable). See
                             resolve_auth_mode().
  relay:access_control     — "true"/"false" — enable per-user access control
  relay:auth_allowed_users — comma-separated usernames allowed when access control
                             is on (passed as RELAY_AUTH_ALLOWED_USERS)
  relay:dev_user / relay:config_source / relay:tz / relay:log_level
  relay:cpu_arch           — X86_64 (default) | ARM64. Must match the pushed
                             image's architecture; relay-context.sh auto-detects
                             the build host and sets it (override: RELAY_CPU_ARCH)
  Node self-identity (the container now owns these — were on the Node Lambda):
  relay:app_name / relay:deployment_id / relay:environment / relay:service_path /
  relay:org_path

HTTPS by default: when relay:phz_id + relay:phz_name are both supplied, the stack
auto-mints an ACM cert (DNS-validated in that zone) and the ALB listener is
HTTPS:443 with an HTTP→HTTPS redirect. Alternatively, pass relay:certificate_arn
to bring your own cert. Without either, the ALB falls back to HTTP:80 and a synth
warning is emitted.
"""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cw_actions,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_ecs_patterns as ecs_patterns,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_route53 as route53,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from constructs import Construct

# Placeholder images we must never silently synth (fail-fast guard).
_PLACEHOLDER_IMAGE_MARKERS = ("amazonlinux", "PLACEHOLDER")

# Read-only tag-lookup actions the in-process detection pipeline uses to resolve
# an alarm's + monitored resource's tags (the COMPONENT_ID/GIT_SHA/GITLAB_* join
# keys → Incident.tags). None support resource-level scoping, hence "*". Driven
# by RELAY_RESOLVE_ALARM_TAGS (default on). Carried over from the retired Node
# Lambda role — the always-on container now owns detection, so it needs them.
_ALARM_TAG_ACTIONS = [
    "cloudwatch:ListTagsForResource",
    "lambda:ListTags",
    "sqs:ListQueueTags",
    "ecs:ListTagsForResource",
    "ec2:DescribeTags",
]


def resolve_auth_mode(explicit: str | None, environment: str | None) -> str:
    """Resolve the UI auth mode for the Hub container.

    An explicit ``relay:auth_mode`` always wins. When it is unset the default is
    environment-aware so a non-prod team board is usable out of the box:

    * prod  → ``none``  (read-only; never accidentally write-open)
    * other → ``dev``   (write-capable so operators can ack/resolve + edit
                         contacts without first wiring an IdP)

    "other" deliberately includes ``unrouted`` (the default when no environment
    is set), since only an explicit ``prod`` should get the locked-down posture.
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    return "none" if (environment or "").strip().lower() == "prod" else "dev"


def resolve_internal_alb(explicit: str | None) -> bool:
    """Resolve whether the ALB is internal (private subnets) or internet-facing.

    Defaults to internal — most orgs run Relay as an internal utility reachable
    only from the corporate network/VPN. Only an explicit ``relay:internal_alb``
    of ``false`` (case-insensitive, whitespace-trimmed) opts into a public,
    internet-facing ALB; anything else (unset, ``true``, junk) stays internal.
    Either way the ECS tasks themselves remain private.
    """
    return (explicit or "true").strip().lower() != "false"


def resolve_cpu_architecture(explicit: str | None) -> str:
    """Resolve the Fargate task CPU architecture from ``relay:cpu_arch``.

    Returns the normalized sentinel ``"ARM64"`` or ``"X86_64"`` (the caller maps
    it to ``ecs.CpuArchitecture`` — the CDK enum members are jsii proxies that
    are not identity- or value-comparable, so this helper stays a plain string
    for unit-testability).

    Fargate defaults a task def with no ``runtime_platform`` to X86_64; an ARM64
    image (built on an aarch64 host) then fails at launch with "exec format
    error". ``relay-context.sh`` auto-detects the build-host arch and passes
    ``relay:cpu_arch`` so ARM64 hosts deploy ARM64 tasks with no operator action.

    Only an explicit ``ARM64`` (case-insensitive, whitespace-trimmed) selects
    ARM64; anything else (unset, ``X86_64``, junk) stays X86_64 — the safe
    default that matches Fargate's own.
    """
    return "ARM64" if (explicit or "").strip().upper() == "ARM64" else "X86_64"


def resolve_certificate(
    stack: Stack,
    *,
    certificate_arn: str,
    phz_id: str,
    phz_name: str,
    alb_subdomain: str,
) -> tuple[acm.ICertificate | None, route53.IHostedZone | None, str | None]:
    """Resolve an ACM certificate (and optional Route53 zone + FQDN) for the ALB.

    Resolution priority:
    1. ``relay:certificate_arn`` supplied — import it; derive fqdn from phz_name
       if available (no DNS minting needed — cert already exists).
    2. ``relay:phz_id`` + ``relay:phz_name`` both set — look up the private hosted
       zone, then mint a new ACM cert with DNS validation.  DNS validation writes
       CNAME records into the zone — for a **private-only** zone that cannot be
       reached from the public internet, ACM cannot complete DNS validation
       automatically; in that case pass ``relay:certificate_arn`` (option 1) with a
       cert you've already validated and issued.
    3. Neither supplied — returns ``(None, None, None)``; the ALB falls back to
       HTTP:80 and a synth warning is emitted.

    Returns:
        (cert_or_None, hosted_zone_or_None, fqdn_or_None)
    """
    fqdn = f"{alb_subdomain}.{phz_name}" if phz_name else None

    if certificate_arn:
        cert = acm.Certificate.from_certificate_arn(
            stack, "RelayCert", certificate_arn
        )
        zone = None
        if phz_id and phz_name:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                stack,
                "RelayPhz",
                hosted_zone_id=phz_id,
                zone_name=phz_name,
            )
        return cert, zone, fqdn

    if phz_id and phz_name:
        zone = route53.HostedZone.from_hosted_zone_attributes(
            stack,
            "RelayPhz",
            hosted_zone_id=phz_id,
            zone_name=phz_name,
        )
        # DNS validation writes a CNAME record into the hosted zone. For a
        # private-only zone not reachable from the public internet, ACM cannot
        # complete automatic DNS validation — supply relay:certificate_arn instead.
        assert fqdn is not None  # guaranteed: phz_name is truthy in this branch
        cert = acm.Certificate(
            stack,
            "RelayCert",
            domain_name=fqdn,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        return cert, zone, fqdn

    return None, None, None


class RelayComputeStack(Stack):
    """Always-on Fargate container + ALB + SQS ingress. Imports the data plane.

    Args:
        data_table_name / data_table_arn:   the RelayDataStack table to import.
        paging_topic_arn:                    team paging SNS topic ARN.
        central_paging_topic_arn:            central paging SNS topic ARN.
        role:                                "team" | "federated-hub".
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_table_name: str,
        data_table_arn: str,
        paging_topic_arn: str,
        central_paging_topic_arn: str,
        role: str = "team",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Context
        # ------------------------------------------------------------------
        resolved_scope: str = (
            self.node.try_get_context("relay:hub_scope")
            or ("central" if role == "federated-hub" else "local")
        )
        central_hub_bus_arn: str = (
            self.node.try_get_context("relay:central_hub_bus_arn") or ""
        )
        servicenow_instance: str = (
            self.node.try_get_context("relay:servicenow_instance") or ""
        )
        certificate_arn: str = self.node.try_get_context("relay:certificate_arn") or ""
        phz_id: str = self.node.try_get_context("relay:phz_id") or ""
        phz_name: str = self.node.try_get_context("relay:phz_name") or ""
        alb_subdomain: str = (
            self.node.try_get_context("relay:alb_subdomain") or "relay"
        )
        # ALB exposure. Defaults to INTERNAL (internet_facing=False) — most orgs
        # run Relay as an internal utility reachable only from the corporate
        # network/VPN. Set relay:internal_alb=false for an internet-facing ALB
        # (e.g. an account with no VPN/peering into the VPC). Either way the ECS
        # tasks stay private (assign_public_ip=False); this only moves the load
        # balancer between the VPC's public and private subnets.
        internal_alb: bool = resolve_internal_alb(
            self.node.try_get_context("relay:internal_alb")
        )
        # In-account alarm/resource tag resolution (default on). Gates both the
        # tag-read IAM grant and the RELAY_RESOLVE_ALARM_TAGS container env.
        resolve_alarm_tags: bool = (
            str(self.node.try_get_context("relay:resolve_alarm_tags") or "true").lower()
            != "false"
        )
        log_level: str = self.node.try_get_context("relay:log_level") or "INFO"

        # Service capacity floor. Default 1 — incidents, escalation deadlines, and
        # the SQS ingress are all durable, so a task cycling loses no state. Set
        # higher (e.g. 2) for cross-AZ HA where a single task is unacceptable.
        # Deploys stay zero-downtime at any floor: minHealthyPercent=100 +
        # maxHealthyPercent=200 force a new healthy task up BEFORE the old one
        # drains (the default 50% would let ECS stop the sole task first).
        min_capacity: int = int(self.node.try_get_context("relay:min_capacity") or 1)
        max_capacity: int = int(
            self.node.try_get_context("relay:max_capacity") or 8
        )

        # BYOR — pre-provisioned ECS roles (accounts that forbid role creation).
        ecs_execution_role_arn: str = (
            self.node.try_get_context("relay:ecs_execution_role_arn") or ""
        )
        ecs_task_role_arn: str = (
            self.node.try_get_context("relay:ecs_task_role_arn") or ""
        )
        byor_mode: bool = bool(ecs_execution_role_arn and ecs_task_role_arn)
        vpc_id: str = self.node.try_get_context("relay:vpc_id") or ""

        # ------------------------------------------------------------------
        # Fail-fast on a missing/placeholder image (debug doc gotcha #1). No
        # scale-to-zero means no circuit-breaker-off wedge, but a silent
        # amazonlinux synth still crash-loops ECS — so we raise at synth.
        # ------------------------------------------------------------------
        hub_image_uri: str = self.node.try_get_context("relay:hub_image_uri") or ""
        # `cdk synth/deploy RelayDataStack` still executes this whole app, so this
        # guard would fire on the compute stack even when ONLY the data plane is
        # being deployed — blocking the documented "data plane first" step, which
        # is the only step a locked-down account (PassRole/CreateRole denied) can
        # run before an image exists. relay:image_check=false skips the guard; the
        # deploy scripts set it automatically whenever RelayComputeStack is not a
        # deploy target (relay-context.sh), and relay-bootstrap.sh sets it too.
        image_check = str(
            self.node.try_get_context("relay:image_check") or "true"
        ).lower() != "false"
        if image_check and (
            not hub_image_uri
            or any(m in hub_image_uri for m in _PLACEHOLDER_IMAGE_MARKERS)
        ):
            raise ValueError(
                "relay:hub_image_uri must be a real ECR image URI for the Relay "
                f"container (got {hub_image_uri!r}). Build + push the image first "
                "(scripts/relay-build-hub-image.sh) — never synth a placeholder. "
                "If you are deploying ONLY the data plane (no compute), pass "
                "-c relay:image_check=false (the deploy scripts do this for you "
                "when RelayComputeStack is not a target)."
            )
        # A non-deploy invocation with no image still needs *something* to build
        # the container construct; use an obviously-fake tag that can never reach
        # a real deploy (the guard above blocks deploys without a real image).
        if not hub_image_uri:
            hub_image_uri = "relay-bootstrap-noop:unused"

        # ------------------------------------------------------------------
        # Import the data plane (RelayDataStack) — table + topics by name/ARN.
        # ------------------------------------------------------------------
        fleet_table = dynamodb.Table.from_table_attributes(
            self,
            "RelayDataTable",
            table_arn=data_table_arn,
            global_indexes=["incident-status-index"],
        )
        central_paging_topic = self._import_topic(
            "RelayCentralPagingTopic", central_paging_topic_arn
        )
        # The team paging topic ARN rides through to the container as an env var;
        # the container publishes via boto3, so we only need the ARN + a grant.

        # ------------------------------------------------------------------
        # VPC — BYOV import or create.
        # ------------------------------------------------------------------
        vpc: ec2.IVpc
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "RelayHubVpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc(
                self,
                "RelayHubVpc",
                max_azs=2,
                nat_gateways=1,
                restrict_default_security_group=not byor_mode,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                    ),
                    ec2.SubnetConfiguration(
                        name="Private",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                        cidr_mask=24,
                    ),
                ],
            )

        # ------------------------------------------------------------------
        # Secrets (opt-in integrations).
        # ------------------------------------------------------------------
        # GitLab and ServiceNow integration tokens are supplied at runtime via the
        # Settings UI (DynamoDB-backed settings store). The stack no longer imports
        # relay/gitlab-token or relay/servicenow-credentials from Secrets Manager.
        ai_api_key_secret_name: str = (
            self.node.try_get_context("relay:ai_api_key_secret") or ""
        )
        ai_key_secret = None
        if ai_api_key_secret_name:
            ai_key_secret = secretsmanager.Secret.from_secret_name_v2(
                self, "RelayAIApiKey", secret_name=ai_api_key_secret_name
            )

        # ------------------------------------------------------------------
        # ECS cluster + roles.
        # ------------------------------------------------------------------
        cluster = ecs.Cluster(
            self, "RelayHubCluster", cluster_name="relay-hub", vpc=vpc,
            container_insights=True,
        )

        if byor_mode:
            execution_role = iam.Role.from_role_arn(
                self, "RelayHubExecutionRole", ecs_execution_role_arn, mutable=False
            )
        else:
            execution_role = iam.Role(
                self,
                "RelayHubExecutionRole",
                role_name="relay-hub-ecs-execution",
                assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AmazonECSTaskExecutionRolePolicy"
                    )
                ],
            )
        if byor_mode:
            task_role = iam.Role.from_role_arn(
                self, "RelayHubTaskRole", ecs_task_role_arn, mutable=False
            )
        else:
            task_role = iam.Role(
                self,
                "RelayHubTaskRole",
                role_name="relay-hub-ecs-task",
                assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            )
            fleet_table.grant_read_write_data(task_role)
            central_paging_topic.grant_publish(task_role)
            # The container also publishes to the team paging topic (resolved
            # on-call). Grant by ARN (imported topic). ListSubscriptionsByTopic +
            # Subscribe back the Contacts screen's per-contact subscription state
            # + Subscribe button (#78): operators subscribe by email, so the Hub
            # lists the topic's subscriptions and can add an email endpoint.
            task_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="RelayTeamPaging",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sns:Publish",
                        "sns:ListSubscriptionsByTopic",
                        "sns:Subscribe",
                        # GetTopicAttributes backs the /health/ready
                        # sns_paging_topic probe.
                        "sns:GetTopicAttributes",
                    ],
                    resources=[paging_topic_arn],
                )
            )
            # In-account alarm + resource tag resolution (default on). The
            # in-process detection pipeline reads the alarm's + monitored
            # resource's tags to populate Incident.tags (COMPONENT_ID/GIT_SHA/
            # GITLAB_* join keys). These APIs don't support resource scoping.
            if resolve_alarm_tags:
                task_role.add_to_principal_policy(
                    iam.PolicyStatement(
                        sid="RelayAlarmTagResolution",
                        effect=iam.Effect.ALLOW,
                        actions=_ALARM_TAG_ACTIONS,
                        resources=["*"],
                    )
                )

        # AI / SMS toggles.
        enable_direct_sms = (
            str(self.node.try_get_context("relay:enable_direct_sms")).lower() == "true"
        )
        ai_enabled = str(self.node.try_get_context("relay:ai_enabled")).lower() == "true"
        ai_provider: str = (self.node.try_get_context("relay:ai_provider") or "").lower()
        ai_base_url: str = self.node.try_get_context("relay:ai_base_url") or ""
        ai_uses_bedrock = ai_enabled and ai_provider in ("", "bedrock", "bedrock-converse")

        if enable_direct_sms and not byor_mode:
            task_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="RelayHubDirectSms",
                    effect=iam.Effect.ALLOW,
                    # CheckIfPhoneNumberIsOptedOut backs the /health/ready
                    # sns_direct_sms probe (only run when direct SMS is enabled).
                    actions=["sns:Publish", "sns:CheckIfPhoneNumberIsOptedOut"],
                    # Direct-to-phone SMS: Publish(PhoneNumber=...) with no topic
                    # ARN, so the resource is "*". Scope by region rather than by
                    # sns:Protocol — sns:Protocol is a Subscribe-only condition key
                    # and is absent from a Publish request context, so gating on it
                    # here would fail closed and silently break direct paging.
                    resources=["*"],
                    conditions={
                        "StringEquals": {"aws:RequestedRegion": self.region}
                    },
                )
            )
        if ai_uses_bedrock and not byor_mode:
            task_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="RelayHubBedrockInvoke",
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock:InvokeModel", "bedrock:Converse"],
                    resources=[
                        f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                        "arn:aws:bedrock:*::foundation-model/*",
                    ],
                )
            )
        if ai_key_secret is not None and not byor_mode:
            ai_key_secret.grant_read(task_role)

        # ------------------------------------------------------------------
        # Ingestion: EventBridge "CloudWatch Alarm State Change" → SQS → container.
        # Step 3 made the container consume + detect in-process; the SQS queue is
        # kept purely as the durable buffer for alarms arriving during a redeploy
        # (plan §9 open question 1). The rule lives here (in the account being
        # monitored). A federated hub also receives forwarded Relay events on its
        # bus — that bus + its ingest rule live in RelayFederationStack.
        # ------------------------------------------------------------------
        ingest_dlq = sqs.Queue(
            self, "RelayHubIngestDLQ", queue_name="relay-hub-ingest-dlq",
            retention_period=Duration.days(14),
        )
        ingest_queue = sqs.Queue(
            self,
            "RelayHubIngestQueue",
            queue_name="relay-hub-ingest",
            visibility_timeout=Duration.seconds(60),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=ingest_dlq),
        )
        if not byor_mode:
            ingest_queue.grant_consume_messages(task_role)

        # Poison-message visibility (issue #21): a message that fails to process
        # 5 times is redriven to the DLQ by the policy above. That move is silent
        # — without an alarm an operator never learns ingestion is dropping
        # events. Alarm on any message landing in the DLQ and notify out-of-band
        # via the team paging topic (NOT back through the ingest pipeline, which
        # is exactly what's broken when the DLQ fills). Self-contained per
        # account: each deployment watches its own DLQ, so a federated hub never
        # needs to know about — or reach into — any team account's queues.
        dlq_paging_topic = self._import_topic(
            "RelayDLQPagingTopic", paging_topic_arn
        )
        dlq_depth_alarm = cloudwatch.Alarm(
            self,
            "RelayHubIngestDLQDepthAlarm",
            alarm_name="relay-hub-ingest-dlq-not-empty",
            alarm_description=(
                "Relay ingest DLQ has poison messages — events failed to process "
                "5x and were redriven. Investigate the un-parseable message(s)."
            ),
            metric=ingest_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5),
                statistic="Maximum",
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_depth_alarm.add_alarm_action(
            cw_actions.SnsAction(dlq_paging_topic)
        )

        # The account-local CloudWatch alarm rule (zero-config seam): every alarm
        # state change → ALARM is delivered to the ingest queue. The container
        # parses + detects in-process (Step 3).
        events.Rule(
            self,
            "RelayCloudWatchAlarmRule",
            rule_name="relay-cloudwatch-alarm",
            description="Route CloudWatch alarm state changes to the Relay ingest queue.",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                detail={"state": {"value": ["ALARM"]}},
            ),
            targets=[targets.SqsQueue(ingest_queue)],
        )

        # ------------------------------------------------------------------
        # Task definition + container.
        # ------------------------------------------------------------------
        # CPU architecture of the pushed image. Fargate defaults a task def with
        # no runtime_platform to X86_64; an ARM64 image (built on an aarch64 host)
        # then fails at launch with "exec format error". relay-context.sh
        # auto-detects the build host arch and passes relay:cpu_arch so ARM64
        # hosts deploy ARM64 tasks with no operator action. X86_64 is the default.
        _cpu_arch = (
            ecs.CpuArchitecture.ARM64
            if resolve_cpu_architecture(self.node.try_get_context("relay:cpu_arch"))
            == "ARM64"
            else ecs.CpuArchitecture.X86_64
        )
        task_def = ecs.FargateTaskDefinition(
            self, "RelayHubTaskDef", family="relay-hub",
            cpu=1024, memory_limit_mib=2048,
            execution_role=execution_role, task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=_cpu_arch,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        # UI auth mode. An explicit relay:auth_mode always wins. When it is unset
        # the default is environment-aware: a non-prod team board (dev/test/etc.)
        # comes up write-capable (`dev`) so operators can ack/resolve and edit
        # contacts without first wiring an IdP, while a prod board stays locked
        # read-only (`none`) — never accidentally write-open. Set relay:auth_mode
        # explicitly (alb|dev|none) to override either default.
        node_environment: str = (
            self.node.try_get_context("relay:environment") or "unrouted"
        )
        auth_mode: str = resolve_auth_mode(
            self.node.try_get_context("relay:auth_mode"), node_environment
        )
        access_control: bool = (
            str(self.node.try_get_context("relay:access_control") or "false").lower()
            == "true"
        )
        allowed_users: str = (
            self.node.try_get_context("relay:auth_allowed_users") or ""
        ).strip()
        config_source: str = self.node.try_get_context("relay:config_source") or ""
        team_tz: str = self.node.try_get_context("relay:tz") or "UTC"
        ai_model_id: str = self.node.try_get_context("relay:ai_model_id") or ""
        team_name: str = self.node.try_get_context("relay:team_name") or "unnamed-team"

        container_env: dict[str, str] = {
            "RELAY_ROLE": "hub",
            "RELAY_RUNTIME": "fargate",
            "RELAY_HUB_SCOPE": resolved_scope,
            "RELAY_FLEET_TABLE_NAME": fleet_table.table_name,
            "RELAY_TABLE_NAME": fleet_table.table_name,
            "RELAY_SQS_QUEUE_URL": ingest_queue.queue_url,
            "AWS_REGION": self.region,
            "AWS_DEFAULT_REGION": self.region,
            "RELAY_CENTRAL_PAGING_TOPIC_ARN": central_paging_topic.topic_arn,
            "RELAY_SNS_TOPIC_ARN": paging_topic_arn,
            "RELAY_PAGING_TOPIC_ARN": paging_topic_arn,
            "RELAY_SERVICENOW_INSTANCE": servicenow_instance,
            "RELAY_AUTH_MODE": auth_mode,
            "RELAY_AUTH_ACCESS_CONTROL": "true" if access_control else "false",
            "RELAY_TZ": team_tz,
            "LOG_LEVEL": log_level,
            "RELAY_RESOLVE_ALARM_TAGS": "true" if resolve_alarm_tags else "false",
            # Signals /health/ready to run the sns_direct_sms probe only when the
            # operator opted into direct SMS (and the matching IAM grant exists).
            "RELAY_ENABLE_DIRECT_SMS": "true" if enable_direct_sms else "false",
            # Node self-identity (the container now owns detection in-process, so
            # it carries the identity that was on the Node Lambda — plan §11 Step 4).
            "RELAY_TEAM_NAME": team_name,
            "RELAY_NODE_APP_NAME": self.node.try_get_context("relay:app_name") or team_name,
            "RELAY_NODE_DEPLOYMENT_ID": self.node.try_get_context("relay:deployment_id") or team_name,
            "RELAY_NODE_ENVIRONMENT": node_environment,
            "RELAY_NODE_SERVICE_PATH": self.node.try_get_context("relay:service_path") or "",
            "RELAY_NODE_ORG_PATH": self.node.try_get_context("relay:org_path") or "",
        }
        if allowed_users:
            container_env["RELAY_AUTH_ALLOWED_USERS"] = allowed_users

        if ai_enabled:
            container_env["RELAY_AI_ENABLED"] = "true"
            if ai_model_id:
                container_env["RELAY_AI_MODEL_ID"] = ai_model_id
            if ai_provider:
                container_env["RELAY_AI_PROVIDER"] = ai_provider
            if ai_base_url:
                container_env["RELAY_AI_BASE_URL"] = ai_base_url
            if ai_api_key_secret_name:
                container_env["RELAY_AI_API_KEY_SECRET"] = ai_api_key_secret_name
        if auth_mode == "dev":
            container_env["RELAY_DEV_USER"] = (
                self.node.try_get_context("relay:dev_user") or "operator"
            )
        # Default to the bundled local config when no source is specified: the
        # image always ships config/*.yaml at /app/config, and without this the
        # container starts with RELAY_CONFIG_SOURCE unset → no config loaded → the
        # routing/ignore seeds (incl. the TargetTracking- ignore rule that keeps
        # autoscaling alarms from paging) never reach DynamoDB. An explicit
        # relay:config_source (e.g. "gitlab") still wins.
        if config_source in ("local", ""):
            container_env["RELAY_CONFIG_SOURCE"] = "local"
            container_env["RELAY_CONFIG_DIR"] = "/app/config"
        if resolved_scope == "local-federated" and central_hub_bus_arn:
            container_env["RELAY_CENTRAL_HUB_BUS_ARN"] = central_hub_bus_arn
            if not byor_mode:
                task_role.add_to_principal_policy(
                    iam.PolicyStatement(
                        sid="RelayCentralHubForwardEvents",
                        effect=iam.Effect.ALLOW,
                        actions=["events:PutEvents"],
                        resources=[central_hub_bus_arn],
                    )
                )

        container_secrets: dict[str, ecs.Secret] = {}

        task_def.add_container(
            "RelayHubContainer",
            container_name="relay-hub",
            image=ecs.ContainerImage.from_registry(hub_image_uri),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="relay-hub",
                log_group=logs.LogGroup(
                    self,
                    "RelayHubLogs",
                    log_group_name="/relay/hub",
                    retention=logs.RetentionDays.THREE_MONTHS,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            ),
            environment=container_env,
            secrets=container_secrets,
            port_mappings=[
                ecs.PortMapping(container_port=8080, protocol=ecs.Protocol.TCP)
            ],
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
        )

        # ------------------------------------------------------------------
        # Certificate resolution. HTTPS is the default whenever a cert can be
        # obtained. Supply relay:phz_id + relay:phz_name for auto-minting, or
        # relay:certificate_arn to bring your own. Without either, the ALB falls
        # back to HTTP:80 and a synth warning is emitted.
        # ------------------------------------------------------------------
        cert, zone, fqdn = resolve_certificate(
            self,
            certificate_arn=certificate_arn,
            phz_id=phz_id,
            phz_name=phz_name,
            alb_subdomain=alb_subdomain,
        )

        # ------------------------------------------------------------------
        # ALB + Fargate service. Always-on (floor of `min_capacity`, default 1);
        # no scale-to-zero. Zero-downtime deploys: min_healthy_percent=100 keeps
        # the current task(s) serving until the replacement passes health checks,
        # max_healthy_percent=200 gives room to run the new one alongside the old.
        # Circuit breaker WITH rollback so a bad image rolls back, never wedges.
        # ------------------------------------------------------------------
        _alb_kwargs: dict[str, Any] = dict(
            cluster=cluster,
            task_definition=task_def,
            desired_count=min_capacity,
            min_healthy_percent=100,
            max_healthy_percent=200,
            public_load_balancer=not internal_alb,
            assign_public_ip=False,
            health_check_grace_period=Duration.seconds(120),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )
        if cert is not None:
            _alb_kwargs["protocol"] = elbv2.ApplicationProtocol.HTTPS
            _alb_kwargs["certificate"] = cert
            _alb_kwargs["redirect_http"] = True
            _alb_kwargs["listener_port"] = 443
            if zone is not None and fqdn is not None:
                _alb_kwargs["domain_name"] = fqdn
                _alb_kwargs["domain_zone"] = zone
        else:
            _alb_kwargs["listener_port"] = 80
            cdk.Annotations.of(self).add_warning(
                "HTTPS is the default but no certificate or private hosted zone "
                "was supplied — the ALB is using HTTP:80. To enable HTTPS, supply "
                "relay:phz_id + relay:phz_name (auto-minted ACM cert) or "
                "relay:certificate_arn (bring-your-own cert)."
            )

        alb_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "RelayHubService",
            service_name="relay-hub",
            **_alb_kwargs,
        )
        alb_service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200",
            interval=Duration.seconds(10),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )
        scheme = "https" if cert is not None else "http"
        _dashboard_host = fqdn or alb_service.load_balancer.load_balancer_dns_name
        default_container = alb_service.task_definition.default_container
        assert default_container is not None  # set above via add_container
        default_container.add_environment(
            "RELAY_DASHBOARD_URL",
            f"{scheme}://{_dashboard_host}/",
        )

        # CPU auto-scaling — floor of `min_capacity` (default 1), no scale-to-zero.
        scaling = alb_service.service.auto_scale_task_count(
            min_capacity=min_capacity, max_capacity=max_capacity,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(30),
        )

        # ------------------------------------------------------------------
        # Outputs.
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self, "DashboardUrl",
            value=f"{scheme}://{_dashboard_host}/",
            description="Relay dashboard URL",
        )
        cdk.CfnOutput(
            self, "IngestQueueUrl", value=ingest_queue.queue_url,
            description="Relay ingest SQS queue URL",
        )

        # BYOR inline-policy outputs (paste onto pre-provisioned roles).
        if byor_mode:
            self._emit_byor_outputs(
                team_name=team_name,
                role=role,
                task_role_arn=ecs_task_role_arn,
                region=self.node.try_get_context("relay:aws_region") or self.region,
                resolved_scope=resolved_scope,
                central_hub_bus_arn=central_hub_bus_arn,
                enable_direct_sms=enable_direct_sms,
                ai_uses_bedrock=ai_uses_bedrock,
                ai_api_key_secret_name=ai_api_key_secret_name,
                resolve_alarm_tags=resolve_alarm_tags,
            )

    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------

    def _import_topic(self, construct_id: str, topic_arn: str) -> sns.ITopic:
        return sns.Topic.from_topic_arn(self, construct_id, topic_arn)

    def _emit_byor_outputs(
        self,
        *,
        team_name: str,
        role: str,
        task_role_arn: str,
        region: str,
        resolved_scope: str,
        central_hub_bus_arn: str,
        enable_direct_sms: bool,
        ai_uses_bedrock: bool,
        ai_api_key_secret_name: str,
        resolve_alarm_tags: bool,
    ) -> None:
        """Emit inline-policy + trust JSON for the two pre-provisioned ECS roles.

        The Resource ARNs are built as LITERAL strings, NOT from construct
        ``.arn`` token attributes. Two things would otherwise leave the output
        unpasteable:

        - A cross-stack construct ARN (the imported data-stack table, the
          same-stack ingest queue) serializes as an ``Fn::ImportValue`` /
          ``Fn::GetAtt`` intrinsic.
        - ``self.partition`` / ``self.account`` / ``self.region`` are themselves
          CDK *tokens* (``{"Ref": "AWS::Partition"}`` …) in an env-agnostic
          synth — they only resolve at deploy time, so ``json.dumps`` on them
          also yields intrinsics.

        Both would land in the CfnOutput as CloudFormation JSON an administrator
        cannot paste into the IAM console. Instead we derive the partition +
        account from the BYOR task-role ARN (always a concrete literal the
        operator supplied) and take the region from context, so the emitted
        policy is a plain, fully-resolved JSON string. The names below mirror
        data_stack.py (table/topics) and the ingest queue name in this stack.
        """
        # Partition + account come from the operator-supplied task-role ARN,
        # which is always a literal: arn:<partition>:iam::<account>:role/<name>.
        _arn_parts = task_role_arn.split(":")
        partition = _arn_parts[1] if len(_arn_parts) > 4 else "aws"
        account = _arn_parts[4] if len(_arn_parts) > 4 else self.account

        # Deterministic resource names (mirror data_stack.py + this stack).
        table_name = "relay-hub-fleet" if role == "federated-hub" else f"relay-{team_name}"
        paging_topic_name = f"relay-{team_name}-paging"
        central_paging_topic_name = (
            "relay-hub-central-paging"
            if role == "federated-hub"
            else f"relay-{team_name}-central-paging"
        )
        table_arn = (
            f"arn:{partition}:dynamodb:{region}:{account}:table/{table_name}"
        )
        paging_topic_arn_literal = (
            f"arn:{partition}:sns:{region}:{account}:{paging_topic_name}"
        )
        central_paging_topic_arn_literal = (
            f"arn:{partition}:sns:{region}:{account}:{central_paging_topic_name}"
        )
        ingest_queue_arn = (
            f"arn:{partition}:sqs:{region}:{account}:relay-hub-ingest"
        )
        log_group_arn = (
            f"arn:{partition}:logs:{region}:{account}:log-group:/relay/hub:*"
        )
        task_statements: list[dict[str, Any]] = [
            {
                "Sid": "RelayHubFleetTable",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                    "dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:Scan",
                    "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem",
                    # DescribeTable backs the /health/ready dynamodb probe.
                    "dynamodb:DescribeTable",
                ],
                "Resource": [table_arn, f"{table_arn}/index/*"],
            },
            {
                "Sid": "RelayHubPaging",
                "Effect": "Allow",
                # GetTopicAttributes backs the /health/ready sns_paging_topic probe.
                "Action": ["sns:Publish", "sns:GetTopicAttributes"],
                "Resource": [central_paging_topic_arn_literal, paging_topic_arn_literal],
            },
            {
                # Contacts screen subscription state + Subscribe button (#78): the
                # Hub lists the paging topic's email subscriptions and can add an
                # email endpoint. Team paging topic only (not the central topic,
                # which has no human subscribers).
                "Sid": "RelayHubPagingSubscriptions",
                "Effect": "Allow",
                "Action": ["sns:ListSubscriptionsByTopic", "sns:Subscribe"],
                "Resource": [paging_topic_arn_literal],
            },
            {
                "Sid": "RelayHubIngestConsume",
                "Effect": "Allow",
                "Action": [
                    "sqs:ReceiveMessage", "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
                ],
                "Resource": ingest_queue_arn,
            },
        ]
        if resolve_alarm_tags:
            task_statements.append({
                "Sid": "RelayAlarmTagResolution",
                "Effect": "Allow",
                "Action": list(_ALARM_TAG_ACTIONS),
                "Resource": "*",
            })
        if resolved_scope == "local-federated" and central_hub_bus_arn:
            task_statements.append({
                "Sid": "RelayHubForwardEvents",
                "Effect": "Allow",
                "Action": ["events:PutEvents"],
                "Resource": central_hub_bus_arn,
            })
        if enable_direct_sms:
            task_statements.append({
                "Sid": "RelayHubDirectSms",
                "Effect": "Allow",
                # CheckIfPhoneNumberIsOptedOut backs the /health/ready
                # sns_direct_sms probe (only run when direct SMS is enabled).
                "Action": ["sns:Publish", "sns:CheckIfPhoneNumberIsOptedOut"],
                # See the non-BYOR grant above: direct-to-phone Publish carries no
                # sns:Protocol context key, so scope by region, not protocol.
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": region}},
            })
        if ai_uses_bedrock:
            task_statements.append({
                "Sid": "RelayHubBedrockInvoke",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
                "Resource": [
                    f"arn:{partition}:bedrock:*:{account}:inference-profile/*",
                    f"arn:{partition}:bedrock:*::foundation-model/*",
                ],
            })
        if ai_api_key_secret_name:
            task_statements.append({
                "Sid": "RelayHubAIKeySecret",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": (
                    f"arn:{partition}:secretsmanager:{region}:"
                    f"{account}:secret:{ai_api_key_secret_name}*"
                ),
            })
        task_policy = {"Version": "2012-10-17", "Statement": task_statements}

        exec_statements: list[dict[str, Any]] = [
            {
                "Sid": "RelayHubEcr",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                ],
                "Resource": "*",
            },
            {
                "Sid": "RelayHubLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": log_group_arn,
            },
        ]
        exec_policy = {"Version": "2012-10-17", "Statement": exec_statements}
        ecs_trust = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }],
        }
        cdk.CfnOutput(self, "ByorTaskRoleInlinePolicy", value=json.dumps(task_policy),
                      description="BYOR: paste this inline policy onto your ECS TASK role")
        cdk.CfnOutput(self, "ByorExecutionRoleInlinePolicy", value=json.dumps(exec_policy),
                      description="BYOR: paste this inline policy onto your ECS EXECUTION role")
        cdk.CfnOutput(self, "ByorEcsRoleTrust", value=json.dumps(ecs_trust),
                      description="BYOR: trust relationship both ECS roles must have")
