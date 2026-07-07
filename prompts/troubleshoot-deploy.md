# Relay — Troubleshoot Deploy Prompt

You are helping the user diagnose a failed or misbehaving Relay deploy. Work **read-only first** — gather facts before suggesting any changes. The goal is to identify the root cause and the minimum fix, not to re-deploy blindly.

Canonical references: [`docs/deploy.md`](../docs/deploy.md), [`docs/byor.md`](../docs/byor.md).

---

## Gather state first

Before any diagnosis, ask the user (or run):

```bash
# Which stacks exist and what are their statuses?
aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName,'Relay')].[StackName,StackStatus,StatusReason]" \
  --output table

# What outputs were written (if any stacks completed)?
cat cdk.outputs.json 2>/dev/null || echo "no outputs yet"
```

---

## Diagnose by symptom

### Symptom: `cdk deploy` fails with `iam:PassRole` denied

```
User: ... is not authorized to perform: iam:PassRole on resource:
arn:aws:iam::<account>:role/cdk-hnb659fds-cfn-exec-role-...
```

**Cause:** The deploy principal's policy explicitly denies `iam:PassRole`. `cdk deploy` passes the CDK bootstrap execution role to CloudFormation — this fails in regulated accounts.

**Fix:** Switch to `relay-deploy-direct.sh`, which synthesizes templates locally then uses `aws cloudformation deploy` (no `PassRole` required):

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_STACK_SELECTOR=data \
./scripts/relay-deploy-direct.sh
```

For the compute stack, also supply BYOR context keys if `iam:CreateRole` is denied. See [`prompts/deploy-byor.md`](deploy-byor.md).

---

### Symptom: `RelayComputeStack` synth fails — "image URI is a placeholder"

```
ValueError: RELAY_HUB_IMAGE_URI is unset or is a placeholder ...
```

**Cause:** `RELAY_HUB_IMAGE_URI` was not set before running synth/deploy.

**Fix:**

```bash
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"
echo "$RELAY_HUB_IMAGE_URI"   # confirm it looks like a real ECR URI
```

Then re-run synth and deploy.

---

### Symptom: `relay-build-hub-image.sh` fails during `RUN apt-get`/`pip install` with DNS errors

```
Temporary failure resolving 'deb.debian.org'
# or: pip ... Could not find a version / connection timed out
```

**Cause:** Docker's default bridge network can't resolve DNS or reach the
internet during the image build. Common on WSL2, VPNs, and locked-down corporate
networks, where the bridge network doesn't inherit the host's working DNS/egress.

**Fix:** build against the host network stack via the `DOCKER_BUILD_NETWORK` env
var (leave it unset on Docker Desktop / Mac / Windows, where host networking for
builds is unsupported — the default bridge is correct there):

```bash
DOCKER_BUILD_NETWORK=host \
export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"
```

---

### Symptom: Stack is stuck in `CREATE_IN_PROGRESS` or `UPDATE_IN_PROGRESS`

```bash
# Stream CloudFormation events for the stuck stack
aws cloudformation describe-stack-events \
  --stack-name RelayComputeStack \
  --query "StackEvents[*].[Timestamp,ResourceStatus,ResourceType,ResourceStatusReason]" \
  --output table | head -40
```

Look for `CREATE_FAILED` or `UPDATE_FAILED` resource entries — the `ResourceStatusReason` column contains the actual error. Common causes:

- **ECS service can't place tasks** → subnet/security-group misconfiguration, or no Fargate capacity in the AZ. Check the `ResourceStatusReason` for "unable to place tasks".
- **ECR pull fails** → the execution role lacks `ecr:GetAuthorizationToken` / `ecr:BatchGetImage`, or the image URI doesn't exist. Check BYOR execution-role inline policy.
- **ALB target group health check fails** → the container port or health-check path is wrong. Review the task definition.

---

### Symptom: CDK bootstrap missing or outdated

```
Error: This stack uses assets, so the toolkit stack must be deployed...
```

**Fix (standard account):**

```bash
AWS_REGION=us-east-1 ./scripts/relay-bootstrap.sh
```

**In accounts where `iam:PassRole` is also denied:** Use `relay-deploy-direct.sh`. The direct-deploy path does not require the bootstrap stack — see the `iam:PassRole` symptom above.

> If the bootstrap is pinned at an older version by a platform team and you cannot update it, the CDK "outdated bootstrap version" warning during `synth` is safe to ignore for the direct-deploy path.

---

### Symptom: Dashboard is unreachable (connection timeout)

**Check 1 — ALB scheme:**

```bash
jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json
```

If the URL has no hostname or is blank, the stack output wasn't written (deploy may have partially failed).

If the URL is present but unreachable: the ALB is likely internal (private subnets only) and the caller is not inside the VPC or behind a VPN.

**Fix:** Redeploy with `RELAY_INTERNAL_ALB=false` (public ALB) if the account has no VPN/peering into the VPC. Note: ALB scheme changes cannot update in place — the compute stack must be destroyed and recreated.

**Check 2 — ECS service health:**

```bash
# Find the service name (cluster is usually relay-<team>)
aws ecs list-services --cluster relay-<team> --output text

# Check service state
aws ecs describe-services \
  --cluster relay-<team> \
  --services relay-hub \
  --query "services[*].{running:runningCount,desired:desiredCount,status:status,events:events[0:3]}" \
  --output json
```

If `runningCount < desiredCount`, check ECS service events and stopped tasks (see next symptom).

---

### Symptom: ECS tasks are stopping immediately / restart loop

```bash
# List recently stopped tasks
aws ecs list-tasks \
  --cluster relay-<team> \
  --desired-status STOPPED \
  --query taskArns --output text

# Describe the first stopped task (replace ARN)
aws ecs describe-tasks \
  --cluster relay-<team> \
  --tasks <task-arn> \
  --query "tasks[*].{stoppedReason:stoppedReason,containers:containers[*].{name:name,exitCode:exitCode,reason:reason}}" \
  --output json
```

Common `stoppedReason` causes:

| stoppedReason | Likely cause | Fix |
|---|---|---|
| `CannotPullContainerError` | ECR auth, bad image URI, or execution role missing ECR permissions | Check `RELAY_HUB_IMAGE_URI` and execution role inline policy |
| `ResourceInitializationError` / secret fetch failed | A Secrets Manager secret referenced by the task definition doesn't exist or the task role lacks `secretsmanager:GetSecretValue` | Check BYOR task-role inline policy |
| Exit code 1, no reason | Container crashed at startup | Check CloudWatch Logs |
| Exit code 137 | OOM kill | Increase task memory; check for memory leak |

---

### Symptom: Container is running but not processing alarms / DLQ growing

```bash
# Check the DLQ depth (messages that failed ingestion)
aws sqs get-queue-attributes \
  --queue-url "$(jq -r '.RelayComputeStack.IngestQueueUrl' cdk.outputs.json | sed 's/ingest/ingest-dlq/')" \
  --attribute-names ApproximateNumberOfMessages \
  --query Attributes.ApproximateNumberOfMessages
```

If the DLQ has messages, the container is receiving events but failing to process them. Check the container logs:

```bash
# Find the log group (usually /ecs/relay-<team>)
aws logs describe-log-groups \
  --log-group-name-prefix /ecs/relay \
  --query "logGroups[*].logGroupName" --output text

# Tail recent logs
aws logs filter-log-events \
  --log-group-name /ecs/relay-<team> \
  --start-time $(($(date +%s) - 600))000 \
  --filter-pattern "ERROR"
```

---

### Symptom: `iam:CreateRole` denied during compute stack deploy

The stack is trying to create the ECS task and execution roles, but the account prohibits it.

**Fix:** Supply pre-provisioned role ARNs as CDK context keys. Follow [`prompts/deploy-byor.md`](deploy-byor.md) for the full workflow.

---

## General diagnostic sequence

1. **Hit the deep readiness endpoint first** — it surfaces IAM and dependency misconfigs in one call:

   ```bash
   # Replace <DASHBOARD_URL> with the DashboardUrl stack output.
   curl -s <DASHBOARD_URL>/health/ready | jq .
   ```

   A `"status": "degraded"` result names the failing check and the AWS error code directly, which usually identifies the root cause without any log diving. See [`docs/byor.md` → Verifying a BYOR deployment](../docs/byor.md#verifying-a-byor-deployment) for the full failure table.

2. Check CloudFormation stack events for `*_FAILED` resources.
3. Check ECS service events and stopped-task `stoppedReason`.
4. Check CloudWatch Logs for container errors.
5. Check the DLQ depth for ingestion failures.
6. Confirm `RELAY_HUB_IMAGE_URI` is a real, pushable ECR URI.
7. Confirm ALB scheme matches the network topology.

Do not re-deploy until you understand the cause — blind re-deploys on a stuck stack often make the CloudFormation state worse.
