# Relay — Deploy Team Topology Prompt

You are helping the user deploy Relay for a single team. This is the default topology: one always-on ECS Fargate container plus one DynamoDB table in the team's own AWS account. Detection, paging, escalation, and the dashboard all run there.

Canonical reference: [`docs/deploy.md`](../docs/deploy.md).

---

## Goal

Complete a fresh team deploy and verify the dashboard is reachable at the `DashboardUrl` stack output.

## Preconditions

- Preflight passes: `./scripts/relay-preflight.sh` exits 0. If it warned about `iam:CreateRole` or `ec2:CreateVpc` being denied, stop here and follow [`prompts/deploy-byor.md`](deploy-byor.md) instead.
- AWS credentials are configured for the target account.
- Docker daemon is running.
- Python 3.12+ and Node.js 20+ are installed (CDK synth runs `npx aws-cdk@2`, which no longer supports the end-of-life Node 18; 22 is recommended). On Amazon Linux 2023, `dnf install nodejs` installs EOL Node 18 — use `dnf install nodejs22`.
- Python CDK deps are installed in a venv: `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[infra]'`. The deploy scripts auto-activate `.venv/` if it exists; without `aws-cdk-lib` + `constructs` (the `[infra]` extra) the synth fails with `ModuleNotFoundError: aws_cdk`.

---

## What gets created

Relay deploys as **three independently deployable stacks**. For the team topology, two stacks are deployed:

| Stack | What it owns | Deploy cadence |
|---|---|---|
| **RelayDataStack** | DynamoDB table (`relay-<team>`) + GSI + SNS paging topics | Once; RETAIN on delete |
| **RelayComputeStack** | VPC, ECS cluster, Fargate service, ALB, EventBridge rule → SQS ingest + DLQ, task role + execution role | Every image change |

---

## Step 1 — Bootstrap CDK (once per account/region)

```bash
AWS_REGION=us-east-1 ./scripts/relay-bootstrap.sh
```

Creates the `CDKToolkit` stack if absent. Idempotent — safe to re-run. Skip if preflight already showed `CDKToolkit` present.

---

## Step 2 — Build and push the container image

```bash
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"
```

This script:
1. Builds the Docker image from the local repo.
2. Creates the `relay-hub` ECR repository if it does not exist.
3. Authenticates Docker to ECR.
4. Pushes the image and prints the fully-qualified URI on the last line.

The image tag defaults to the git short SHA. Override with `IMAGE_TAG=<tag>`.

To bake in custom config files instead of the in-repo defaults, set `RELAY_CONFIG_DIR` to the directory holding your `*.yaml` files before running the script. The originals are restored automatically after the build.

> **Important:** `RelayComputeStack` fails fast at synth if `RELAY_HUB_IMAGE_URI` is unset or contains `amazonlinux`/`PLACEHOLDER`. Always complete this step before synthesizing.

Verify the variable is set:

```bash
echo "$RELAY_HUB_IMAGE_URI"
# expected: something like 123456789012.dkr.ecr.us-east-1.amazonaws.com/relay-hub:abc1234
```

---

## Step 3 — Synthesize and review (no AWS writes)

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-synth.sh
```

CloudFormation templates land in `cdk.out/`. Review them before deploying — no AWS writes occur at this step.

---

## Step 4 — Deploy

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

The script deploys data first, then compute. Outputs land in `cdk.outputs.json`.

**Key environment variables:**

| Variable | Required | Description |
|---|---|---|
| `RELAY_DEPLOY_TYPE` | yes | `team` selects the team topology |
| `RELAY_TEAM_NAME` | yes | Team identifier; names the DynamoDB table `relay-<team>` |
| `RELAY_HUB_IMAGE_URI` | yes | ECR image URI from Step 2 |
| `AWS_REGION` | no | Target region (default `us-east-1`) |
| `RELAY_INTERNAL_ALB` | no | `true` (default) = internal ALB; set `false` if no VPN/peering into the VPC |
| `RELAY_TZ` | no | IANA timezone for schedule resolution (e.g. `America/New_York`) |

> **ALB reachability:** The ALB is internal by default. If the account has no VPN or VPC peering, set `RELAY_INTERNAL_ALB=false` or the dashboard will be unreachable from outside the VPC.

---

## Step 5 — Verify

```bash
# Get the dashboard URL
jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json

# Confirm the dashboard answers 200
curl -s -o /dev/null -w '%{http_code}\n' "$(jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json)"
# expected: 200

# Confirm both stacks completed
aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName,'Relay')].[StackName,StackStatus]" \
  --output text
# expected: RelayDataStack CREATE_COMPLETE, RelayComputeStack CREATE_COMPLETE
```

---

## Scoped deploys — the image-update inner loop

After the first deploy, you only need to redeploy the compute stack when the image changes. Use `RELAY_STACK_SELECTOR=compute` so the data plane is never touched:

```bash
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"

RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
RELAY_STACK_SELECTOR=compute \
./scripts/relay-deploy.sh
```

`RELAY_STACK_SELECTOR` values: `data` | `compute` | `federation` | unset/`all`.

---

## Pause and resume

Scale the Fargate service to zero to save cost without losing the table, topics, ALB, or DNS:

```bash
./scripts/relay-down.sh   # scale to 0
./scripts/relay-up.sh     # scale back to 2
```

Neither script touches the data plane or any stack resource.

---

## Next steps

- Edit config and contacts → [`prompts/configure.md`](configure.md)
- Wire a federated hub → [`prompts/deploy-federated-hub.md`](deploy-federated-hub.md)
- Diagnose a failed deploy → [`prompts/troubleshoot-deploy.md`](troubleshoot-deploy.md)
