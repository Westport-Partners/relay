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

> **Deploy with `scripts/relay-deploy-direct.sh` — not `cdk deploy`.** `cdk deploy`
> passes the CDK bootstrap execution role to CloudFormation via `iam:PassRole`, which
> locked-down accounts deny, so it fails immediately. `relay-deploy-direct.sh` synths
> locally then submits with `aws cloudformation deploy` using your own credentials — no
> bootstrap role is passed, and no CDK bootstrap is required. This is the only supported
> deploy path here; every example below uses it.
>
> **`iam:PassRole` is still required — but scoped, not blanket.** "No bootstrap role is
> passed" does **not** mean "no PassRole at all." Registering the ECS task definition
> requires the **deploy identity** to `iam:PassRole` the task and execution roles to
> `ecs-tasks.amazonaws.com` — this is intrinsic to ECS; no deploy tool avoids it.
> Locked-down accounts allow this by scoping `iam:PassRole` to exactly those two role
> ARNs with an `iam:PassedToService = ecs-tasks.amazonaws.com` condition (see
> [Deploy principal permissions](#deploy-principal-permissions)). If your account denies
> `iam:PassRole` outright with no scoped exception, the ECS/Fargate path is impossible —
> run the released container directly instead ([local-dev.md](local-dev.md)).
>
> To evaluate Relay without deploying ECS at all, `scripts/relay-provision-cli.sh`
> creates just the data plane + alarm ingest with plain AWS CLI calls.

---

## BYOR — Bring-Your-Own-Role

Pass two CDK context keys and the compute stack imports the roles instead of creating them:

| CDK context key | What it is |
|---|---|
| `relay:ecs_task_role_arn` | Your pre-provisioned ECS task role |
| `relay:ecs_execution_role_arn` | Your pre-provisioned ECS task execution role |

BYOR activates when **both** are supplied. The stack creates zero IAM roles.

> **One role for both is fine.** If your organization pre-provisions a single service
> role that covers both ECS task and execution responsibilities, pass the **same ARN**
> for both context keys. The stack imports it twice under separate CDK construct IDs
> (`RelayHubTaskRole` and `RelayHubExecutionRole`) — valid, and it results in one role
> carrying both inline policies. In that case apply **both** emitted inline policies
> (`ByorTaskRoleInlinePolicy` + `ByorExecutionRoleInlinePolicy`) to that single role.

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

> **Direct-to-contact SMS is opt-in.** SMS to a specific phone number (the "Test page"
> button and targeted pages) uses `sns:Publish` against a *phone-number* resource, which
> the base task policy does not grant. The `RelayHubDirectSms` statement is added to
> `ByorTaskRoleInlinePolicy` only when you synth with `-c relay:enable_direct_sms=true`
> (or `RELAY_ENABLE_DIRECT_SMS=true`). It is scoped by `aws:RequestedRegion` — **not** by
> `sns:Protocol`, which is a Subscribe-only condition key absent from a `Publish` request
> and would fail closed. Without this statement, "Test page" returns 200 but delivers
> nothing and the logs show an `sns:Publish` `AuthorizationError`. IAM edits apply on the
> next task launch, so `force-new-deployment` after adding it.

> **"Test page" pages the team topic.** The test page (and real escalation pages) publish
> to the **team** paging topic — the one operators subscribe to — resolved from
> `RELAY_SNS_TOPIC_ARN` (falling back to `RELAY_PAGING_TOPIC_ARN`, then the central
> federation topic only as a last resort). If a test page reports `{"ok": true}` but
> nobody receives it, confirm there are subscriptions on the team topic, not just that the
> publish succeeded.

**Execution role** — Standard ECR image pull (`ecr:GetAuthorizationToken`,
`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`) and CloudWatch Logs writes
(`logs:CreateLogStream`, `logs:PutLogEvents`).

**Trust policy** — Both roles need `ecs-tasks.amazonaws.com` as a trusted principal. The
`ByorEcsRoleTrust` output contains the exact trust document.

### Example policy documents (for pre-deploy security review)

The documents below are the literal output of a representative synth (placeholder account
`123456789012`, placeholder team `example-team`, region `us-east-1`, no BYOV) — they let a
security team review the exact permission shape **before** any deploy is attempted, breaking
the chicken-and-egg problem this section exists to solve: you can't get the real policy
without deploying, and you can't get pre-approval to deploy without the policy.

Your real ARNs will carry your account id, team name, and region instead of the placeholders
above — this is the shape, not a promise of the exact resource names for every deployment.
As noted above, some statements (direct-to-contact SMS, AI, federation-forwarding) are
conditional on synth-time flags and won't all appear for every deployment.

Task role inline policy (`ByorTaskRoleInlinePolicy`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RelayHubFleetTable",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:BatchWriteItem",
        "dynamodb:BatchGetItem",
        "dynamodb:DescribeTable"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:123456789012:table/relay-example-team",
        "arn:aws:dynamodb:us-east-1:123456789012:table/relay-example-team/index/*"
      ]
    },
    {
      "Sid": "RelayHubPaging",
      "Effect": "Allow",
      "Action": [
        "sns:Publish",
        "sns:GetTopicAttributes"
      ],
      "Resource": [
        "arn:aws:sns:us-east-1:123456789012:relay-example-team-central-paging",
        "arn:aws:sns:us-east-1:123456789012:relay-example-team-paging"
      ]
    },
    {
      "Sid": "RelayHubPagingSubscriptions",
      "Effect": "Allow",
      "Action": [
        "sns:ListSubscriptionsByTopic",
        "sns:Subscribe"
      ],
      "Resource": [
        "arn:aws:sns:us-east-1:123456789012:relay-example-team-paging"
      ]
    },
    {
      "Sid": "RelayHubIngestConsume",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl"
      ],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:relay-hub-ingest"
    },
    {
      "Sid": "RelayAlarmTagResolution",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:ListTagsForResource",
        "lambda:ListTags",
        "sqs:ListQueueTags",
        "ecs:ListTagsForResource",
        "ec2:DescribeTags"
      ],
      "Resource": "*"
    }
  ]
}
```

Execution role inline policy (`ByorExecutionRoleInlinePolicy`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RelayHubEcr",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RelayHubLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:123456789012:log-group:/relay/hub:*"
    }
  ]
}
```

ECS trust policy (`ByorEcsRoleTrust`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "123456789012"
        }
      }
    }
  ]
}
```

These same three documents are also published as a release asset
(`relay-byor-inline-policies-<version>.json`) alongside the CFN templates on each tagged
release, for teams that want a stable versioned artifact for change-management purposes.

---

## BYOV — Bring-Your-Own-VPC

Pass one context key and the compute stack calls `from_lookup` on the existing VPC instead
of creating one (no VPC, subnets, NAT gateways, or Internet Gateway created):

| CDK context key | What it is |
|---|---|
| `relay:vpc_id` | ID of the existing VPC to import (e.g. `vpc-0abc1234`) |

Requirements for the imported VPC:
- **Public subnets** — the ALB is placed here.
- **Private subnets** — Fargate tasks run here. They need outbound reachability
  to the AWS APIs below, via **either** a NAT gateway **or** the specific VPC
  endpoints listed. In a locked-down account with no NAT, request these
  endpoints from your network team (substitute your region for `<region>`):

  | Endpoint | Type | Used for |
  |---|---|---|
  | `com.amazonaws.<region>.ecr.api` | Interface | Pull the container image (ECR auth) |
  | `com.amazonaws.<region>.ecr.dkr` | Interface | Pull the container image (layers) |
  | `com.amazonaws.<region>.s3` | Gateway | ECR layer blobs live in S3 |
  | `com.amazonaws.<region>.dynamodb` | Gateway | Fleet/incidents table |
  | `com.amazonaws.<region>.sqs` | Interface | Ingest queue |
  | `com.amazonaws.<region>.sns` | Interface | Paging topics |
  | `com.amazonaws.<region>.logs` | Interface | CloudWatch Logs |
  | `com.amazonaws.<region>.secretsmanager` | Interface | AI/integration secrets (if used) |

  Interface endpoints must have a security group allowing inbound HTTPS (443)
  from the Fargate task security group. Without NAT and without these
  endpoints, ECS tasks fail to start with no clear error — `relay-preflight.sh`
  emits a WARN when it detects a BYOV VPC with neither NAT nor endpoint
  coverage.

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
./scripts/relay-synth.sh \
  -c relay:ecs_task_role_arn=arn:aws:iam::<account>:role/<task-role> \
  -c relay:ecs_execution_role_arn=arn:aws:iam::<account>:role/<exec-role> \
  -c relay:vpc_id=vpc-<id>
```

Trailing `-c relay:*` flags are forwarded verbatim to `cdk synth`. Do **not** insert a `--`
before them — CDK's arg parser treats everything after `--` as positional and silently
ignores it. The synth writes templates to `cdk.out/` — no AWS writes occur. Inspect the
emitted policy outputs with:

```bash
cat cdk.out/RelayComputeStack.template.json | jq '.Outputs'
```

### 3. An admin applies the policies

Recommended: have the administrator run `scripts/relay-apply-byor-policies.sh
<task-role-name> <exec-role-name>` — it reads the three outputs above straight from
the stack, applies the inline policies, and safely merges the trust statement into
each role (never overwriting existing trust entries). See
[`prompts/deploy-byor.md` Step 3](https://github.com/Westport-Partners/relay/blob/main/prompts/deploy-byor.md#step-3--account-administrator-applies-the-policies)
for the full walkthrough, including the manual IAM-console fallback:

1. Open **IAM → Roles → (task role) → Add permissions → Create inline policy**, choose
   JSON, paste `ByorTaskRoleInlinePolicy`, and save.
2. Do the same for `ByorExecutionRoleInlinePolicy` on the execution role.
3. Update the trust policy on **both** roles to include the `ByorEcsRoleTrust` document
   (merge it with any existing trust entries; do not replace).

### 4. Deploy with the same context keys

Deploy the data plane first (no IAM, no VPC), then the compute stack. Always use
`relay-deploy-direct.sh`:

```bash
# Data stack first
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh

# Then the compute stack with the BYOR/BYOV context
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=compute \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy-direct.sh \
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
./scripts/relay-deploy-direct.sh \
  -c relay:ecs_task_role_arn=<task-role-arn> \
  -c relay:ecs_execution_role_arn=<exec-role-arn> \
  -c relay:vpc_id=<vpc-id>
```

---

## Verifying a BYOR deployment

Once the container is running, hit the deep readiness endpoint to confirm every
wired dependency is reachable and the task role has the required permissions.
**This is the recommended first diagnostic step for any BYOR deployment.**

```bash
# Replace <DASHBOARD_URL> with the value of the DashboardUrl stack output.
curl -s <DASHBOARD_URL>/health/ready | jq .
```

A healthy deployment returns:

```json
{
  "status": "ok",
  "checks": {
    "dynamodb":             {"ok": true, "table": "relay-<team>"},
    "sqs_ingest":           {"ok": true},
    "sns_paging_topic":     {"ok": true},
    "sns_direct_sms":       {"ok": true},
    "config_loaded":        {"ok": true, "source": "local", "path": "/app/config"},
    "ignore_rules_seeded":  {"ok": true, "count": 2},
    "routing_rules_seeded": {"ok": true, "count": 8}
  }
}
```

> **Fresh deploy with no config:** The example above reflects a deployment with a
> populated `config/routing.yaml`. A first-time deploy with no user-supplied config
> returns `"loaded": false` and `"count": 0` for `config_loaded`,
> `ignore_rules_seeded`, and `routing_rules_seeded` — this is correct and expected.
> The deployment is healthy; routing rules are seeded only after configuration is
> applied (see [`prompts/configure.md`](https://github.com/Westport-Partners/relay/blob/main/prompts/configure.md)).

> **SCP-restricted accounts:** In accounts where `sms-voice:DescribeOptedOutNumbers`
> is SCP-denied (Pinpoint SMS Voice v2 blocked), `sns_direct_sms` returns an extra
> `"warn"` field: `{"ok": true, "warn": "opt-out probe blocked by SCP (Pinpoint SMS
> Voice v2 denied) — direct SMS publish path unaffected"}`. This is expected; `ok`
> stays `true` and routing is unaffected.

> **EXPRESS re-deploys:** `RELAY_CFN_MODE=EXPRESS` returns as soon as CloudFormation
> applies resource configuration — ECS is still rolling the new task in the
> background. `relay-deploy-direct.sh` handles this automatically: after the
> CloudFormation call returns, it runs `wait services-stable`, checks the running
> task's image by container name (not index — GuardDuty Runtime Monitoring can inject
> a sidecar that shifts container indices), and forces a fresh ECS deployment if the
> service stabilized on the prior image. This only fires on re-deploys onto a running
> service; first-time creates and STANDARD mode are unaffected. See
> [`prompts/deploy-byor.md`](https://github.com/Westport-Partners/relay/blob/main/prompts/deploy-byor.md)
> for the full Express Mode details.

If `status` is `"degraded"`, the failing check's `error` field names the AWS
error code.  Common BYOR failures:

| Failing check | Error | Fix |
|---|---|---|
| `dynamodb` | `AccessDeniedException` | Add `dynamodb:*` actions on the fleet table to `ByorTaskRoleInlinePolicy` |
| `sqs_ingest` | `AccessDenied` | Add `sqs:ReceiveMessage` / `sqs:DeleteMessage` / `sqs:GetQueueAttributes` on the ingest queue |
| `sns_paging_topic` | `AuthorizationError` | Add `sns:Publish` on the paging topic ARN |
| `sns_direct_sms` | `AuthorizationError` | Add the `RelayHubDirectSms` statement from `ByorTaskRoleInlinePolicy` (requires `-c relay:enable_direct_sms=true` at synth time) |

> **Note on `sns_direct_sms`:** This check runs only when direct SMS is enabled
> (`-c relay:enable_direct_sms=true`); otherwise it is skipped and reports
> `ok: true` with a note. The probe calls `sns:ListPhoneNumbersOptedOut`, which
> stays within SNS. It deliberately avoids `sns:CheckIfPhoneNumberIsOptedOut`,
> which AWS routes internally to Pinpoint SMS Voice
> (`sms-voice:DescribeOptedOutNumbers`) — in accounts with an SCP that denies
> Pinpoint SMS Voice that call would report a false `degraded` even though direct
> SMS (`sns:Publish` to a phone number, a separate path) works at runtime.

---

## Deploy principal permissions

The identity that runs the deploy — the deploy box's instance profile, or a CI runner,
or your CLI credentials — needs permission to call CloudFormation and the services each
stack provisions. In BYOR mode the stack does not create roles, so `iam:CreateRole` can be
omitted; in BYOV mode `ec2:CreateVpc` is not needed either. But the deploy identity still
needs the service actions for the compute stack (ECS, ELBv2, autoscaling, CloudWatch,
security groups, logs, events, SQS, SNS, DynamoDB, ECR, CloudFormation) **and** a scoped
`iam:PassRole`.

An administrator attaches this as an inline policy on your pre-provisioned deploy/instance
role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RelayDirectDeploy",
      "Effect": "Allow",
      "Action": [
        "cloudformation:*", "dynamodb:*", "sns:*", "sqs:*", "logs:*",
        "events:*", "ecr:*", "ecs:*", "elasticloadbalancing:*",
        "application-autoscaling:*", "cloudwatch:*",
        "ec2:Describe*", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateTags", "ec2:DeleteTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RelayPassRuntimeRolesToEcs",
      "Effect": "Allow",
      "Action": ["iam:PassRole"],
      "Resource": [
        "arn:aws:iam::<account>:role/<task-role>",
        "arn:aws:iam::<account>:role/<execution-role>"
      ],
      "Condition": {
        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
      }
    }
  ]
}
```

The `RelayPassRuntimeRolesToEcs` statement is the one most often missed — without it the
compute deploy rolls back at `AWS::ECS::TaskDefinition` with an `iam:PassRole`
`AccessDenied`. Full reference: [infra/RUNNER_IAM.md](https://github.com/Westport-Partners/relay/blob/main/infra/RUNNER_IAM.md).
