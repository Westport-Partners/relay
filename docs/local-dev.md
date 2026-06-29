# Relay — Local Development Guide

This guide covers the inner dev loop: running Relay fully offline against
DynamoDB-Local, firing test alarms, and watching the dashboard respond — with
no AWS account and no credentials. It also covers the **evaluation path** —
provisioning real AWS resources and running the Hub locally on an EC2 instance
(released container or plain Python, no ECS) — and executing the test suite.

---

## Run modes

Set the `RELAY_RUNTIME` environment variable to select a mode:

| Mode | Value | DynamoDB | SQS consumer | POST /ingest/alarm | Use when |
|------|-------|----------|--------------|--------------------|----------|
| Production | `fargate` (default) | Real AWS | Running | Blocked (unless `RELAY_ALLOW_INGEST=true`) | Deployed in ECS Fargate |
| Real sandbox | `local-aws` | Real AWS | Off | Open | Laptop/EC2 talking to a real sandbox table |
| Fully offline | `local-mock` | DynamoDB-Local | Off | Open | Zero-AWS inner loop on any machine |

The seam that makes offline work: when `RELAY_AWS_ENDPOINT_URL` is set, every
DynamoDB client routes to that endpoint. No code branches — the same code runs in
all three modes.

---

## Fully-offline harness (docker compose)

Brings up DynamoDB-Local, a one-shot bootstrap container (creates the table, GSI,
and seeds demo contacts), and the Relay container — all against a local in-memory
database. No AWS account. No credentials.

**Start the stack:**

```bash
docker compose up --build
```

This starts three services:
- `dynamodb` — DynamoDB-Local on port 8000, in-memory
- `bootstrap` — one-shot that creates the table + GSI and seeds demo contacts, then exits
- `relay` — the Relay container on port 8080, `RELAY_RUNTIME=local-mock`

**Fire a test alarm:**

```bash
./scripts/relay-fire.sh
```

This POSTs `fixtures/alarms/lambda-error.json` to `POST /ingest/alarm`, then
prints the `/fleet` and `/incidents` responses so you can confirm the alarm was
ingested.

**Open the dashboard:**

```bash
open http://localhost:8080/
```

The tile for the fired alarm turns red. SNS paging is a no-op in this mode (no
topic ARN configured); the escalation leg logs a harmless isolated failure.

### relay-fire.sh usage

```
./scripts/relay-fire.sh [fixture.json] [base_url]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `fixture.json` | `fixtures/alarms/lambda-error.json` | Path to the alarm fixture to POST |
| `base_url` | `http://localhost:8080` | Base URL of the running Relay container |

**Available fixtures:**

| File | Scenario |
|------|----------|
| `fixtures/alarms/lambda-error.json` | Lambda function error alarm |
| `fixtures/alarms/canary-failure.json` | Synthetic canary failure alarm |

Both fixtures use real `CloudWatch Alarm State Change` event shapes with
object-form metric dimensions — the same shape CloudWatch sends in production.

**Example — fire the canary fixture against a remote host:**

```bash
./scripts/relay-fire.sh fixtures/alarms/canary-failure.json http://10.0.1.55:8080
```

### What the compose stack sets

| Variable | Value |
|----------|-------|
| `RELAY_RUNTIME` | `local-mock` |
| `RELAY_ALLOW_INGEST` | `true` |
| `RELAY_AWS_ENDPOINT_URL` | `http://dynamodb:8000` |
| `RELAY_TABLE_NAME` | `relay-local` |
| `RELAY_FLEET_TABLE_NAME` | `relay-local` |
| `AWS_REGION` | `us-east-1` |
| `RELAY_CONFIG_SOURCE` | `local` |
| `RELAY_AUTH_MODE` | `dev` (user: `operator`) |

---

## Self-populating demo (`RELAY_DEMO=true`)

The bare stack above starts an **empty** Hub — no apps, no people, no incidents.
To see what a real, populated deployment looks like with a single command, set
`RELAY_DEMO=true`:

```bash
RELAY_DEMO=true docker compose up
open http://localhost:8080/    # a full agency big-board, filling in live
```

This runs the **test-environment harness** (`tools/testenv/`) against the Hub as
it comes up. It generates a deterministic fake "government agency" and drives the
Hub's HTTP API to populate it:

- **~39 deployment tiles** across four product lines — Primary Product Line,
  Secondary Product Line, Infrastructure, Administrative — in prod / test / dev.
- **25 contacts** with on-call availability, and an auto-generated weekly
  schedule (with a couple of deliberate coverage gaps to show gap-highlighting).
- A few **routing + ignore rules** demonstrating mission-vs-back-office handling.
- A stream of **fake incidents** so the board visibly evolves.

The org is generated with [Faker](https://faker.readthedocs.io/) under a fixed
seed, so the same world regenerates identically every run. It models a real
agency's structure but names no real agency. Phone numbers use the reserved
`+1-555-0100xxx` test range (never real, dialable numbers).

### Demo knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_DEMO` | `false` | `true` enables the self-populating harness on startup |
| `RELAY_DEMO_MODE` | `drip` | `drip` keeps the board live + trickles incidents; `once` seeds + a single incident burst, then stops |
| `RELAY_DEMO_INTERVAL` | `20` | Seconds between drip incidents |
| `RELAY_DEMO_SEED` | `42` | World-generation seed |

### Running the harness by hand

The harness also runs standalone against any reachable Hub (e.g. a container you
started separately), which is useful when iterating on the scenarios:

```bash
pip install -e ".[demo]"     # faker + httpx (included in the [dev] extra too)
python tools/testenv/harness.py --base-url http://localhost:8080
python tools/testenv/harness.py --once       # seed + one burst, then exit
python tools/testenv/world.py --emit summary  # preview the generated world
```

Demo writes require the Hub to be in `dev` (or `alb`) auth mode; the compose
stack sets `RELAY_AUTH_MODE=dev`, and `RELAY_DEMO=true` forces it on if unset.

---

## Run on EC2 against real AWS (the evaluation path)

This is the **recommended way to evaluate Relay** before committing to a full ECS
deploy: stand up the few stateful AWS resources Relay needs, then run the Hub
locally on an EC2 instance against them. It needs **no ECS, no VPC, no IAM role
creation, and no `iam:PassRole`** — so it works in locked-down accounts where the
full CDK deploy cannot. Progression:

```
Phase 1 — provision the data plane + alarm ingest (one script)
Phase 2 — run the Hub locally (released container OR plain Python)
Phase 3 — (later) build an image + deploy RelayComputeStack on ECS  → docs/deploy.md
```

AWS credentials come from the EC2 instance role automatically — no access keys.
(Run `./scripts/relay-preflight.sh` first; if it warns that `AWS_PROFILE` is set,
that profile overrides the instance role — `unset AWS_PROFILE` to use the role.)

### Phase 1 — provision the data plane

`scripts/relay-provision-cli.sh` creates the DynamoDB table (+ GSIs, PITR, TTL,
stream), the SNS paging topics, and the alarm ingest path (SQS + DLQ + an
EventBridge rule that routes CloudWatch `ALARM` state changes into the queue) —
all with plain AWS CLI calls, no CDK or CloudFormation:

```bash
RELAY_TEAM_NAME=<team> AWS_REGION=us-east-1 ./scripts/relay-provision-cli.sh
```

On success it prints the exact `export` lines for the next step. (To provision via
CDK instead in an account that denies `iam:PassRole`, see
[deploy.md → Locked-down accounts](deploy.md#locked-down-accounts-iampassrole-denied).
For a native Terraform equivalent of this data plane, see
[deploy.md → Terraform / Terragrunt path](deploy.md#terraform--terragrunt-path-native-no-cdk).)

> **Enterprise accounts — central security alarm noise.** If your AWS account is
> governed by a central security team, you may see a flood of incidents within
> minutes of install from alarms like `IAMPolicyChanges`, `UnauthorizedAPICalls`,
> or `RootAccountUsage`. These are CloudTrail metric alarms installed account-wide
> by a CloudFormation **StackSet** (CSPM / Security Hub / CIS tooling) — they fire
> on routine admin activity, including the activity of installing Relay. They are
> working as designed and are **not** application incidents. Suppress them with an
> ignore rule scoped to the StackSet's alarm-name prefix (find it on the
> Maintenance → Ignore Rules screen, or in `config/routing.yaml` under `ignore:` —
> see the commented CSPM block in `config/routing.example.yaml`). `relay-preflight.sh`
> scans for `StackSet-`-prefixed alarms and warns you if any are present.

### Phase 2, on-ramp A — run the released container (lowest friction)

The Hub image is published to `ghcr.io/westport-partners/relay`. Pull it and run
it against the resources from Phase 1 — no build step:

```bash
docker run -d --name relay -p 8080:8080 \
  -e RELAY_RUNTIME=local-aws \
  -e RELAY_ALLOW_INGEST=true \
  -e AWS_REGION=us-east-1 \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e RELAY_TABLE_NAME=relay-<team> \
  -e RELAY_FLEET_TABLE_NAME=relay-<team> \
  -e RELAY_SQS_QUEUE_URL=<queue-url from Phase 1> \
  -e RELAY_SNS_TOPIC_ARN=<paging-topic-arn from Phase 1> \
  -e RELAY_CONFIG_SOURCE=local \
  -e RELAY_AUTH_MODE=dev \
  -e RELAY_DEV_USER=you \
  ghcr.io/westport-partners/relay:latest

docker logs -f relay
open http://localhost:8080/
```

### Phase 2, on-ramp B — run as a plain Python process (no Docker)

If Docker isn't available (or you want to iterate on the code), run the Hub
directly. `pip install` exposes the `relay-hub` console entrypoint, which serves
the dashboard on port 8080:

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # see note below
pip install -e ".[serve]"

export RELAY_RUNTIME=local-aws
export RELAY_ALLOW_INGEST=true
export AWS_REGION=us-east-1
export RELAY_TABLE_NAME=relay-<team>
export RELAY_FLEET_TABLE_NAME=relay-<team>
export RELAY_SQS_QUEUE_URL=<queue-url from Phase 1>
export RELAY_SNS_TOPIC_ARN=<paging-topic-arn from Phase 1>
export RELAY_CONFIG_SOURCE=local
export RELAY_AUTH_MODE=dev RELAY_DEV_USER=you

relay-hub      # serves http://0.0.0.0:8080
```

> **Amazon Linux 2023:** the system `python3` is 3.9, but Relay needs 3.12+. Install
> it with `sudo dnf install -y python3.12` and create the venv with the **versioned**
> binary (`python3.12 -m venv .venv`) — installing 3.12 does not repoint `python3`.
> `relay-preflight.sh` detects this and tells you which binary to use.

### Verifying ingestion

In `local-aws` mode the SQS consumer does not run, so the EventBridge → SQS path
buffers alarms but the Hub does not drain the queue automatically. To confirm the
pipeline end-to-end, inject an alarm over HTTP (`RELAY_ALLOW_INGEST=true` opens
`POST /ingest/alarm`):

```bash
./scripts/relay-fire.sh                       # localhost:8080
./scripts/relay-fire.sh fixtures/alarms/canary-failure.json http://<ec2-host>:8080
```

The matching tile turns red and an incident appears on `/incidents`. Real SNS
paging fires when `RELAY_SNS_TOPIC_ARN` is set (as above).

### Tearing down / starting over

`relay-down.sh` only scales the ECS service to zero — it deliberately leaves the
data plane intact. To remove the resources `relay-provision-cli.sh` created (clean
slate after a test, or to re-provision from scratch), run **`relay-teardown-cli.sh`**
with the same `RELAY_TEAM_NAME`:

```bash
RELAY_TEAM_NAME=<team> AWS_REGION=us-east-1 ./scripts/relay-teardown-cli.sh
```

It deletes in dependency-safe order — EventBridge rule → SQS ingest + DLQ → SNS
paging topics → DynamoDB table last. The DynamoDB table holds durable
incident/contact/schedule data, so the script prompts for confirmation before
deleting it (set `RELAY_FORCE=1` to skip the prompt in automation).

### Transitioning from the CLI provisioner to the CDK deploy

The CLI provisioner and `RelayDataStack` create resources with the **same names**
(`relay-<team>` table, paging topics, ingest queue, alarm rule). `RelayDataStack`
**creates** these resources — it does not adopt or import pre-existing ones. So if
you provisioned via the CLI and then deploy `RelayDataStack` on top, CloudFormation
fails with:

```
Resource already exists: relay-<team>
```

Two supported paths, depending on whether you want to keep the evaluation data:

- **Discard and redeploy (clean cutover):** run `relay-teardown-cli.sh` to remove
  the CLI resources, then deploy `RelayDataStack`. The CLI path is for evaluation —
  if you have no data worth keeping, this is the simplest transition.
- **Keep the data:** the DynamoDB table is `RemovalPolicy.RETAIN`, so you can leave
  the CLI-provisioned table in place and run only `RelayComputeStack` against it
  (point it at the existing table name). Do **not** deploy `RelayDataStack` over the
  existing resources — it will fail on the name clash.

> CloudFormation resource import (`cdk import`) of the existing table into
> `RelayDataStack` is possible but not currently scripted; treat it as an advanced,
> manual operation.

---

## Running the tests

Install the dev dependencies, then run pytest:

```bash
pip install -e ".[dev]"
pytest -q
```

Requires Python 3.12+. Tests run entirely offline (no AWS calls).

---

## Full environment variable reference

The tables above show only the variables needed for local dev. For the complete
reference — all `RELAY_*` variables, their defaults, and valid values — see
[configure.md](configure.md).
