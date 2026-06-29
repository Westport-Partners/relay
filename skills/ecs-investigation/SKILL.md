---
name: ecs-investigation
description: >
  Diagnose an AWS ECS (Fargate/EC2) service incident — read-only. Identifies the
  cluster, service, tasks, and ALB target group; surfaces failed deployments,
  stopped-task reasons, unhealthy targets, and rollout state. Use when an
  incident involves an ECS-deployed application (5xx, unavailable, restart loop,
  failed deploy). Resolves the cluster/service from the app name when not given.
---

# ECS investigation

Most Relay-monitored apps run on ECS, so "is the service actually healthy, and
did a deploy break it?" is the most common first question. This skill answers it
without you remembering the describe-call chain.

## When to use

- The incident's app is deployed on ECS (Fargate or EC2 launch type).
- Symptoms: elevated 5xx, service unavailable, tasks restarting, a deploy that
  "went out" right before the alarm, target group draining/unhealthy.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | yes | App name; used to discover the cluster/service when not supplied. |
| `RELAY_ECS_CLUSTER` | no | Cluster name/ARN. If absent, the probe lists clusters and matches on app name. |
| `RELAY_ECS_SERVICE` | no | Service name. If absent, matched within the cluster by app name. |
| `RELAY_WINDOW_MINUTES` | no | Lookback for stopped tasks / events (default 60). |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... [RELAY_ECS_CLUSTER=...] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which cluster/service it's investigating and how it found them.
2. **Service summary** — desired/running/pending counts, launch type, task def revision.
3. **Deployments** — active deployments and rollout state (`PRIMARY`/`ACTIVE`),
   failed-task counts, circuit-breaker rollback status.
4. **Service events** — the last N `describe-services` events (these state the
   *reason* a service can't place tasks or register targets).
5. **Stopped tasks** — recently stopped tasks with `stoppedReason` and container
   exit codes.
6. **ALB target health** — target group health for the service, with
   `reason`/`description` for unhealthy targets.

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `ecs:DescribeServices` | **Yes** | Service status, deployments, events |
| `ecs:ListClusters` | No | Discover the cluster by app name |
| `ecs:ListServices` | No | Discover the service within the cluster |
| `ecs:ListTasks` | No | Find recently stopped tasks |
| `ecs:DescribeTasks` | No | Stopped-task details and exit codes |
| `elasticloadbalancing:DescribeTargetHealth` | No | ALB target health for tasks |

## How to interpret (raw output → hypotheses)

- **`runningCount < desiredCount` + stopped tasks with `OutOfMemory` /
  exit code 137** → container OOM. Hypothesis: memory limit too low or a leak,
  often right after a deploy. Cross-check the task def revision bump.
- **`stoppedReason` mentions `CannotPullContainerError` / `ResourceInitializationError`**
  → bad image tag, ECR auth, or a missing Secrets Manager secret referenced by
  the task def. Very common on a just-deployed revision.
- **Deployment `rolloutState: FAILED` or circuit breaker rolled back** → the new
  task def is crash-looping; the service likely reverted. Correlate the deploy
  time with the alarm via the `recent-changes` skill.
- **Service event "unable to place tasks" / "insufficient capacity"** → ENI/IP
  exhaustion in the subnets, no Fargate capacity, or (EC2) no container
  instances with room.
- **Targets `unhealthy` with `Health checks failed`** → app boots but the ALB
  health-check path 5xx/times out; check the container logs (use
  `cloudwatch-alarm-context`) and the health-check path/grace period.
- **Targets `draining` and never replaced** → deployment stuck; tie to the
  deployments section.
- **Healthy service + healthy targets** → the problem is likely downstream
  (database, dependency, network) — pivot to `database-connectivity` /
  `network-connectivity`, or upstream (the ALB/DNS/cert) — pivot to
  `certificate-expiry`.

Always present these as hypotheses with the evidence line that supports them,
never as a confirmed cause.
