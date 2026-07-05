# Relay — Deploy Federated Hub Prompt

You are helping the user deploy a Relay federated Hub — the org-wide aggregator that serves a NOC big-board and receives SEV1/SEV2 escalations forwarded from team deployments. **Read this whole prompt before running any commands.** The hub deploys a cross-account EventBridge bus policy that grants every account in the AWS Organization the right to send events, and that is not something to enable by copy-paste.

Canonical reference: [`docs/deploy.md`](../docs/deploy.md) (federated-hub section).

---

## Goal

Deploy the three stacks in the hub account, verify the big-board answers 200, then wire one or more team deployments to forward escalations up to the hub bus.

## Preconditions

- The hub account is identified and you have AWS credentials for it.
- Preflight passes in the hub account: `./scripts/relay-preflight.sh` exits 0.
- `RELAY_HUB_IMAGE_URI` is set (built with `relay-build-hub-image.sh` — see [`prompts/deploy-team.md`](deploy-team.md) Step 2).
- You have the AWS Organization ID (`o-xxxxxxxxxxxx`) — find it in the Organizations console or with `aws organizations describe-organization --query Organization.Id --output text`.

---

## Choosing the hub account

| Option | Verdict |
|---|---|
| Dedicated shared-services account (under a "Core"/shared-services OU) | **Recommended.** Isolates the org-wide ingress; matches AWS best practice. |
| An existing team's account | Workable for a small org, but couples the org-wide NOC to one team's blast radius. |
| The Organizations management (root) account | **Do not.** An internet-facing workload and org-wide `PutEvents` ingress in your most privileged account is a finding in any security review. |

---

## What gets created in the hub account

Three stacks:

| Stack | Resource | Security note |
|---|---|---|
| **RelayDataStack** | DynamoDB table + SNS paging topics | Same as a team deploy |
| **RelayComputeStack** | VPC, ECS cluster, Fargate service, ALB (the big-board), task + execution roles | Same as a team deploy |
| **RelayFederationStack** | `relay-hub` EventBridge bus + org-scoped resource policy + ingest rule | Cross-account ingress; review the policy |

The bus policy is **scoped to your org ID**, not open to the world:

```json
{
  "Effect": "Allow",
  "Principal": "*",
  "Action": "events:PutEvents",
  "Resource": "arn:aws:events:<region>:<hub-account>:event-bus/relay-hub",
  "Condition": { "StringEquals": { "aws:PrincipalOrgID": "o-xxxxxxxxxxxx" } }
}
```

If you omit `RELAY_ORG_ID`, the policy falls back to same-account-only ingress — no team account can forward up. Always supply it for a real federated deployment.

Trust flows **one way**: team accounts push to the hub bus. The hub holds no credentials for, and makes no calls into, any team account.

---

## Step 1 — Bootstrap CDK in the hub account (once)

```bash
AWS_REGION=us-east-1 ./scripts/relay-bootstrap.sh
```

---

## Step 2 — Deploy the hub

```bash
RELAY_DEPLOY_TYPE=federated-hub \
RELAY_ORG_ID=o-xxxxxxxxxxxx \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

All three stacks deploy. Outputs land in `cdk.outputs.json`.

**Key environment variables for the hub deploy:**

| Variable | Required | Description |
|---|---|---|
| `RELAY_DEPLOY_TYPE` | yes | `federated-hub` |
| `RELAY_ORG_ID` | yes | AWS Organization ID for the bus resource policy |
| `RELAY_HUB_IMAGE_URI` | yes | Container image URI |
| `AWS_REGION` | no | Target region (default `us-east-1`) |
| `RELAY_INTERNAL_ALB` | no | `true` (default); set `false` if no VPN/peering |

---

## Step 3 — Verify the hub

```bash
# All three stacks should report *_COMPLETE
aws cloudformation list-stacks \
  --query "StackSummaries[?starts_with(StackName,'Relay')].[StackName,StackStatus]" \
  --output text

# The big-board should answer 200
curl -s -o /dev/null -w '%{http_code}\n' \
  "$(jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json)"

# Confirm the bus policy is org-scoped (not open to the world)
aws events describe-event-bus --name relay-hub --query Policy --output text
# Verify: aws:PrincipalOrgID condition is present and matches YOUR org ID
```

---

## Step 4 — Wire team deploys to the hub

Take the `EventBusArn` from `cdk.outputs.json`:

```bash
export HUB_BUS_ARN="$(jq -r '.RelayFederationStack.EventBusArn' cdk.outputs.json)"
echo "$HUB_BUS_ARN"
```

In **each team account**, redeploy (or deploy fresh) with `RELAY_HUB_SCOPE=local-federated`:

```bash
RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_SCOPE=local-federated \
RELAY_UPSTREAM_HUB_BUS_ARN=<EventBusArn from above> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
./scripts/relay-deploy.sh
```

With `RELAY_HUB_SCOPE=local-federated`, the team container forwards SEV1/SEV2 escalations to the federated bus. Which incidents forward is governed by the `federation:` block in `config/routing.yaml` — see [`prompts/configure.md`](configure.md) and [`docs/configure.md`](../docs/configure.md).

---

## Security review checklist before going live

Before putting the hub in production, confirm:

- [ ] The hub account is **not** the Organizations management account.
- [ ] `aws events describe-event-bus --name relay-hub --query Policy --output text` shows `aws:PrincipalOrgID` scoped to your org ID.
- [ ] `RELAY_INTERNAL_ALB` is appropriate for the network topology (internal if behind VPN/peering, otherwise external).
- [ ] The deploy principal's role follows the permissions in `infra/RUNNER_IAM.md` — no permanent broad admin access.
- [ ] No integration credentials are stored as env vars or in config files — credentials belong on the Settings screen (DynamoDB), not in Git.

---

## Integrations are optional — no secret prerequisites

A fresh hub deploy requires no integration credentials. GitLab, ServiceNow, and Teams are optional. Configure them at runtime on the **Settings** screen in the dashboard. A missing token never blocks a deploy or a page.

---

## Locked-down hub account

If the hub account denies `iam:CreateRole` or `ec2:CreateVpc`, follow [`prompts/deploy-byor.md`](deploy-byor.md) for the hub deploy as well — the same BYOR/BYOV context keys apply.

---

## Next steps

- Configure the federation gate and escalation policies → [`prompts/configure.md`](configure.md)
- Diagnose hub deploy failures → [`prompts/troubleshoot-deploy.md`](troubleshoot-deploy.md)
