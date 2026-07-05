# Relay — Deploy BYOR/BYOV (Locked-Down Accounts) Prompt

You are helping the user deploy Relay into an account that prohibits creating IAM roles (`iam:CreateRole` denied) and/or VPCs (`ec2:CreateVpc` denied). These constraints are common in regulated environments and government agencies. BYOR (Bring-Your-Own-Role) and BYOV (Bring-Your-Own-VPC) let you supply pre-provisioned ARNs so the compute stack imports them and creates nothing.

Canonical reference: [`docs/byor.md`](../docs/byor.md) and [`docs/deploy.md`](../docs/deploy.md) (locked-down accounts section).

---

## Goal

Deploy Relay using pre-provisioned IAM roles and/or an existing VPC, produce the inline-policy JSON from the synth output, have an account administrator apply it, then complete the deploy.

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
./scripts/relay-synth.sh -- \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

Arguments after `--` are forwarded verbatim to `cdk synth`. Templates land in `cdk.out/`.

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
./scripts/relay-deploy-direct.sh -- \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

**If only `iam:CreateRole` / `ec2:CreateVpc` are denied (but `iam:PassRole` is allowed)** — use `relay-deploy.sh`:

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh -- \
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
./scripts/relay-deploy-direct.sh -- \
  -c relay:ecs_task_role_arn=$TASK_ROLE_ARN \
  -c relay:ecs_execution_role_arn=$EXEC_ROLE_ARN \
  -c relay:vpc_id=$VPC_ID
```

---

## Terraform path (native, no CDK)

For teams that standardize on Terraform, the `infra/terraform/modules/compute` module **always imports** `vpc_id`, `private_subnet_ids`, `ecs_task_role_arn`, and `ecs_execution_role_arn` as required inputs — BYOR + BYOV are mandatory for the Terraform path. It emits the same inline-policy + trust JSON as outputs. See [`docs/deploy.md`](../docs/deploy.md) (Terraform section) and `infra/terraform/`.

---

## Next steps

- Complete configuration → [`prompts/configure.md`](configure.md)
- Diagnose failures → [`prompts/troubleshoot-deploy.md`](troubleshoot-deploy.md)
