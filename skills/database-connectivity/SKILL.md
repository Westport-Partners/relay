---
name: database-connectivity
description: >
  RDS/Aurora database connectivity diagnostics — read-only. Checks instance/cluster
  availability, connection-count saturation vs peak, recent failovers/reboots/
  maintenance events, and the security-group path from the app to the database.
  Use for "can't connect to DB", connection-pool exhaustion, timeouts, or DB
  failover symptoms. Resolves the DB identifier from the app name when not given.
---

# Database connectivity investigation

The most common class of "app is broken" incidents that aren't a code change is a
database connectivity problem: the instance is down, the connection pool is
exhausted, storage filled, or a security-group rule was changed. This skill gathers
all of that evidence in one pass without you remembering the RDS/CloudWatch/EC2
describe chains.

## When to use

- Symptoms: "can't connect to database", connection-pool exhaustion or timeout
  errors, `too many connections`, DB failover page, write failures.
- An ECS/Lambda app is throwing DB connection errors (use `ecs-investigation` first
  to confirm the app layer is healthy, then pivot here).
- After a multi-AZ failover notification — confirm the new primary is `available`
  and that no pool connections were stranded.
- After a maintenance window — instance may have rebooted; check event history.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | yes | App name; used to discover the DB identifier when `RELAY_DB_IDENTIFIER` is not supplied. |
| `RELAY_DB_IDENTIFIER` | no | RDS instance id or Aurora cluster id. If absent the probe lists instances/clusters and matches on app name. |
| `RELAY_APP_SECURITY_GROUP` | no | The app's security-group id (e.g. `sg-0abc1234`). Used to check whether the DB SG inbound rules allow a path from the app. If absent, the probe still prints the DB SG inbound rules for the DB port. |
| `RELAY_WINDOW_MINUTES` | no | Lookback for CloudWatch metrics and RDS events (default 60). |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... [RELAY_DB_IDENTIFIER=...] \
  [RELAY_APP_SECURITY_GROUP=sg-...] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which DB instance/cluster is being investigated and how it was found.
2. **DB status** — `DBInstanceStatus`/`Status`, engine + version, MultiAZ, endpoint
   address + port, instance class. Flags status != `available`.
3. **Connection saturation** — CloudWatch `DatabaseConnections` (Maximum), `CPUUtilization`
   (Average), `FreeableMemory` (Minimum), and `FreeStorageSpace` (Minimum) over the
   window. Prints peak observed connections; instructs the agent to compare against
   the instance's known `max_connections` limit (derived from instance class, not
   directly available as a metric).
4. **Recent DB events** — `rds describe-events` for the window: failovers, reboots,
   maintenance, parameter changes, storage events.
5. **Security-group path** — describes the DB's `VpcSecurityGroups`, then
   `ec2 describe-security-groups` on the DB SG. Prints inbound rules for the DB
   port and states whether a path from `RELAY_APP_SECURITY_GROUP` appears to exist.
   If `RELAY_APP_SECURITY_GROUP` is unset, notes it and prints all inbound rules for
   the DB port so a human can judge.

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `rds:DescribeDBInstances` | **Yes** | RDS instance details |
| `rds:DescribeDBClusters` | **Yes** | Aurora cluster details |
| `ec2:DescribeSecurityGroups` | **Yes** | DB security-group inbound rules |
| `cloudwatch:GetMetricStatistics` | No | Connection/CPU/memory saturation metrics |
| `rds:DescribeEvents` | No | DB events in the window |

## How to interpret (raw output → hypotheses)

- **`DBInstanceStatus` / cluster `Status` != `available`** (e.g. `rebooting`,
  `modifying`, `failing-over`, `storage-full`) → the database is not accepting
  connections right now. Status explains the outage directly; no need to look
  further until it returns to `available`.
- **A `failover` or `reboot` event within the window** → multi-AZ standby promoted;
  DNS TTL lag or hardcoded IP could leave the app pointing at the old primary for
  up to ~60 s. Connection-pool stranding is common post-failover.
- **`DatabaseConnections` (peak) plateauing at or near the instance's `max_connections`
  limit** → pool exhaustion. The app is either not releasing connections (connection
  leak, missing `finally`/`using` blocks) or the pool is sized larger than
  `max_connections` allows. Hypothesis: scale the instance class, reduce pool
  `maxSize`, or fix the leak. Cross-check `CPUUtilization` — if CPU is also high,
  suspect long-running queries holding connections open.
- **`FreeStorageSpace` near zero (< 1 GiB or < 5% of allocated)** → storage-full
  condition. RDS sets the instance to read-only when storage fills; writes fail and
  the app may interpret this as a connectivity error. Hypothesis: storage autoscaling
  may be disabled or the growth rate exceeded its threshold.
- **`FreeableMemory` very low** → instance may be swapping; slow queries accumulate,
  connections queue, and the app's connect timeout fires. Hypothesis: instance class
  too small for the workload.
- **DB SG inbound rules for the DB port do not include the app SG
  (`RELAY_APP_SECURITY_GROUP`) or any covering CIDR** → the classic "new task/SG
  can't reach DB" misconfiguration. The network path is blocked at the security-group
  layer. Pivot to `network-connectivity` for deeper NACL/route-table analysis.
- **DB SG has a correct inbound rule but the app still can't connect** → suspect
  NACLs, subnet route tables, or a VPC endpoint issue. Pivot to
  `network-connectivity`.

Always present these as hypotheses with the evidence line that supports them,
never as a confirmed cause.
