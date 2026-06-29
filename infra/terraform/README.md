# Relay IaC — Terraform / Terragrunt

A native **Terraform** path for provisioning Relay, as an alternative to the
canonical [CDK stacks](../README.md). The modules here are kept at parity with
the CDK stacks and the pure-CLI provisioner by
[`tests/infra/test_terraform_parity.py`](../../tests/infra/test_terraform_parity.py).

> **CDK is still primary.** This Terraform path is a *maintained alternative* for
> teams that standardize on Terraform. When you change the data plane, change it
> in all three places (the CDK stack, `scripts/relay-provision-cli.sh`, and the
> `data-plane` module here) — the parity test fails if they drift.

## Modules

| Module | Parity with | Creates |
|---|---|---|
| [`modules/data-plane`](modules/data-plane) | `RelayDataStack` + `relay-provision-cli.sh` | DynamoDB table (+ `incident-status-index`/`incident-all-index` GSIs, PITR, TTL=`ttl`, stream `NEW_AND_OLD_IMAGES`), 2 SNS paging topics, SQS ingest + DLQ (redrive), CloudWatch-alarm EventBridge rule → queue |
| [`modules/compute`](modules/compute) | `RelayComputeStack` | ECS cluster + Fargate service + task def, ALB + listener(s), security groups, DLQ-depth alarm, CPU autoscaling |
| [`modules/federation`](modules/federation) | `RelayFederationStack` | (federated-hub) `relay-hub` EventBridge bus + resource policy + optional ingest rule |

The data plane is **deploy-once / RETAIN** (the DynamoDB table has
`prevent_destroy = true`); the compute plane redeploys on every image change and
imports the data plane by name + ARN.

## Built for locked-down accounts (BYOV + BYOR are required)

Unlike the CDK compute stack — which creates a VPC and IAM roles by default and
treats import as an opt-in — the Terraform **compute module always imports**
them. These inputs are **required**, with no create path:

| Input | Why |
|---|---|
| `vpc_id`, `private_subnet_ids` (`public_subnet_ids` when `internal_alb=false`) | Target accounts deny `ec2:CreateVpc`; the account ships a pre-provisioned VPC |
| `ecs_task_role_arn`, `ecs_execution_role_arn` | Target accounts deny `iam:CreateRole`; teams may only attach **inline** policies to pre-provisioned roles |
| `hub_image_uri` | Real ECR image (placeholders are rejected by a variable validation) |

Because the module can't attach policies to roles it doesn't own, it **emits the
inline-policy + trust JSON** to paste onto the two roles, as outputs:
`byor_task_role_inline_policy`, `byor_execution_role_inline_policy`,
`byor_ecs_role_trust` (mirrors `RelayComputeStack._emit_byor_outputs`).

## Using a single module (plain Terraform)

```bash
cd modules/data-plane
terraform init
terraform apply -var team_name=payments-api          # role defaults to "team"
```

```bash
cd modules/compute
terraform init
terraform apply \
  -var team_name=payments-api \
  -var hub_image_uri=<account>.dkr.ecr.us-east-1.amazonaws.com/relay-hub:<sha> \
  -var vpc_id=vpc-0123 \
  -var 'private_subnet_ids=["subnet-a","subnet-b"]' \
  -var ecs_task_role_arn=arn:aws:iam::<acct>:role/relay-task \
  -var ecs_execution_role_arn=arn:aws:iam::<acct>:role/relay-exec \
  -var 'table_name=relay-payments-api' -var 'table_arn=…' \
  -var 'ingest_queue_url=…' -var 'ingest_queue_arn=…' -var 'ingest_dlq_arn=…' \
  -var 'paging_topic_arn=…' -var 'central_paging_topic_arn=…'
```

(In practice you feed the data-plane outputs into compute — that's exactly what
the Terragrunt wiring below automates.)

## Using Terragrunt (per-environment, with dependency ordering)

[`live/`](live) wires the modules per environment. **Environment is the namespace
above org** — one Relay deployment per environment/isolation-zone — matching
`config/environments.yaml`:

```
live/
  terragrunt.hcl     # root: remote state (S3 + DynamoDB lock), provider, common inputs
  _env/env.hcl       # documented per-env input shape (template)
  {prod,dev,test}/
    env.hcl          # this env's real account values (VPC/subnets/roles/image)
    data-plane/terragrunt.hcl
    compute/terragrunt.hcl   # depends on ../data-plane; mock_outputs for plan
```

```bash
# 1. Edit live/terragrunt.hcl — set your state bucket + lock table (the CHANGE-ME values).
# 2. Edit live/<env>/env.hcl — set this account's vpc_id, subnet ids, role ARNs, image URI.
cd live/dev
terragrunt run-all plan      # compute sees mock data-plane outputs until applied
terragrunt run-all apply     # data-plane applies first; compute imports its outputs
```

The `dependency "data_plane"` block in each compute leaf resolves the table/queue/
topic ARNs from the data-plane state and orders `apply` automatically.

## Remote state

The root config uses an **S3 backend with a DynamoDB lock table** (compatible
with Terraform < 1.10). Both must already exist — Relay does **not** provision
remote-state infrastructure; point the `CHANGE-ME` values at your own bucket
(`relay-tf-state`-style) and lock table (partition key `LockID`).

## Verifying

```bash
# Format + validate every module (no AWS writes; -backend=false skips state).
terraform fmt -check -recursive .
for m in data-plane compute federation; do
  ( cd modules/$m && terraform init -backend=false && terraform validate )
done

# HCL formatting for the Terragrunt configs.
terragrunt hclfmt --terragrunt-check

# Parity + BYOR/BYOV invariants (pure Python; runs in CI).
pytest tests/infra/test_terraform_parity.py -v
```

## Requirements

- Terraform >= 1.6, AWS provider >= 5.0
- Terragrunt (for the `live/` wiring) — tested on v0.55
- AWS credentials for the target account
