# Relay — Deployment Guide

Relay runs as a single always-on ECS Fargate container, deployed via AWS CDK (Python v2).
All deploy logic lives in portable shell scripts under `scripts/`; the CI pipeline calls
the same scripts as a local operator does.

For locked-down accounts that cannot create IAM roles or VPCs, see [byor.md](byor.md).

---

## Topologies and stacks

Relay deploys as **three independently deployable stacks**. You choose a topology with the
`RELAY_DEPLOY_TYPE` environment variable; the topology determines which stacks are active.

| Stack | What it owns | Deploy cadence |
|---|---|---|
| **RelayDataStack** | DynamoDB table (`relay-<team>`) + `incident-status-index` GSI + stream + two SNS paging topics | Once; RETAIN on delete |
| **RelayComputeStack** | VPC (or imported BYOV), ECS cluster, always-on Fargate service (2–8 tasks, circuit-breaker + auto-rollback), ALB, CloudWatch-alarm EventBridge rule → SQS ingress + DLQ, one task role + one execution role | Every image change |
| **RelayFederationStack** | `relay-hub` EventBridge bus + org-scoped `PutEvents` resource policy + ingest rule | Rarely; federated-hub topology only |

| `RELAY_DEPLOY_TYPE` | Stacks deployed |
|---|---|
| `team` (default) | RelayDataStack + RelayComputeStack |
| `federated-hub` | RelayDataStack + RelayComputeStack + RelayFederationStack |

---

## Prerequisites

- Python 3.12+, Node.js 18+, Docker
- CDK CLI: `npm i -g aws-cdk` (or the scripts fall back to `npx aws-cdk@2`)
- Python dependencies in a venv: `pip install aws-cdk-lib constructs` (the deploy scripts
  activate `.venv/` automatically if it exists)
- AWS credentials for the target account with the permissions described in
  [infra/RUNNER_IAM.md](https://github.com/Westport-Partners/relay/blob/main/infra/RUNNER_IAM.md)

---

## Fresh team deploy (step by step)

### 1. Bootstrap CDK (once per account/region)

```bash
AWS_REGION=us-east-1 ./scripts/relay-bootstrap.sh
```

Idempotent — safe to re-run. Creates the CDK bootstrap stack (`CDKToolkit`) if absent.

### 2. Build and push the container image

```bash
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"
```

The script builds the Docker image, creates the `relay-hub` ECR repository if it does not
exist, authenticates Docker to ECR, pushes the image, and prints the fully-qualified URI
on the last line. The tag defaults to the git short SHA; override with `IMAGE_TAG=<tag>`.

To bake in your team's config files instead of the in-repo defaults, set `RELAY_CONFIG_DIR`
to the directory that holds your `*.yaml` files before running the script. The originals
are restored automatically after the build even if the build fails.

The build uses Docker's default bridge network. On WSL2, VPNs, and locked-down corporate
networks the bridge often can't resolve DNS during `RUN` steps (e.g. `apt-get`) even when
the host has connectivity; set `DOCKER_BUILD_NETWORK=host` to build against the host's
network stack instead. It only affects build-time steps; the pushed image is identical.

**CPU architecture.** The image is built for the build host's architecture. The deploy
scripts auto-detect it (`relay-context.sh` → `relay:cpu_arch`) and set the Fargate task's
`RuntimePlatform` to match, so an aarch64 host deploys ARM64 tasks with no operator action
— otherwise the task dies at launch with `exec format error`. Override with `RELAY_CPU_ARCH`
(`X86_64` | `ARM64`), e.g. when cross-building.

`RelayComputeStack` **fails fast at synth** if `RELAY_HUB_IMAGE_URI` is unset or contains
`amazonlinux`/`PLACEHOLDER`. Build the image first; never skip this step.

### 3. Synth and review (no AWS writes)

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-synth.sh
```

Templates land in `cdk.out/`. Review before deploying.

### 4. Deploy

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

Deploys data first, then compute. Outputs are written to `cdk.outputs.json`; the
`DashboardUrl` output is your dashboard URL.

---

## Scoped deploys (the inner loop)

`RELAY_STACK_SELECTOR` narrows a deploy to one stack. All deploys use `--exclusively`, so
a compute deploy can **never** touch the data plane.

| `RELAY_STACK_SELECTOR` | Effect |
|---|---|
| `data` | Deploy only RelayDataStack (run once, then leave it alone) |
| `compute` | Deploy only RelayComputeStack (every image change) |
| `federation` | Deploy only RelayFederationStack |
| unset / `all` | Deploy the full topology set |

Example — image update inner loop:

```bash
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"

RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
RELAY_STACK_SELECTOR=compute \
./scripts/relay-deploy.sh
```

---

## Federated-hub deploy

A federated hub is the org-wide aggregator: a single always-on container that serves the
NOC big-board and owns the `relay-hub` EventBridge bus that every team account forwards
SEV1/SEV2 escalations up to. The team topology stands alone and needs none of this — the
hub is only for organizations that want one cross-team view across many accounts.

This is a **shared service consumed by every team account**, so *where* you deploy it and
*what cross-account trust it requires* matter more than for a single-team deploy. Read this
whole section before deploying — the bus resource policy below grants every account in your
AWS Organization the right to send events to the hub, and that is not something to enable by
copy-paste.

### Choosing the hub account

Deploy the hub into a **dedicated shared-services account**, not an existing team account and
**never the AWS Organizations management (root) account**.

| Option | Verdict |
|---|---|
| **Dedicated shared-services account** (own account under a "Core"/shared-services OU) | **Recommended.** Matches AWS best practice (keep workloads out of the org root), isolates the org-wide ingress, and gives security review one small, purpose-built account to assess. |
| An existing team's account | Workable for a small org, but couples the org-wide NOC to one team's blast radius and IAM. Avoid for anything shared across orgs/agencies. |
| The Organizations **management account** | **Do not.** It runs an internet-facing workload and an org-wide `PutEvents` ingress in your most privileged account — a finding in any security review. |

> **For organizations that manage accounts as code:** create the shared-services account in
> your account/organization IaC (e.g. an `aws_organizations_account` resource under a Core OU),
> not by hand and not from this repo. A newly Org-created account automatically gets an
> `OrganizationAccountAccessRole` the management account can assume — that is the only access
> the deploy below needs to reach the new account.

### What gets created, and where

A federated-hub deploy provisions **three stacks in the hub account** — the same data + compute
stacks as a team, **plus** `RelayFederationStack`:

| Resource (hub account) | Why it matters for review |
|---|---|
| `relay-hub` EventBridge bus | The cross-account ingress point. |
| Bus **resource policy** | Allows `events:PutEvents` from every principal in the org, gated by `aws:PrincipalOrgID` (see below). |
| Ingest rule → SQS queue | Routes `relay.*` events on the bus into the hub's ingest queue. |
| Data + compute stacks | DynamoDB table, Fargate service + ALB (the big-board), one task role + one execution role. |

The bus policy is scoped to your organization ID and **not** open to the world:

```json
{
  "Effect": "Allow",
  "Principal": "*",
  "Action": "events:PutEvents",
  "Resource": "arn:aws:events:<region>:<hub-account>:event-bus/relay-hub",
  "Condition": { "StringEquals": { "aws:PrincipalOrgID": "o-xxxxxxxxxxxx" } }
}
```

`aws:PrincipalOrgID` means only principals in *your* AWS Organization can put events, and it
covers current **and future** org accounts with no per-account policy edits. If you omit
`RELAY_ORG_ID`, the policy falls back to same-account-only ingress (the hub can still receive
its own events, but no team account can forward up).

### Permissions: what is needed where

| Where | Who attaches it | Permission |
|---|---|---|
| **Hub account** (deploy identity) | Account admin, once | The **Federated-hub deploy** policy in [infra/RUNNER_IAM.md](https://github.com/Westport-Partners/relay/blob/main/infra/RUNNER_IAM.md) — the team policy plus the EventBridge-bus statement. |
| **Hub account** (running container) | Created by the deploy | One ECS task role + one execution role. No `PassRole`, no Lambda role. See [byor.md](byor.md) for the exact two-role surface in locked-down accounts. |
| **Each team account** (running container) | Created by that team's deploy | `events:PutEvents` scoped to the single hub bus ARN — nothing else cross-account. The team grants *itself* the right to forward up; the hub never reaches into a team account. |

Trust flows **one way**: team accounts push to the hub bus. The hub holds no credentials for,
and makes no calls into, any team account.

### Integrations are optional — no secret prerequisites

A hub has **no secret prerequisites**. The GitLab and ServiceNow integrations are entirely
optional, on the hub exactly as on a team. Deploy the hub with nothing configured and it
serves the big-board and pages the central on-call as normal; an unconfigured adapter simply
isn't loaded.

When you *do* want an integration, paste its token on the **Settings** screen in the
dashboard. Relay stores it in its own DynamoDB table (the same table as incident state,
server-side encrypted) and reads it at runtime — there is no Secrets Manager secret to
pre-create, and a missing token never blocks a deploy.

> Run `scripts/relay-preflight.sh` first regardless; it verifies toolchain, identity, IAM
> capability (role/VPC creation), and CDK bootstrap. It works whether you authenticate
> directly or via an assumed role such as `OrganizationAccountAccessRole`.

### Deploy the hub

In the hub account (e.g. via its `OrganizationAccountAccessRole`):

```bash
RELAY_DEPLOY_TYPE=federated-hub \
RELAY_ORG_ID=o-xxxxxxxxxxxx \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

Take the `EventBusArn` from `cdk.outputs.json` and hand it to each team deploy:

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_SCOPE=local-federated \
RELAY_UPSTREAM_HUB_BUS_ARN=<EventBusArn from above> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

With `RELAY_HUB_SCOPE=local-federated`, the team container forwards SEV1/SEV2 escalations up
to the federated bus.

### Verifying the hub

```bash
# All three stacks should report *_COMPLETE
aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName,'Relay')].[StackName,StackStatus]" --output text

# The big-board should answer 200 (DashboardUrl is in cdk.outputs.json)
curl -s -o /dev/null -w '%{http_code}\n' "$(jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json)"

# The bus policy should be org-scoped to YOUR org id
aws events describe-event-bus --name relay-hub --query Policy --output text
```

### Note for federal contractors and locked-down accounts

You do **not** have to trust the `install.sh` one-liner or any script in this repo to run
Relay. Everything here is auditable and runnable by hand:

- The installer only installs toolchain and clones the repo; read [install.md](install.md)
  for its exact six steps, or do a **manual install** (`git clone` + `pip install -e .`) and
  skip it entirely.
- All deploy logic lives in the `scripts/relay-*.sh` shell scripts and the CDK app under
  `infra/` — both are plain-text and reviewable. `relay-synth.sh` produces the exact
  CloudFormation templates (in `cdk.out/`) with **no AWS writes**, so your security team can
  review precisely what will be created before anything is deployed.
- If your accounts prohibit creating IAM roles or VPCs, use **BYOR/BYOV** mode — you supply
  pre-provisioned role and VPC ARNs and Relay creates none. See [byor.md](byor.md).
- The only cross-account grant in the whole topology is the org-scoped `events:PutEvents`
  bus policy shown above. There is no central service that holds your credentials and no
  inbound path from the hub into a team account.

---

## Locked-down accounts (`iam:PassRole` denied)

`cdk deploy` hands the CDK bootstrap CloudFormation execution role to
CloudFormation via `iam:PassRole`. Many regulated and government accounts deny
`iam:PassRole` in an identity-based policy, so `cdk deploy` fails before it
creates anything:

```
User: arn:aws:iam::<account>:role/<runner-role>/... is not authorized to perform:
iam:PassRole on resource: arn:aws:iam::<account>:role/cdk-hnb659fds-cfn-exec-role-...
with an explicit deny in an identity-based policy
```

Use **`scripts/relay-deploy-direct.sh`** instead. It synthesizes the templates
locally (no AWS writes), then submits each one with `aws cloudformation deploy`
using **your own credentials** — CloudFormation acts as the caller, so there is
no execution role to pass:

```bash
# Data plane first — creates zero IAM roles and zero VPC, so it deploys even in
# the most restricted accounts. This is the right first step on a fresh account.
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh
```

Relay's stacks reference the container image by registry URI (no CDK file or
image assets), so the synthesized templates are self-contained and deploy this
way with no asset-publishing step. The script takes the **same environment
variables** as `relay-deploy.sh` (`RELAY_STACK_SELECTOR`, `RELAY_TEAM_NAME`,
`RELAY_HUB_IMAGE_URI`, …).

For the **compute** stack in accounts that *also* deny `iam:CreateRole` /
`ec2:CreateVpc`, supply pre-provisioned ARNs (`relay:ecs_task_role_arn`,
`relay:ecs_execution_role_arn`, `relay:vpc_id`) so the stack imports rather than
creates them — see [byor.md](byor.md).

> **CDK bootstrap is not required for this path.** `relay-deploy-direct.sh` never
> calls the bootstrap execution role. If your account's bootstrap stack is pinned
> at an older version by a platform team (e.g. v18) and you cannot update it, the
> "outdated bootstrap version" notice CDK prints during `synth` is **safe to
> ignore** — synth makes no AWS writes and the direct deploy does not use the
> bootstrap roles. (A normal `cdk deploy` of the **compute** stack does expect a
> current bootstrap; the data plane does not.)

---

## Terraform / Terragrunt path (native, no CDK)

CDK is Relay's canonical IaC, but the same topology can be provisioned with
**hand-written Terraform** for teams that standardize on it. The modules live in
[`infra/terraform/`](../infra/terraform/) and are kept at parity with the CDK
stacks by `tests/infra/test_terraform_parity.py`.

| Module | Parity with | What it creates |
|---|---|---|
| `modules/data-plane` | `RelayDataStack` + `relay-provision-cli.sh` | DynamoDB (+GSIs/PITR/TTL/stream), 2 SNS topics, SQS ingest + DLQ, CloudWatch-alarm EventBridge rule |
| `modules/compute` | `RelayComputeStack` | ECS cluster/service/task-def, ALB + listeners, DLQ-depth alarm, CPU autoscaling |
| `modules/federation` | `RelayFederationStack` | (federated-hub) `relay-hub` EventBridge bus + policy + ingest rule |

**Built for locked-down accounts:** unlike the CDK compute stack (which creates a
VPC and IAM roles by default), the Terraform compute module **always imports**
them — `vpc_id`, `private_subnet_ids`, `ecs_task_role_arn`, and
`ecs_execution_role_arn` are **required** inputs (BYOV + BYOR are mandatory, since
these accounts deny `ec2:CreateVpc` and `iam:CreateRole`). The module emits the
inline-policy + trust JSON to paste onto the two pre-provisioned roles as the
`byor_task_role_inline_policy` / `byor_execution_role_inline_policy` /
`byor_ecs_role_trust` outputs.

Use the modules directly, or via the **Terragrunt** wiring in
[`infra/terraform/live/`](../infra/terraform/live/) — one stack per environment
(env is the namespace above org), with `data-plane → compute` dependency ordering
resolved automatically:

```bash
cd infra/terraform/live/dev   # or prod / test
# edit env.hcl with this account's VPC/subnets/role ARNs + image URI,
# and the root terragrunt.hcl with your state bucket + lock table, then:
terragrunt run-all plan
terragrunt run-all apply       # data plane applies first, compute imports it
```

To use a single module without Terragrunt:

```bash
cd infra/terraform/modules/data-plane
terraform init && terraform apply -var team_name=<team>
```

See the [`infra/terraform/`](../infra/terraform/) README for the full input
reference.

---

## Deploy environment variables

### Required

| Variable | Description |
|---|---|
| `RELAY_DEPLOY_TYPE` | `team` (default) or `federated-hub` — selects the topology |
| `RELAY_TEAM_NAME` | Team identifier; names the DynamoDB table `relay-<team>`. Required for `team`. |
| `RELAY_ORG_ID` | AWS organization ID (e.g. `o-xxxxxxxxxxxx`). Required for `federated-hub`. |
| `RELAY_HUB_IMAGE_URI` | ECR image URI. Required for any compute deploy. Build with `relay-build-hub-image.sh`. |

### Scoping and approval

| Variable | Default | Description |
|---|---|---|
| `RELAY_STACK_SELECTOR` | (full topology) | `data` \| `compute` \| `federation` — deploy one stack |
| `AWS_REGION` | `us-east-1` | Target region |
| `RELAY_REQUIRE_APPROVAL` | `never` | CDK approval mode: `never` \| `any-change` \| `broadening` |

### Topology and federation

| Variable | Default | Description |
|---|---|---|
| `RELAY_HUB_SCOPE` | `local` (team) / `central` (fed-hub) | `local` \| `local-federated` \| `central` |
| `RELAY_UPSTREAM_HUB_BUS_ARN` | — | Federated bus ARN; required when `RELAY_HUB_SCOPE=local-federated`. |

### Image build (`relay-build-hub-image.sh`)

| Variable | Default | Description |
|---|---|---|
| `IMAGE_TAG` | git short SHA | Image tag. A new tag is what triggers an ECS roll; rebuilding the same tag does not. |
| `DOCKER_BUILD_NETWORK` | — | Value forwarded to `docker build --network=<v>`. Unset uses Docker's default bridge; set `host` to work around WSL2/VPN/locked-down bridge networks that can't resolve DNS during `RUN` steps. |
| `RELAY_CPU_ARCH` | auto (`uname -m`) | Override the detected build-host arch (`X86_64` \| `ARM64`). Sets the Fargate task `RuntimePlatform` via `relay:cpu_arch` so the task matches the image; a mismatch fails at launch with `exec format error`. |

### Networking

| Variable | Default | Description |
|---|---|---|
| `RELAY_INTERNAL_ALB` | `true` (internal) | `true`: ALB in private subnets, reachable only from the corporate network/VPN — the right posture for an internal utility. `false`: public, internet-facing ALB. ECS tasks stay private either way. |

> The ALB is **internal by default**. If you deploy into an account with no VPN or peering into the VPC, set `RELAY_INTERNAL_ALB=false` or the dashboard will be unreachable from outside the VPC.

### Container behavior

| Variable | Default | Description |
|---|---|---|
| `RELAY_TZ` | `UTC` | IANA timezone for on-call schedule resolution |
| `RELAY_LOG_LEVEL` | `INFO` | Container log level |
| `RELAY_UI_AUTH_MODE` | `none` | UI auth: `none` (public read-only) \| `alb` \| `dev` |
| `RELAY_UI_DEV_USER` | — | Dev username when `RELAY_UI_AUTH_MODE=dev` |
| `RELAY_CONFIG_SOURCE` | `local` | Config source. Defaults to `local` (bundled `config/` at `/app/config`) so a fresh hub seeds its routing/ignore rules on first boot; set to `gitlab` for a GitLab config source |

### Node self-identity (tile key)

| Variable | Default | Description |
|---|---|---|
| `RELAY_NODE_APP_NAME` | team_name | Application name on the dashboard tile |
| `RELAY_NODE_DEPLOYMENT_ID` | — | Deployment ID; with `environment` forms the tile key |
| `RELAY_NODE_ENVIRONMENT` | — | Environment label (e.g. `prod`) |

### Integrations (all optional)

| Variable | Description |
|---|---|
| `RELAY_GITLAB_REPO` | GitLab project path for config (e.g. `my-group/relay-config`) |
| `RELAY_GITLAB_SECRET_NAME` | Secrets Manager secret for the GitLab token (default: `relay/gitlab-token`) |
| `RELAY_SERVICENOW_INSTANCE` | ServiceNow hostname — enables the adapter |
| `RELAY_ENABLE_DIRECT_SMS` | `true` to grant direct-to-phone SMS (opt-in) |

### AI augmentation (all optional)

| Variable | Description |
|---|---|
| `RELAY_AI_ENABLED` | `true` to enable AI incident briefings/AARs |
| `RELAY_AI_PROVIDER` | `bedrock` (default, in-AWS) \| `bedrock-converse` \| `openai` (OpenAI-compatible) |
| `RELAY_AI_MODEL_ID` | Model ID passed to the provider |
| `RELAY_AI_BASE_URL` | Base URL for OpenAI-compatible providers |
| `RELAY_AI_API_KEY_SECRET` | Secrets Manager secret **name** holding the provider API key |

---

## Stack outputs

After every deploy, outputs land in `cdk.outputs.json`.

| Stack | Output key | Value |
|---|---|---|
| RelayDataStack | `DataTableName` | DynamoDB table name |
| RelayDataStack | `DataTableArn` | DynamoDB table ARN |
| RelayDataStack | `PagingTopicArn` | SNS paging topic ARN |
| RelayDataStack | `CentralPagingTopicArn` | SNS central paging topic ARN |
| RelayComputeStack | `DashboardUrl` | ALB URL — your dashboard |
| RelayComputeStack | `IngestQueueUrl` | SQS ingest queue URL |
| RelayComputeStack | `ByorTaskRoleInlinePolicy` | (BYOR mode) inline policy JSON for the task role |
| RelayComputeStack | `ByorExecutionRoleInlinePolicy` | (BYOR mode) inline policy JSON for the execution role |
| RelayComputeStack | `ByorEcsRoleTrust` | (BYOR mode) trust policy JSON both roles need |
| RelayFederationStack | `EventBusArn` | EventBridge bus ARN — pass to team deploys as `RELAY_UPSTREAM_HUB_BUS_ARN` |

---

## CI / GitLab pipeline

`.gitlab-ci.yml` calls the same `scripts/relay-*.sh` scripts from an in-account GitLab
runner. AWS credentials come from the runner's IAM instance role — no access keys stored
in CI variables. The runner role needs the permissions documented in
[infra/RUNNER_IAM.md](https://github.com/Westport-Partners/relay/blob/main/infra/RUNNER_IAM.md) (one inline policy, attached once by an
account admin before the first deploy).

The `deploy_type` pipeline input maps directly to `RELAY_DEPLOY_TYPE`. All deploy logic
lives in the scripts; the pipeline is a thin caller.

---

## Pause and resume

Scale the Fargate service to zero to eliminate compute cost overnight without losing your
table, topics, ALB, or DNS:

```bash
./scripts/relay-down.sh   # scale to 0 — stops Fargate tasks; keeps everything else
./scripts/relay-up.sh     # scale back to 2 (default)
```

Neither script touches IAM, VPC, the data plane, or any stack resource.

---

## Useful CDK commands

```bash
# List all stacks CDK sees in the current context
cdk ls

# Diff compute stack before deploying (supply the same context you'd deploy with)
cdk diff RelayComputeStack \
  -c relay:role=team \
  -c relay:team_name=<team> \
  -c relay:hub_image_uri=<uri>

# Deploy data + compute directly (same flags as relay-deploy.sh uses internally)
cdk deploy RelayDataStack RelayComputeStack \
  --exclusively \
  --require-approval never

# Destroy compute stack (data stack is RETAIN — you must destroy it explicitly if intended)
cdk destroy RelayComputeStack
```
