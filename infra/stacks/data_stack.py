"""
RelayDataStack — the durable data plane (collapse Step 4)
=========================================================
The first independent deploy target: DynamoDB table (+ GSI, stream), the team
paging SNS topic, and (for a federated hub) the central paging topic. **No
compute.** Stable, rarely changes, RETAIN on delete.

Deploy this once and it's done; the compute stack redeploys on every image
change without ever touching this stack (``relay-deploy.sh data`` vs.
``relay-deploy.sh compute``). This is the collapsed-single-container redesign's
"multiple starting points" (plan §5): one table-schema definition shared by both
topologies, no Node↔Hub schema duplication.

Context keys consumed:
  relay:role        — "team" (default) | "federated-hub"; selects the table name
  relay:team_name   — team identifier (team role); table = relay-{team}
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_sns as sns,
)
from constructs import Construct


class RelayDataStack(Stack):
    """DynamoDB table + paging SNS topic(s). The durable, compute-free data plane.

    Public attributes (consumed by RelayComputeStack via cross-stack refs):
      - ``table``               the single DynamoDB table
      - ``table_name`` / ``table_arn``
      - ``paging_topic``        the team paging SNS topic
      - ``central_paging_topic`` central team paging topic (federated hub only)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        role: str = "team",
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        team_name: str = self.node.try_get_context("relay:team_name") or "unnamed-team"
        # One table name per topology. Team: relay-{team} (the container runs both
        # detection + the dashboard against it). Federated hub: relay-hub-fleet.
        table_name = (
            "relay-hub-fleet" if role == "federated-hub" else f"relay-{team_name}"
        )

        # ------------------------------------------------------------------
        # DynamoDB single table — contacts (PII) + incident/escalation state +
        # fleet tiles + on-call schedule + escalation deadlines (collapse Step 2).
        # Entity types differentiated by PK prefix:
        #   CONTACT#<id>         — contact PII
        #   INCIDENT#<id>        — incident record + embedded timeline
        #   ESC#<id>/STATE       — escalation FSM context
        #   ESC#<id>/DEADLINE    — escalation timeout deadline (swept by the container)
        #   SCHED#<week>         — generated on-call schedule
        #   FLEET#<env>#<dep>    — fleet big-board tile
        # ------------------------------------------------------------------
        self.table = dynamodb.Table(
            self,
            "RelayTable",
            table_name=table_name,
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,  # keep data on stack delete
            time_to_live_attribute="ttl",  # resolved incidents expire automatically
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,  # feeds dashboard live push
        )
        # GSI — query incidents by status (OPEN / ACKED / RESOLVED) for the dashboard.
        self.table.add_global_secondary_index(
            index_name="incident-status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="opened_at", type=dynamodb.AttributeType.STRING
            ),
        )
        self.table_name = self.table.table_name
        self.table_arn = self.table.table_arn

        # ------------------------------------------------------------------
        # SNS paging topics. The container publishes to these after resolving
        # on-call. Per-contact subscriptions are managed at runtime by Relay
        # (not as CDK resources), so adding a contact needs no deploy.
        # ------------------------------------------------------------------
        self.paging_topic = sns.Topic(
            self,
            "RelayPagingTopic",
            topic_name=f"relay-{team_name}-paging",
            display_name=f"Relay on-call paging — {team_name}",
        )
        # The central team-paging topic exists for the federated aggregator's own
        # on-call; a team-topology container still reads RELAY_CENTRAL_PAGING_TOPIC_ARN
        # so we always provide one (named per team to avoid collisions).
        self.central_paging_topic = sns.Topic(
            self,
            "RelayCentralPagingTopic",
            topic_name=(
                "relay-hub-central-paging"
                if role == "federated-hub"
                else f"relay-{team_name}-central-paging"
            ),
            display_name="Relay Hub — central team paging",
        )

        # ------------------------------------------------------------------
        # Outputs — the compute stack imports the table + topics by name/ARN.
        # ------------------------------------------------------------------
        cdk.CfnOutput(self, "DataTableName", value=self.table_name,
                      description="Relay data-plane DynamoDB table name")
        cdk.CfnOutput(self, "DataTableArn", value=self.table_arn,
                      description="Relay data-plane DynamoDB table ARN")
        cdk.CfnOutput(self, "PagingTopicArn", value=self.paging_topic.topic_arn,
                      description="Relay team paging SNS topic ARN")
        cdk.CfnOutput(self, "CentralPagingTopicArn",
                      value=self.central_paging_topic.topic_arn,
                      description="Relay central team paging SNS topic ARN")
