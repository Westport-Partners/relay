---
name: cloudwatch-alarm-context
description: >
  Read-only. Pulls the firing CloudWatch alarm's recent metric datapoints and
  threshold, lists sibling alarms currently in ALARM state (blast radius), and
  tails recent error/exception log lines for the resource via Logs Insights /
  filter-log-events. Use when an incident is triggered by a CloudWatch alarm and
  you need to understand: is this a spike or sustained degradation? are multiple
  alarms from a shared cause? what do the logs say is actually failing?
---

# CloudWatch alarm context

A CloudWatch alarm fired — but what does that mean in context? This skill
answers three questions in order:

1. **What exactly is the alarm measuring, and what has its metric been doing?**
   (Trend: is it a transient spike or sustained breach?)
2. **Are there sibling alarms also in ALARM right now?**
   (Blast radius: many alarms in a tight window usually mean one shared
   dependency is failing, not N independent incidents.)
3. **What do the application logs say?**
   (Proximate cause: the log lines often name the actual failure — DB timeout,
   AccessDenied, DNS failure — and point to which pivot skill to use next.)

## When to use

- An incident was triggered by a CloudWatch alarm.
- You need the raw metric trend to distinguish spike vs sustained breach.
- You want to correlate the firing alarm with others to find a shared cause.
- You need the first log-level signal before diving into a specialist skill.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | yes | App name; used to match alarms and log groups when exact names are not supplied. |
| `RELAY_ALARM_NAME` | no | The exact name of the firing alarm. If absent, the probe lists alarms in ALARM state and matches on `RELAY_APP_NAME`. |
| `RELAY_LOG_GROUP` | no | CloudWatch Logs group to tail. If absent, the probe runs `logs describe-log-groups` and matches on app name. |
| `RELAY_WINDOW_MINUTES` | no | Lookback window in minutes (default 60). |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... [RELAY_ALARM_NAME=...] [RELAY_LOG_GROUP=...] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which alarm, log group, and window were chosen and how.
2. **Alarm detail** — metric name, namespace, statistic, threshold, comparison
   operator, period, and the current `StateReason` from CloudWatch. This is the
   alarm's own explanation of why it fired.
3. **Metric history** — `get-metric-statistics` over the window with 60-second
   resolution. Datapoints are sorted by time so you can see the trend — when the
   metric crossed the threshold and whether it has recovered.
4. **Sibling alarms in ALARM** — all alarms currently in ALARM state, their
   metric, and their namespace. If several alarms from the same app (or the same
   shared dependency) are all firing within a short window, this section shows it.
5. **Recent error logs** — `filter-log-events` over the window with pattern
   `?ERROR ?Error ?error ?Exception ?timeout ?5xx`, last ~20 lines with
   timestamps. Capped to keep output scannable.

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `cloudwatch:DescribeAlarms` | **Yes** | Firing alarms and alarm details |
| `cloudwatch:GetMetricStatistics` | **Yes** | Metric datapoints over the lookback window |
| `logs:DescribeLogGroups` | No | Discover the log group by app name |
| `logs:FilterLogEvents` | No | Error-pattern log lines in the window |

## How to interpret (raw output → hypotheses)

### Metric trend (section 3)

- **Single spike then recovery** → transient burst (downstream timeout, GC
  pause, cold-start). Alarm may have already auto-recovered. Hypothesis: retry
  storm or resource contention, likely self-healing.
- **Sustained breach for the whole window** → real degradation in progress.
  Cross-check with sibling alarms and log lines to find the shared cause.
- **Staircase climb** → capacity leak (connections, memory, open files) — the
  resource is being consumed over time. Pivot to `ecs-investigation` (OOM) or
  `database-connectivity` (connection saturation).
- **Cliff drop then flat zero** → the metric source stopped reporting. Suggests
  the service itself crashed or the metrics agent failed. Cross-check
  `ecs-investigation` for stopped tasks.

### Sibling alarms (section 4)

- **Many alarms in ALARM at the same time across the same app** → almost
  certainly ONE incident with a shared cause, not N separate problems. Look for
  the deepest/lowest-level dependency among the firing metrics (DB, cache,
  network). Avoid creating N incident tickets — merge them.
- **Alarms in ALARM from different apps sharing a namespace** (e.g., same RDS
  cluster, same ALB) → shared infrastructure failure. Pivot to
  `database-connectivity` or `network-connectivity`.
- **Only the one alarm** → isolated failure; trust the log lines.

### Error log lines (section 5)

| Log pattern | Likely hypothesis | Pivot skill |
|---|---|---|
| `DB connection refused` / `timeout` / `too many connections` | Database connectivity or saturation | `database-connectivity` |
| `AccessDenied` / `UnauthorizedOperation` / `is not authorized` | IAM permission revoked or missing | `iam-permissions` |
| `connection refused` / `no route to host` / `Name or service not known` | Network path broken (SG, NACL, VPC endpoint) | `network-connectivity` |
| `SSL` / `certificate` / `PKIX` | TLS/cert expiry | `certificate-expiry` |
| `Task failed to start` / `CannotPull` / `OOM` | ECS launch failure | `ecs-investigation` |
| `Task timed out` / `RequestTooLarge` / `throttle` | Lambda limits | `lambda-errors` |

Always present these as hypotheses with the supporting evidence line, never as a
confirmed root cause. The human decides.
