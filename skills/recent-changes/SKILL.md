---
name: recent-changes
description: >
  Read-only; correlates the incident window with recent ECS service deployments,
  CloudFormation stack updates, and CloudTrail mutating events (config changes)
  to find what changed right before the alarm. The single highest-leverage triage
  question: if something broke, a change almost always preceded it.
---

# Recent-changes investigation

The most powerful first question in any incident is "what changed?" — a deploy,
a stack update, or a config mutation in the 30 minutes before the alarm explains
the majority of production incidents. This skill answers that question without
requiring you to remember the CloudTrail/CFN/ECS CLI call chain.

It uses a wider default lookback (24 h) than most probes because deploys and
stack updates that *cause* an alarm often precede it by minutes to hours. A
narrow 1-hour window would miss a slow-burn misconfiguration.

## When to use

- An alarm fired and you suspect "we deployed something" or "someone changed a
  config" as the root cause.
- The `ecs-investigation` skill shows a degraded service and you want to know
  whether a task-def rollout or stack change preceded it.
- You want to rule out "we broke it" before pivoting to external/dependency causes.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | yes | App name; used to scope ECS discovery and CFN stack matching. |
| `RELAY_ECS_CLUSTER` | no | ECS cluster name/ARN. If absent, the probe lists clusters and matches on app name. |
| `RELAY_ECS_SERVICE` | no | ECS service name. If absent, matched within the cluster by app name. |
| `RELAY_CFN_STACK` | no | CloudFormation stack name to inspect. If absent, the probe lists stacks and matches on app name. |
| `RELAY_WINDOW_MINUTES` | no | Lookback window in minutes. **Defaults to 1440 (24 h)** — wider than the standard 60-minute probe default because deploy-related changes often precede the alarm by tens of minutes to hours. Set to a smaller value (e.g. `120`) for a tighter window once you have narrowed the timeline. |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... [RELAY_ECS_CLUSTER=...] [RELAY_CFN_STACK=...] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — what app, cluster/service, stack, and window the probe is
   using, and how it discovered optional inputs.
2. **ECS deployments** — `ecs describe-services` deployments with `createdAt`,
   `updatedAt`, `rolloutState`, and task-def revision. Shows the timeline of
   recent task-def rollouts. Skipped cleanly when the app is not on ECS.
3. **CloudFormation recent activity** — stack events in the window filtered to
   `UPDATE_*` / `CREATE_*` / `DELETE_*` resource statuses. Also surfaces
   recently-updated stacks by `LastUpdatedTime` so you can spot a sibling stack
   change even if the primary stack matches fine.
4. **CloudTrail mutating events** — `cloudtrail lookup-events` over the window,
   filtered to write operations (EventName starting with Create/Update/Delete/Put/
   Modify/Attach/Detach/Set). Shows time, EventName, Username, and the affected
   resource. This is the "who changed what" view — the most actionable signal.
5. **Note on GitLab deploy correlation** — GitLab pipeline/MR data is not
   queried here; that correlation is handled by the Hub's deploy-context
   attachment (see `docs/AI.md §4`).

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `cloudtrail:LookupEvents` | **Yes — core** | Mutating API calls in the window |
| `cloudformation:DescribeStackEvents` | **Yes** | Stack create/update/delete events |
| `cloudformation:ListStacks` | No | Find the stack by app name |
| `ecs:DescribeServices` | No | ECS deployment history |
| `ecs:ListClusters` | No | Discover the cluster by app name |
| `ecs:ListServices` | No | Discover the service within the cluster |

## How to interpret (raw output → hypotheses)

**ECS deployment findings**

- **A `PRIMARY` deployment whose `createdAt` falls inside or just before the
  alarm window** → a task-def rollout is the prime suspect. Correlate with
  `ecs-investigation` to see whether tasks are running or crash-looping.
- **`rolloutState: FAILED`** alongside a recent deploy → the new revision broke
  at startup; the service may have rolled back. Check the task-def revision
  shown and pivot to `ecs-investigation` for stopped-task reasons.
- **Multiple `ACTIVE` deployments** → a previous rollout did not finish draining;
  the cluster may be in a transitional state.

**CloudFormation findings**

- **A stack event with `UPDATE_COMPLETE` or `UPDATE_IN_PROGRESS` on a
  security-group, IAM role, load-balancer rule, or environment-variable resource
  in the window** → configuration change is the prime suspect. The resource name
  and timestamp in the event output narrow the pivot: security-group change →
  `network-connectivity` skill; IAM change → `iam-permissions` skill.
- **`UPDATE_ROLLBACK_COMPLETE`** → a stack update failed and auto-rolled back;
  the app may be in an inconsistent state depending on whether the rollback was
  clean.
- **No stack events in the window** → this app's infrastructure was not touched
  via CloudFormation in the lookback period. Does not rule out console/API
  one-off changes — see CloudTrail section.
- **A recently-updated *sibling* stack** (listed under `LastUpdatedTime`) with a
  name suggesting a shared resource (VPC, security-group module, secrets) → the
  change may have had a blast radius wider than the primary stack.

**CloudTrail mutating events**

- **A `ModifyDBInstance`, `ModifyDBClusterParameterGroup`, or `RebootDBInstance`
  in the window** → database config change or reboot is the prime suspect; pivot
  to `database-connectivity`.
- **A `AuthorizeSecurityGroupIngress` / `RevokeSecurityGroupIngress` /
  `ModifyNetworkInterfaceAttribute`** → network path change; pivot to
  `network-connectivity`.
- **An `UpdateFunctionConfiguration` / `UpdateFunctionCode`** → Lambda change;
  pivot to `lambda-errors`.
- **A `PutRolePolicy` / `AttachRolePolicy` / `DetachRolePolicy`** near the alarm
  → IAM change is a strong lead; pivot to `iam-permissions`.
- **A `PutSecretValue` / `UpdateSecret`** → credentials/config rotated; the app
  may be holding stale cached values.
- **No mutating events from your team's principals in the window** → points away
  from "we broke it"; suspect external/dependency causes (upstream API, AWS
  service disruption, certificate expiry). Check `certificate-expiry` and
  `database-connectivity`.

**Combined signals**

- **ECS deploy + CFN stack update within 30 min of alarm** → extremely high
  confidence the change caused the incident. Frame as: *"A task-def rollout
  (revision X→Y at HH:MM) and a stack update (resource R at HH:MM) both
  occurred within 30 minutes of the alarm. These are the prime suspects."*
- **CloudTrail change by a service account rather than a human principal** →
  may indicate automation (autoscaling policy, rotation lambda, drift remediation)
  triggered a side-effect rather than a deliberate human change.
- **No changes of any kind in the window** → hypothesis: the incident is driven
  by external load, a dependency outage, or a slow-burn resource exhaustion that
  crossed a threshold. Widen the window (`RELAY_WINDOW_MINUTES=4320` for 3 days)
  and re-run, or pivot to `cloudwatch-alarm-context` to examine metric trends.

Always present these as hypotheses with the evidence line (timestamp + event/
resource) that supports them, never as a confirmed root cause.
