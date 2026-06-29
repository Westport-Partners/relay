# Relay IaC — CDK Stacks

AWS CDK (Python, v2) infrastructure for Relay. Relay deploys as **independent, separately-deployable stacks**:

| Stack | What it owns | Deploy cadence |
|---|---|---|
| **RelayDataStack** | DynamoDB table (+ `incident-status-index` GSI, stream) + paging SNS topics | Once; RETAIN |
| **RelayComputeStack** | VPC (or BYOV), ECS cluster, always-on Fargate service + ALB, CloudWatch-alarm EventBridge rule → SQS ingress + DLQ (+ DLQ-depth alarm → paging topic), task + exec IAM roles | Every image change |
| **RelayFederationStack** | (federated-hub only) the `relay-hub` EventBridge bus + resource policy + ingest rule | Rarely |

There are two **topologies**, selected by `relay:role`:

| Topology | `relay:role` | Stacks |
|---|---|---|
| **team** (default) | `team` | RelayDataStack + RelayComputeStack |
| **federated-hub** | `federated-hub` | RelayDataStack + RelayComputeStack + RelayFederationStack |

In the **team** topology one always-on container runs detection **in-process** and
serves the dashboard, against one DynamoDB table (`relay-{team}`). A CloudWatch alarm
flows: EventBridge rule → SQS → container's in-process `DetectionPipeline` → page +
tile + lifecycle. Escalation timers are DynamoDB deadlines swept by the container.

In the **federated-hub** topology the same container runs as the org-wide aggregator
and RelayFederationStack adds the bus that team containers forward SEV1/2 up to
(`relay:org_id` scopes the org-wide `PutEvents` grant).

## Deploying Relay

### Primary path: portable scripts in `scripts/`

```bash
bash scripts/relay-bootstrap.sh        # CDK bootstrap (first time only)
bash scripts/relay-synth.sh            # synth + review templates (no AWS writes)
bash scripts/relay-deploy.sh           # deploy the topology (data first, then compute)
```

A real container image URI is **required** for a compute deploy — build + push first:

```bash
bash scripts/relay-build-hub-image.sh  # builds + pushes; exports RELAY_HUB_IMAGE_URI
```

`RelayComputeStack` **fails fast at synth** if `relay:hub_image_uri` is unset or a
placeholder, so it can never silently synth the old amazonlinux placeholder.

#### Independent / scoped deploys

The whole point of the split is that you can deploy one plane at a time:

```bash
RELAY_STACK_SELECTOR=data    bash scripts/relay-deploy.sh   # just the data plane (once)
RELAY_STACK_SELECTOR=compute bash scripts/relay-deploy.sh   # just the container (inner loop)
```

Deploys use `--exclusively`, so a `compute` deploy can never touch the data plane.

### CI option: GitLab pipeline

`.gitlab-ci.yml` calls the same `scripts/relay-*.sh` from an in-account GitLab runner
(AWS creds from the runner's instance role). The runner's role needs the resource-create
permissions in **[RUNNER_IAM.md](./RUNNER_IAM.md)**.

## Required context values

| Key | Topology | Example | Description |
|-----|----------|---------|-------------|
| `relay:role` | both | `team` | `team` (default) or `federated-hub` |
| `relay:hub_image_uri` | both | `…dkr.ecr….amazonaws.com/relay-hub:sha` | **Required.** Real ECR image; fail-fast on placeholder. |
| `relay:team_name` | team | `payments-api` | Unique identifier in resource names (`relay-{team}`) |
| `relay:org_id` | federated-hub | `o-xxxxxxxxxxxx` | Org id for the org-wide `PutEvents` bus policy |

## Optional context values

| Key | Default | Description |
|-----|---------|-------------|
| `relay:hub_scope` | `local` (team) / `central` (fed) | `local` \| `local-federated` (also forward SEV1/2 up) \| `central` |
| `relay:central_hub_bus_arn` | — | Required when `hub_scope=local-federated` — the federated bus ARN |
| `relay:servicenow_instance` | — | ServiceNow hostname (enables the adapter) |
| `relay:enable_integrations` | `false` | Wire GitLab/ServiceNow secrets into the task |
| `relay:gitlab_secret_name` | `relay/gitlab-token` | Secrets Manager secret name for the GitLab token |
| `relay:ai_enabled` / `relay:ai_provider` / `relay:ai_model_id` / `relay:ai_base_url` / `relay:ai_api_key_secret` | off | AI augmentation (Bedrock default; OpenAI-compatible needs base URL + key) |
| `relay:enable_direct_sms` | `false` | Grant `sns:Publish` for direct-to-phone SMS (broad; opt-in) |
| `relay:auth_mode` / `relay:dev_user` | `none` | UI auth: `none` (read-only) \| `alb` \| `dev` |
| `relay:certificate_arn` | — | Bring-your-own ACM cert ARN (HTTPS; see HTTPS section below) |
| `relay:phz_id` | — | Route53 private hosted zone ID for auto-minted cert + DNS record |
| `relay:phz_name` | — | Private hosted zone name, e.g. `corp.example.internal` |
| `relay:alb_subdomain` | `relay` | Left DNS label; dashboard lands at `{subdomain}.{zone}` |
| `relay:access_control` | `false` | Enable per-user access control (`true`/`false`) |
| `relay:auth_allowed_users` | — | Comma-separated usernames allowed when access control is on |
| `relay:config_source` / `relay:tz` / `relay:log_level` | — / `UTC` / `INFO` | Bundled config, on-call timezone, container log level |
| Node self-identity: `relay:app_name` / `relay:deployment_id` / `relay:environment` / `relay:service_path` / `relay:org_path` | team_name / unrouted | Carried on the heartbeat so the tile aligns with the deployment |

## BYOR / BYOV (locked-down accounts)

Pass existing ARNs/IDs and the compute stack imports them instead of creating roles/VPC.
Net IAM surface: **one task role + one exec role**.

| Key | Description |
|-----|-------------|
| `relay:ecs_execution_role_arn` | Existing ECS task execution role |
| `relay:ecs_task_role_arn` | Existing ECS task role |
| `relay:vpc_id` | Existing VPC to import (skips VPC creation) |

In BYOR mode `RelayComputeStack` emits the inline-policy + trust JSON as stack outputs
(`ByorTaskRoleInlinePolicy`, `ByorExecutionRoleInlinePolicy`, `ByorEcsRoleTrust`) for the
team to paste onto the pre-provisioned roles. See **[docs/byor.md](../docs/byor.md)**.

## Local-mock harness (no AWS)

Run the whole stack offline against DynamoDB-Local — see the repo-root
[`docker-compose.yml`](../docker-compose.yml):

```bash
docker compose up --build      # DynamoDB-Local + table bootstrap + container
./scripts/relay-fire.sh        # fire fixtures/alarms/lambda-error.json
open http://localhost:8080/    # watch the tile go red
```

The seam is `RELAY_AWS_ENDPOINT_URL` (`src/relay/adapters/aws/endpoint.py`): when set,
every DynamoDB client routes to the local endpoint. No code branches in the stores.

## Prerequisites

- Python 3.12+, Node.js 18+ (CDK CLI), `npm i -g aws-cdk` (or the bundled `npx`)
- `pip install -e ".[infra]"` in the venv (bundled into `.[dev]`)
- AWS credentials for the target account

## Useful CDK commands

```bash
cdk ls
cdk diff  RelayComputeStack -c relay:role=team -c relay:team_name=t -c relay:hub_image_uri=…
cdk deploy RelayDataStack RelayComputeStack --exclusively --require-approval never
cdk destroy RelayComputeStack          # data stack is RETAIN; destroy it explicitly if needed
```

## Stack outputs

- **RelayDataStack:** `DataTableName`, `DataTableArn`, `PagingTopicArn`, `CentralPagingTopicArn`
- **RelayComputeStack:** `DashboardUrl`, `IngestQueueUrl` (+ BYOR policy JSON in BYOR mode)
- **RelayFederationStack:** `EventBusArn` — hand to team deploys as `relay:central_hub_bus_arn`

## ALB HTTPS by default

The ALB defaults to **HTTPS** whenever a certificate can be obtained.  Supply a
private hosted zone so the dashboard is served at `relay.<zone>` with an
auto-issued ACM cert and an HTTP→HTTPS redirect:

```bash
cdk deploy RelayComputeStack \
  -c relay:hub_image_uri=… \
  -c relay:team_name=payments \
  -c relay:phz_id=Z1234567890ABC \
  -c relay:phz_name=corp.example.internal
# → ALB at https://relay.corp.example.internal/
```

Alternatively, pass `relay:certificate_arn` with a cert you have already issued:

```bash
-c relay:certificate_arn=arn:aws:acm:us-east-1:…:certificate/…
```

Without either context key the ALB falls back to HTTP:80 and CDK emits a synth
warning (`[WARNING] HTTPS is the default but no certificate…`).

> **Private-zone note:** ACM DNS validation writes CNAME records into the hosted
> zone. For a zone not resolvable from the public internet (pure private), ACM
> cannot auto-validate — bring a pre-issued cert via `relay:certificate_arn`.
