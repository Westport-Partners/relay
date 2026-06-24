# Relay — Local Development Guide

This guide covers the inner dev loop: running Relay fully offline against
DynamoDB-Local, firing test alarms, and watching the dashboard respond — with
no AWS account and no credentials. It also covers running against a real sandbox
table and executing the test suite.

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
| `RELAY_UI_AUTH_MODE` | `dev` (user: `operator`) |

---

## Run against a real table (local-aws mode)

Use this when you want to test against a real DynamoDB table in a sandbox account
from your laptop or EC2 dev box. AWS credentials come from the instance role or
mounted `~/.aws` credentials — no dummy keys.

```bash
docker build -t relay-hub:dev .

docker run -d --name relay -p 8080:8080 \
  -e RELAY_RUNTIME=local-aws \
  -e RELAY_ALLOW_INGEST=true \
  -e LOG_LEVEL=INFO \
  -e AWS_REGION=us-east-1 \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e RELAY_TABLE_NAME=<your-table> \
  -e RELAY_FLEET_TABLE_NAME=<your-table> \
  -e RELAY_CONFIG_SOURCE=local \
  -e RELAY_CONFIG_DIR=/app/config \
  -e RELAY_UI_AUTH_MODE=dev \
  -e RELAY_UI_DEV_USER=you \
  relay-hub:dev

docker logs -f relay
```

To exercise real SNS paging, add:

```bash
  -e RELAY_SNS_TOPIC_ARN=<topic-arn>
```

The SQS consumer does not run in `local-aws` mode; inject alarms directly via
`relay-fire.sh` or any HTTP client that can reach port 8080.

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
