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

## `cdk deploy` vs. `relay-deploy-direct.sh`

`cdk deploy` passes the CDK bootstrap execution role to CloudFormation via `iam:PassRole`. Many regulated accounts deny this, so `cdk deploy` fails immediately.

Use **`scripts/relay-deploy-direct.sh`** instead. It synthesizes templates locally (no AWS writes), then submits them with `aws cloudformation deploy` using your own credentials — CloudFormation acts as the caller, no execution role is passed:

```bash
# Data plane first — creates zero IAM roles and zero VPC
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh
```

> CDK bootstrap is **not required** for this path. `relay-deploy-direct.sh` never calls the bootstrap execution role.

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

Have the administrator:

1. Open **IAM → Roles → (task role) → Add permissions → Create inline policy** → JSON tab → paste `ByorTaskRoleInlinePolicy` → save.
2. Repeat for `ByorExecutionRoleInlinePolicy` on the execution role.
3. Update the trust policy on **both** roles to include `ByorEcsRoleTrust` — merge it with any existing trust entries; do not replace.

Both roles need `ecs-tasks.amazonaws.com` as a trusted principal. The `ByorEcsRoleTrust` output contains the exact trust document.

> If one role covers both task and execution responsibilities, apply **both** inline policies to that single role.

---

## Step 4 — Deploy with the same context keys

**If `iam:PassRole` is denied** — use `relay-deploy-direct.sh`:

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

**If only `iam:CreateRole` / `ec2:CreateVpc` are denied (but `iam:PassRole` is allowed)** — use `relay-deploy.sh`:

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

---

## VPC requirements (BYOV)

The imported VPC must have:
- **Public subnets** — the ALB is placed here.
- **Private subnets** — Fargate tasks run here (NAT or VPC endpoints needed for ECR, DynamoDB, SQS, SNS, CloudWatch Logs, Secrets Manager).

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
