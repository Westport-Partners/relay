# Relay — Deploy BYOR/BYOV (Locked-Down Accounts) Prompt

You are helping the user deploy Relay into an account that prohibits creating IAM roles (`iam:CreateRole` denied) and/or VPCs (`ec2:CreateVpc` denied). These constraints are common in regulated environments and government agencies. BYOR (Bring-Your-Own-Role) and BYOV (Bring-Your-Own-VPC) let you supply pre-provisioned ARNs so the compute stack imports them and creates nothing.

Canonical reference: [`docs/byor.md`](../docs/byor.md) and [`docs/deploy.md`](../docs/deploy.md) (locked-down accounts section).

---

## Goal

Deploy Relay using pre-provisioned IAM roles and/or an existing VPC, produce the inline-policy JSON from the synth output, have an account administrator apply it, then complete the deploy.

> **Do NOT run `scripts/relay-provision-cli.sh` on this path.** That script creates
> the DynamoDB table, SNS topics, SQS queues, and EventBridge resources directly via
> the AWS CLI. `relay-deploy-direct.sh` (below) creates the *same* resources via
> CloudFormation. Running the CLI provisioner first makes the data-stack deploy fail
> with `AWS::EarlyValidation::ResourceExistenceCheck` — CloudFormation refuses to
> create resources that already exist. The two paths are mutually exclusive: pick
> `relay-deploy-direct.sh` and never run `relay-provision-cli.sh` alongside it. (If
> you already ran it, tear those resources down with `scripts/relay-teardown-cli.sh`
> before deploying.)

## Preconditions

- Preflight has been run: `./scripts/relay-preflight.sh`. The WARN on `iam:CreateRole` / `ec2:CreateVpc` is expected here — that is exactly why you are on this path.
- **Node.js 20+** is installed (CDK synth runs `npx aws-cdk@2`, which dropped the EOL Node 18; 22 recommended). On Amazon Linux 2023, `dnf install nodejs` gives EOL Node 18 — use `dnf install nodejs22`.
- **Python CDK deps are installed in a venv:** `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[infra]'`. `relay-synth.sh` / `relay-deploy-direct.sh` auto-activate `.venv/` if present, but do not create it or install deps — without the `[infra]` extra (`aws-cdk-lib` + `constructs`) the synth fails with `ModuleNotFoundError: aws_cdk`.
- An account administrator has provisioned (or can identify) two IAM roles: one ECS task role and one ECS execution role. One role may cover both responsibilities — pass the same ARN for both context keys.
- VPC ID is available if `ec2:CreateVpc` is also denied.
- `RELAY_HUB_IMAGE_URI` is set (built with `relay-build-hub-image.sh` — see [`prompts/deploy-team.md`](deploy-team.md) Step 2).

---

## IAM surface

Relay runs as a **single always-on container**. The IAM surface is exactly **two roles**:

- **ECS task role** — DynamoDB operations, SNS Publish on paging topic, optional secrets reads, optional `events:PutEvents` to the federation bus.
- **ECS execution role** — ECR image pull, CloudWatch Logs writes.

There is no Lambda role, no EventBridge Scheduler role, and no `iam:PassRole` grant in the task definition.

> **Direct-to-contact SMS ("Test page" and targeted pages) needs an opt-in.** SMS to
> a specific phone uses `sns:Publish` with a *phone number* resource (not a topic ARN),
> which the base task policy does not cover. The synth only adds the `RelayHubDirectSms`
> statement to `ByorTaskRoleInlinePolicy` when you pass **`-c relay:enable_direct_sms=true`**
> (or set `RELAY_ENABLE_DIRECT_SMS=true`) on the synth in Step 2. Without it, "Test page"
> returns 200 but no SMS is delivered and the logs show `AuthorizationError ... sns:Publish`.
> Set it before generating the policy so the administrator applies the complete policy in
> one pass. IAM changes take effect on the **next task launch**, so after any policy edit
> run `aws ecs update-service --cluster relay-hub --service relay-hub --force-new-deployment`.

---

## The only deploy path on locked-down accounts: `relay-deploy-direct.sh`

On this path you deploy **exclusively** with `scripts/relay-deploy-direct.sh`. Do
**not** use `cdk deploy` (nor `relay-deploy.sh`, which wraps it): `cdk deploy`
passes the CDK bootstrap execution role to CloudFormation via `iam:PassRole`,
which locked-down accounts deny — so it fails immediately.

`relay-deploy-direct.sh` synthesizes templates locally (no AWS writes), then
submits them with `aws cloudformation deploy` using your own credentials.
CloudFormation acts as the caller — **no bootstrap execution role is passed**:

```bash
# Data plane first — creates zero IAM roles and zero VPC
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh
```

> CDK bootstrap is **not required** for this path. The stacks synth with the
> bootstrap-version rule suppressed, so the templates carry no `/cdk-bootstrap`
> SSM lookup and `relay-deploy-direct.sh` never touches the bootstrap role.

> **`iam:PassRole` is still required — but narrowly.** "No bootstrap role is
> passed" is *not* the same as "no PassRole at all." Registering the ECS task
> definition requires the **deploy identity** to `iam:PassRole` the task role and
> execution role to `ecs-tasks.amazonaws.com` — this is intrinsic to ECS and no
> deploy method avoids it. Locked-down accounts handle this by allowing
> `iam:PassRole` **scoped to exactly those two role ARNs** with an
> `iam:PassedToService = ecs-tasks.amazonaws.com` condition, while denying broad
> PassRole. See **Deploy-identity permissions** below. If your account denies
> `iam:PassRole` outright with no scoped exception, the ECS/Fargate path is not
> possible — run the released container directly instead
> ([`docs/local-dev.md`](../docs/local-dev.md), "Run on EC2 against real AWS").

---

## Deploy-identity permissions

BYOR covers the two **runtime** roles (task + execution) via the synth-emitted
inline policies below. Separately, the **deploy identity** — the principal that
runs `relay-deploy-direct.sh` (an instance profile on the deploy box, or your
CLI credentials) — needs permission to create the stacks' resources. In a
locked-down account an administrator attaches this as an inline policy on your
pre-provisioned deploy/instance role:

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

The `RelayPassRuntimeRolesToEcs` statement is the one locked-down accounts most
often miss — without it the compute deploy rolls back at
`AWS::ECS::TaskDefinition` with an `iam:PassRole` `AccessDenied`. (`ec2:CreateVpc`
is **not** needed — the VPC is imported via BYOV.)

---

## Step 1 — Identify your roles and VPC

Obtain the ARNs from the account administrator or internal service catalog:

```
TASK_ROLE_ARN=arn:aws:iam::<account>:role/<task-role>
EXEC_ROLE_ARN=arn:aws:iam::<account>:role/<exec-role>
VPC_ID=vpc-<id>
```

---

## Step 2 — Synth to generate the policy outputs (no AWS writes)

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-synth.sh \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID \
  -c relay:enable_direct_sms=true   # include if you use direct-to-contact SMS / "Test page"
```

Trailing `-c relay:*` flags are forwarded verbatim to `cdk synth`. Templates land in `cdk.out/`. Do **not** insert a `--` before the flags — CDK's arg parser treats everything after `--` as positional and silently ignores it.

**Extract the policy outputs:**

```bash
# View all outputs from the compute stack
cat cdk.out/RelayComputeStack.template.json | jq '.Outputs'
```

The three outputs to hand to the account administrator:

| Output key | Action |
|---|---|
| `ByorTaskRoleInlinePolicy` | Add as inline policy on the task role |
| `ByorExecutionRoleInlinePolicy` | Add as inline policy on the execution role |
| `ByorEcsRoleTrust` | Update the trust policy on **both** roles |

---

## Step 3 — Account administrator applies the policies

Have the administrator run **`scripts/relay-apply-byor-policies.sh`** (needs `aws` +
`jq`, and permission to call `iam:PutRolePolicy` / `iam:UpdateAssumeRolePolicy` on
both roles):

```bash
./scripts/relay-apply-byor-policies.sh <task-role-name> <exec-role-name>

# Also force a fresh ECS deployment so running tasks pick up the new
# permissions immediately (otherwise they take effect on the next deploy):
./scripts/relay-apply-byor-policies.sh <task-role-name> <exec-role-name> --force-redeploy
```

The script:

1. Reads `ByorTaskRoleInlinePolicy`, `ByorExecutionRoleInlinePolicy`, and
   `ByorEcsRoleTrust` from the `RelayComputeStack` CloudFormation outputs (from
   Step 2 — synth or deploy the compute stack first).
2. `iam:PutRolePolicy`'s the two inline policies onto the task and execution roles.
3. Merges `ecs-tasks.amazonaws.com` into **both** roles' trust policies — safely: if a
   role already trusts `ecs-tasks.amazonaws.com` it's a no-op, otherwise the required
   trust statement is **appended** alongside whatever the role already trusts (it
   never overwrites the existing document). If the merge can't be computed
   confidently, it prints the current trust doc and the exact statement to add, and
   asks you to apply it by hand instead of guessing.

Idempotent — safe to re-run after a re-synth (e.g. if the policy content changed).

> If one role covers both task and execution responsibilities, pass that same role
> name as both arguments — the script applies both inline policies to it.

**Manual fallback** (if you prefer the IAM console or don't have `jq`):

1. Open **IAM → Roles → (task role) → Add permissions → Create inline policy** → JSON tab → paste `ByorTaskRoleInlinePolicy` → save.
2. Repeat for `ByorExecutionRoleInlinePolicy` on the execution role.
3. Update the trust policy on **both** roles to include `ByorEcsRoleTrust` — merge it with any existing trust entries; do not replace.

Both roles need `ecs-tasks.amazonaws.com` as a trusted principal. The `ByorEcsRoleTrust` output contains the exact trust document.

---

## Step 4 — Deploy with the same context keys

Deploy the data stack first (no IAM, no VPC), then the compute stack with the
BYOR/BYOV context. Always use `relay-deploy-direct.sh` on this path — never
`relay-deploy.sh`/`cdk deploy` (see the path note above).

```bash
# Data stack first (no IAM, no VPC)
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh

# Then compute stack with BYOR/BYOV context
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=compute \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy-direct.sh \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

The deploy identity needs the inline policy from **Deploy-identity permissions**
above (including the scoped `iam:PassRole`); the compute step fails at the ECS
task definition without it.

---

## VPC requirements (BYOV)

The imported VPC must have:
- **Public subnets** — the ALB is placed here.
- **Private subnets** — Fargate tasks run here. They need outbound reachability to the AWS APIs below, via **either** a NAT gateway **or** the specific VPC endpoints listed (substitute your region for `<region>`):

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

  Interface endpoints need a security group allowing inbound HTTPS (443) from the Fargate task security group. With neither NAT nor these endpoints, ECS tasks fail to start with no clear error; `relay-preflight.sh` emits a WARN when it detects a BYOV VPC lacking both.

`from_lookup` queries the live account at synth time and caches the result to `cdk.context.json`. Commit or carry `cdk.context.json` so CI synths are reproducible without live AWS access.

---

## Scoped re-deploys in BYOR mode

Pass the same context keys on every compute deploy. Use `RELAY_STACK_SELECTOR=compute` for image-only updates:

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
RELAY_STACK_SELECTOR=compute \
./scripts/relay-deploy-direct.sh \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

> **`RELAY_HUB_IMAGE_URI` does not survive a new shell.** If you built the image in
> one terminal/session and are running this re-deploy in another (a new tab, a new
> SSH session, or separate automation tool calls), the `export` from the build step
> is gone here — the command above then fails with the misleading
> `relay:hub_image_uri is empty` error, because the variable was never actually in
> scope. Rather than re-exporting by hand, resolve it fresh from ECR:
>
> ```bash
> RELAY_HUB_IMAGE_URI="$(./scripts/relay-get-latest-image.sh)"
> ```
>
> `relay-get-latest-image.sh` queries ECR directly for the most recently pushed
> `relay-hub` tag and prints the fully-qualified URI — it doesn't rely on anything
> exported earlier, so Steps that build and Steps that deploy can run in completely
> independent shells.

> **Same image tag = no rollout.** CloudFormation only starts an ECS deployment when
> the task definition changes. `relay-build-hub-image.sh` tags by git short SHA, so a
> normal commit-and-rebuild produces a new tag and rolls automatically. But if you
> rebuild the **same SHA** (e.g. to fix a Dockerfile issue without committing), the
> image URI is unchanged, the task def is unchanged, and ECS keeps running the old
> revision even though the deploy "succeeds". Either build a distinct `IMAGE_TAG` or
> force a rollout:
>
> ```bash
> aws ecs update-service --cluster relay-hub --service relay-hub --force-new-deployment
> ```

### Faster re-deploys with Express Mode (opt-in)

`relay-deploy-direct.sh` waits for full resource stabilization by default — for the
compute stack that means the entire ECS service roll (health checks passing), often
15-20+ minutes. During iterative BYOR work you can opt into CloudFormation **Express
Mode**, which returns as soon as resource *configuration* is applied and lets ECS/ALB
finish coming up in the background:

```bash
RELAY_CFN_MODE=EXPRESS \
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=compute \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy-direct.sh \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

- **Default is `STANDARD`** — omit `RELAY_CFN_MODE` and behavior is unchanged.
- **Requires AWS CLI ≥ 2.35** (the version that added `--deployment-config`). The
  script fails fast with a clear message on older CLIs, and `relay-preflight.sh`
  flags it as the `aws-cli-express` WARN. To upgrade, follow
  [`prompts/upgrade-aws-cli.md`](upgrade-aws-cli.md).
- **"Success" ≠ "serving traffic."** The command returns before the ECS service is
  healthy. If you need to gate on readiness, poll:
  `aws ecs wait services-stable --cluster relay-hub --services relay-hub`.
- **`wait services-stable` does not confirm the new image is live.** It gates only on
  `runningCount == desiredCount`, so if a healthy task on the *prior* image is already
  running, the wait can return while the replacement is still rolling — and a
  subsequent `/health/ready` may report the old image's state. Before trusting the
  health check on an EXPRESS deploy, confirm the running task is on the expected image:
  ```bash
  TASK_ARN=$(aws ecs list-tasks --cluster relay-hub --service-name relay-hub \
    --region us-east-1 --query 'taskArns[0]' --output text)
  aws ecs describe-tasks --cluster relay-hub --tasks "$TASK_ARN" \
    --region us-east-1 \
    --query "tasks[0].containers[?name=='relay-hub'].image | [0]" --output text
  ```
  Query by container **name**, not index — in accounts with AWS GuardDuty Runtime
  Monitoring enabled, ECS can inject an agent sidecar that appears as
  `containers[0]`, shadowing the real `relay-hub` container and causing an
  index-based query to silently return `None`.
  If it doesn't match, force a fresh roll of the latest task definition and wait again:
  ```bash
  LATEST=$(aws ecs describe-services --cluster relay-hub --services relay-hub \
    --region us-east-1 --query 'services[0].deployments[0].taskDefinition' --output text)
  aws ecs update-service --cluster relay-hub --service relay-hub \
    --task-definition "$LATEST" --force-new-deployment --region us-east-1
  aws ecs wait services-stable --cluster relay-hub --services relay-hub --region us-east-1
  ```
  This only bites on a **re-deploy** onto a service with a running healthy task — a
  first-time stack *create* has no prior task to keep serving, so there's nothing to
  confirm. STANDARD mode isn't affected either — it waits for the full service roll.
- **Same-tag rebuilds still don't roll** (see the note above) — that's a task-def
  identity thing, independent of the deploy mode.
- Rollback stays enabled (`DisableRollback:false`), so a failed EXPRESS update rolls
  back rather than stranding the stack.
- Best for the **compute** stack (the slow one). The data stack is already fast.

---

## Teardown

`cdk destroy` requires `iam:PassRole` just like `cdk deploy`, so on this path tear down
via **`scripts/relay-teardown-direct.sh`**:

```bash
# Standard teardown (confirms before deleting)
RELAY_TEAM_NAME=<team> AWS_REGION=us-east-1 ./scripts/relay-teardown-direct.sh

# Also delete ECR images (opt-in — all images in relay-hub are removed)
RELAY_TEAM_NAME=<team> AWS_REGION=us-east-1 ./scripts/relay-teardown-direct.sh --purge-ecr

# Non-interactive (CI / automation)
RELAY_TEAM_NAME=<team> AWS_REGION=us-east-1 RELAY_FORCE=1 ./scripts/relay-teardown-direct.sh
```

The script implements the sequence below in order, waiting for each step:

1. **Compute stack** (`RelayComputeStack`) — ECS service, ALB, security groups.
2. **Data stack** (`RelayDataStack`) — SNS, SQS, EventBridge. The DynamoDB table has a `RETAIN` deletion policy and **survives** this step.
3. **DynamoDB table** (`relay-<team>`) — deleted explicitly because the RETAIN policy keeps it after the stack is gone.
4. **ECR images** (opt-in: `--purge-ecr` / `RELAY_PURGE_ECR=1`) — removes all images in `relay-hub`. The repository itself is left intact.

All steps are idempotent; already-absent stacks are skipped gracefully.

**Manual fallback** (if you prefer raw CLI commands):

```bash
# 1. Compute stack (ECS service, ALB, security groups)
aws cloudformation delete-stack --stack-name RelayComputeStack --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete --stack-name RelayComputeStack --region "$AWS_REGION"

# 2. Data stack (SNS, SQS, EventBridge — the DynamoDB table is retained)
aws cloudformation delete-stack --stack-name RelayDataStack --region "$AWS_REGION"
aws cloudformation wait stack-delete-complete --stack-name RelayDataStack --region "$AWS_REGION"

# 3. DynamoDB table (RETAIN policy means it outlives the stack) — deletes incident history
aws dynamodb delete-table --table-name relay-<team> --region "$AWS_REGION"

# 4. (Optional) ECR images
aws ecr batch-delete-image --repository-name relay-hub --region "$AWS_REGION" \
  --image-ids "$(aws ecr list-images --repository-name relay-hub --region "$AWS_REGION" --query 'imageIds[*]' --output json)"
```

> `scripts/relay-teardown-cli.sh` only removes resources created by `relay-provision-cli.sh`.
> It does **not** apply to CloudFormation-deployed stacks — use `relay-teardown-direct.sh` above.

---

## Terraform path (native, no CDK)

For teams that standardize on Terraform, the `infra/terraform/modules/compute` module **always imports** `vpc_id`, `private_subnet_ids`, `ecs_task_role_arn`, and `ecs_execution_role_arn` as required inputs — BYOR + BYOV are mandatory for the Terraform path. It emits the same inline-policy + trust JSON as outputs. See [`docs/deploy.md`](../docs/deploy.md) (Terraform section) and `infra/terraform/`.

---

## Next steps

- Complete configuration → [`prompts/configure.md`](configure.md)
- Diagnose failures → [`prompts/troubleshoot-deploy.md`](troubleshoot-deploy.md)
