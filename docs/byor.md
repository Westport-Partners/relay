# Relay — BYOR / BYOV (Locked-Down Accounts)

Many regulated and government accounts prohibit creating IAM roles and VPCs. Teams get a
fixed set of pre-provisioned roles they may only modify via inline policies and trust edits,
and one or more pre-existing VPCs they must reuse.

By default Relay creates all IAM and network resources. BYOR (Bring-Your-Own-Role) and BYOV
(Bring-Your-Own-VPC) let you supply existing ARNs instead; the compute stack imports them
and creates nothing.

> **IAM surface.** Relay runs as a single always-on container. The IAM surface is
> **exactly two roles**: one ECS task role and one ECS execution role. There is no
> Lambda execution role, no EventBridge Scheduler invoke role, and no `PassRole` grant in the
> task definition.

---

## BYOR — Bring-Your-Own-Role

Pass two CDK context keys and the compute stack imports the roles instead of creating them:

| CDK context key | What it is |
|---|---|
| `relay:ecs_task_role_arn` | Your pre-provisioned ECS task role |
| `relay:ecs_execution_role_arn` | Your pre-provisioned ECS task execution role |

BYOR activates when **both** are supplied. The stack creates zero IAM roles.

### What the stack emits in BYOR mode

Because the stack cannot modify the roles itself, it emits the exact policy JSON you need
as CloudFormation outputs. An account administrator pastes these onto the roles — the one
IAM action they are permitted.

| Output key | What to do with it |
|---|---|
| `ByorTaskRoleInlinePolicy` | Add as inline policy on the task role |
| `ByorExecutionRoleInlinePolicy` | Add as inline policy on the execution role |
| `ByorEcsRoleTrust` | Update the trust policy on **both** roles |

The stack output is the source of truth for the exact permissions. The categories of what
the policies grant at runtime:

**Task role** — DynamoDB item-level operations on the Relay table and its indexes;
`sns:Publish` on the paging topic; `events:PutEvents` on the federation bus (federated-hub
topology only); `secretsmanager:GetSecretValue` on the GitLab and/or ServiceNow secrets
(when those integrations are enabled); `secretsmanager:GetSecretValue` on the AI API-key
secret (when AI is enabled); alarm and resource tag-read APIs
(`cloudwatch:ListTagsForResource`, `lambda:ListTags`, `sqs:ListQueueTags`,
`ecs:ListTagsForResource`) — these do not support resource-level scoping so they are on
`*`; without them the container degrades to alarm-name matching for app resolution.

**Execution role** — Standard ECR image pull (`ecr:GetAuthorizationToken`,
`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`) and CloudWatch Logs writes
(`logs:CreateLogStream`, `logs:PutLogEvents`).

**Trust policy** — Both roles need `ecs-tasks.amazonaws.com` as a trusted principal. The
`ByorEcsRoleTrust` output contains the exact trust document.

---

## BYOV — Bring-Your-Own-VPC

Pass one context key and the compute stack calls `from_lookup` on the existing VPC instead
of creating one (no VPC, subnets, NAT gateways, or Internet Gateway created):

| CDK context key | What it is |
|---|---|
| `relay:vpc_id` | ID of the existing VPC to import (e.g. `vpc-0abc1234`) |

Requirements for the imported VPC:
- **Public subnets** — the ALB is placed here.
- **Private subnets** — Fargate tasks run here (NAT or VPC endpoints needed for ECR, DynamoDB, SQS, SNS, CloudWatch Logs, Secrets Manager).

`from_lookup` queries the live account at synth time and caches the result to
`cdk.context.json`. **Commit or carry `cdk.context.json`** so CI synths are reproducible
without live AWS access.

BYOV is independent of BYOR — you can use either, both, or neither.

---

## BYOR deploy workflow

### 1. Identify your roles and VPC

Obtain the ARNs of your two pre-provisioned roles and, if needed, your VPC ID from the
account administrator or your internal service catalog.

### 2. Synth to generate the policy outputs

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-synth.sh -- \
  -c relay:ecs_task_role_arn=arn:aws:iam::<account>:role/<task-role> \
  -c relay:ecs_execution_role_arn=arn:aws:iam::<account>:role/<exec-role> \
  -c relay:vpc_id=vpc-<id>
```

Arguments after `--` are forwarded verbatim to `cdk synth`. The synth writes templates to
`cdk.out/` — no AWS writes occur. Stack outputs appear in the synth output; use `cdk diff`
if you want to review before committing.

### 3. An admin applies the policies

Have an account administrator:

1. Open **IAM → Roles → (task role) → Add permissions → Create inline policy**, choose
   JSON, paste `ByorTaskRoleInlinePolicy`, and save.
2. Do the same for `ByorExecutionRoleInlinePolicy` on the execution role.
3. Update the trust policy on **both** roles to include the `ByorEcsRoleTrust` document
   (merge it with any existing trust entries; do not replace).

### 4. Deploy with the same context keys

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh -- \
  -c relay:ecs_task_role_arn=arn:aws:iam::<account>:role/<task-role> \
  -c relay:ecs_execution_role_arn=arn:aws:iam::<account>:role/<exec-role> \
  -c relay:vpc_id=vpc-<id>
```

The compute stack imports both roles and the VPC, creates all other resources, and emits
`DashboardUrl` when complete.

### Scoped re-deploys in BYOR mode

Pass the same `-c relay:ecs_*_role_arn` and `-c relay:vpc_id` flags on every deploy.
Use `RELAY_STACK_SELECTOR=compute` for image-only updates (data stack already stable):

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
RELAY_STACK_SELECTOR=compute \
./scripts/relay-deploy.sh -- \
  -c relay:ecs_task_role_arn=<task-role-arn> \
  -c relay:ecs_execution_role_arn=<exec-role-arn> \
  -c relay:vpc_id=<vpc-id>
```

---

## Deploy principal permissions

The role that runs the deploy (human or CI runner) still needs permission to call
CloudFormation and the services each stack provisions. In BYOR mode the stack does not create roles, so `iam:CreateRole` can be
omitted from that policy — but all other permissions remain. Full reference: [infra/RUNNER_IAM.md](../infra/RUNNER_IAM.md).
