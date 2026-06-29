"""
RelayFederationStack — the federated-hub bus (collapse Step 4, optional)
========================================================================
The third, optional deploy target — synthesized only for the federated
aggregator (``relay:role=federated-hub``). It owns the ``relay-hub`` EventBridge
bus that team containers forward selected SEV1/2 incidents up to, plus the rule
that routes those events into the aggregator's ingest queue.

A *team* container does NOT need this stack: its central leg is an in-process
call (collapse §2), and its CloudWatch alarm rule lives in RelayComputeStack.
The bus only exists where teams forward *up* to a federated NOC.

Cycle-free by construction: the bus policy is a standalone ``CfnEventBusPolicy``
referencing ``bus.event_bus_arn`` directly (no L1 ``.policy`` escape hatch, no
constructed-ARN string — those workarounds existed only because the old monolith
set the policy inside the same stack that consumed the bus).

Context keys consumed:
  relay:org_id              — org id for the org-wide PutEvents policy (central)
  relay:federation_ingest_queue_arn — optional: route bus events to this SQS queue
                              (the aggregator's RelayComputeStack ingest queue)
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from constructs import Construct

_HUB_BUS_NAME = "relay-hub"


class RelayFederationStack(Stack):
    """The federated-hub EventBridge bus + resource policy + ingest rule.

    Public attributes:
      - ``hub_bus``       the EventBridge bus
      - ``hub_bus_arn``   its ARN (hand to team deploys as relay:central_hub_bus_arn)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ingest_queue_arn: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        org_id: str = self.node.try_get_context("relay:org_id") or ""

        self.hub_bus = events.EventBus(
            self, "RelayHubBus", event_bus_name=_HUB_BUS_NAME
        )
        self.hub_bus_arn = self.hub_bus.event_bus_arn

        # Resource policy as a standalone CfnEventBusPolicy referencing the bus
        # ARN directly — no circular dependency, so no L1 escape hatch needed.
        if org_id:
            # Org-wide PutEvents — covers all current + future org accounts.
            events.CfnEventBusPolicy(
                self,
                "RelayHubBusOrgPolicy",
                event_bus_name=self.hub_bus.event_bus_name,
                statement_id="AllowOrgWidePutEvents",
                statement={
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "events:PutEvents",
                    "Resource": self.hub_bus.event_bus_arn,
                    "Condition": {"StringEquals": {"aws:PrincipalOrgID": org_id}},
                },
            )
        else:
            # Same-account-only ingress when no org id is configured.
            events.CfnEventBusPolicy(
                self,
                "RelayHubBusSameAccountPolicy",
                event_bus_name=self.hub_bus.event_bus_name,
                statement_id="AllowSameAccountPutEvents",
                statement={
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "events:PutEvents",
                    "Resource": self.hub_bus.event_bus_arn,
                    "Condition": {"StringEquals": {"aws:PrincipalAccount": self.account}},
                },
            )

        # Route all Relay events on the bus (source prefix "relay.") to the
        # aggregator's ingest queue, if one was supplied.
        resolved_queue_arn = ingest_queue_arn or (
            self.node.try_get_context("relay:federation_ingest_queue_arn") or ""
        )
        if resolved_queue_arn:
            ingest_queue = sqs.Queue.from_queue_arn(
                self, "RelayFederationIngestQueue", resolved_queue_arn
            )
            events.Rule(
                self,
                "RelayHubIngestRule",
                rule_name="relay-hub-ingest",
                event_bus=self.hub_bus,
                description="Route all Relay events on the hub bus to the ingest SQS queue.",
                event_pattern=events.EventPattern(source=events.Match.prefix("relay.")),
                targets=[targets.SqsQueue(ingest_queue)],
            )

        cdk.CfnOutput(
            self, "EventBusArn", value=self.hub_bus.event_bus_arn,
            description=(
                "Relay federated-hub EventBridge bus ARN — supply to team deploys "
                "as relay:central_hub_bus_arn"
            ),
        )
