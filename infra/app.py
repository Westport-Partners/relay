#!/usr/bin/env python3
"""
Relay CDK App — entrypoint (collapsed single-container topology)
================================================================
Composes independent, separately-deployable stacks (collapsed-single-container
plan §5). No more StandaloneStack fanning one deploy across two coupled stacks —
each stack deploys on its own, and a compute redeploy never touches the data
plane.

Stacks:
  RelayDataStack       — DynamoDB table (+ GSI, stream) + paging SNS topics. The
                         durable, compute-free data plane. Deploy once; RETAIN.
  RelayComputeStack    — VPC/ECS/Fargate/ALB + SQS ingress + CloudWatch alarm rule
                         + IAM roles. The always-on container. Redeploys on every
                         image change. Imports the data stack by name/ARN.
  RelayFederationStack — (federated-hub only) the relay-hub EventBridge bus +
                         resource policy + ingest rule for org-wide forwarding.

Relay has exactly TWO topologies (unchanged):
  team           (default) — one always-on container running detection in-process
                             + the dashboard, against one DynamoDB table in ONE
                             team account. Deploys RelayDataStack + RelayComputeStack.
                             Optional: relay:hub_scope=local-federated +
                             relay:central_hub_bus_arn to forward up to a federated hub.
  federated-hub            — the always-on upstream aggregator (org-wide NOC
                             big-board). Deploys all three stacks: Data + Compute
                             + Federation (the bus teams forward up to).

Deprecated role aliases (still accepted): standalone → team, hub → federated-hub,
node → team.

Usage
-----
    # Team deploy (default): data + compute, data first.
    cdk deploy RelayDataStack RelayComputeStack \\
        -c relay:role=team -c relay:team_name=<team> \\
        -c relay:hub_image_uri=<ecr-image-uri>

    # Data plane only (first starting point — deploy once):
    cdk deploy RelayDataStack -c relay:team_name=<team>

    # Compute only (the common inner loop — redeploys on image change):
    cdk deploy RelayComputeStack -c relay:team_name=<team> \\
        -c relay:hub_image_uri=<ecr-image-uri>

    # Federated-hub deploy (all three stacks):
    cdk deploy RelayDataStack RelayComputeStack RelayFederationStack \\
        -c relay:role=federated-hub -c relay:org_id=<org-id> \\
        -c relay:hub_image_uri=<ecr-image-uri>
"""

import aws_cdk as cdk
from stacks.compute_stack import RelayComputeStack
from stacks.data_stack import RelayDataStack
from stacks.federation_stack import RelayFederationStack

app = cdk.App()

# Role from context; default "team". Old names accepted as deprecated aliases.
_raw_role = app.node.try_get_context("relay:role") or "team"
_ALIASES = {"standalone": "team", "node": "team", "hub": "federated-hub"}
role = _ALIASES.get(_raw_role, _raw_role)

env = cdk.Environment(
    account=app.node.try_get_context("relay:aws_account") or None,
    region=app.node.try_get_context("relay:aws_region") or None,
)

# 1. Data plane — always synthesized. Deploy first; the compute stack imports it.
data = RelayDataStack(
    app,
    "RelayDataStack",
    role=role,
    env=env,
    description="Relay data plane — DynamoDB table + paging SNS topics [RETAIN]",
)

# 2. Compute — the always-on container. Imports the data stack by name/ARN.
compute = RelayComputeStack(
    app,
    "RelayComputeStack",
    role=role,
    data_table_name=data.table_name,
    data_table_arn=data.table_arn,
    paging_topic_arn=data.paging_topic.topic_arn,
    central_paging_topic_arn=data.central_paging_topic.topic_arn,
    env=env,
    description="Relay compute plane — always-on Fargate container + ALB + SQS ingress",
)
compute.add_dependency(data)

# 3. Federation — only for the federated aggregator (the bus teams forward up to).
if role == "federated-hub":
    RelayFederationStack(
        app,
        "RelayFederationStack",
        env=env,
        description="Relay federated-hub EventBridge bus + org policy + ingest rule",
    )

app.synth()
