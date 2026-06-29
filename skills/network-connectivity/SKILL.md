---
name: network-connectivity
description: >
  Read-only reachability analysis between two points in a VPC — security
  groups, NACLs, route tables, VPC endpoints, and subnet/AZ placement.
  Use for connection timeouts/refused, "cannot reach" a dependency, or
  suspected SG/NACL/routing misconfig. Hypotheses only; never mutates
  account state.
---

# Network-connectivity investigation

"Can't reach X" is one of the most common incident patterns: an ECS task
times out connecting to its database, an app can't call Secrets Manager,
a service can't reach an internal API. The cause is nearly always in one
of four places: a missing security-group inbound rule on the target, a
NACL missing ephemeral return ports, a route table with no path out, or a
missing VPC endpoint in a no-NAT subnet. This skill checks all four
systematically so you don't have to remember the describe-call chain.

## When to use

- Connection timeout or "connection refused" to a dependency.
- "Cannot reach" an AWS service (S3, ECR, Secrets Manager, SSM, etc.)
  from a private subnet.
- Suspected security-group or NACL misconfiguration after a recent
  infrastructure change.
- A new ECS task or Lambda has no outbound access.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_SOURCE_SG` | no | Source security group ID (e.g. the ECS task SG). |
| `RELAY_TARGET_SG` | no | Target security group ID (e.g. the DB or dependency SG). |
| `RELAY_TARGET_PORT` | no | Port traffic should reach (default `443`). |
| `RELAY_SUBNET_IDS` | no | Comma-separated subnet IDs to inspect NACLs and routes for. If absent, discovered from the SGs' VPC. |
| `RELAY_VPC_ID` | no | VPC to scope the inspection. If absent, derived from the provided SGs. |

All inputs are optional except `RELAY_REGION`; when SG IDs are omitted the
probe still runs the VPC-endpoint and route sections with whatever is
resolvable, and it notes what it could not check.

## Run

```bash
RELAY_REGION=us-east-1 \
  RELAY_SOURCE_SG=sg-0abc123 \
  RELAY_TARGET_SG=sg-0def456 \
  RELAY_TARGET_PORT=5432 \
  ./probe.sh
```

The probe prints these sections (each isolated — one failure never aborts the rest):

1. **Resolution** — what source/target/port/VPC it's analyzing and where each value came from.
2. **Security group rules** — inbound rules on the target SG for the target port and outbound rules on the source SG. Explicitly states whether an allowing rule exists. This SG-pair check is the most common culprit.
3. **NACLs** — inbound and outbound NACL entries for the target subnets relevant to the target port and ephemeral return ports (1024–65535). NACLs are stateless; a missing ephemeral-return rule silently blocks even when SGs are open.
4. **Route tables** — routes for the target subnets; notes whether a path exists (local, NAT, IGW, TGW, VPC peering, or VPC endpoint).
5. **VPC endpoints** — interface and gateway endpoints in the VPC; relevant when the dependency is an AWS service reached from a no-NAT private subnet.

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `ec2:DescribeSecurityGroups` | **Yes** | Security-group inbound/egress rules |
| `ec2:DescribeNetworkAcls` | **Yes** | NACL stateless rules per direction |
| `ec2:DescribeRouteTables` | **Yes** | Routes and default-gateway presence |
| `ec2:DescribeSubnets` | No | Discover subnets in the VPC |
| `ec2:DescribeVpcEndpoints` | No | VPC endpoints for AWS-service connectivity |

## How to interpret (raw output → hypotheses)

- **Target SG has no inbound rule matching the port and source SG / CIDR** →
  the SG is blocking the connection. This is the single most common
  cause. Check whether the rule was ever added or was recently deleted
  (correlate with `recent-changes`).
- **Source SG egress has been restricted and the target port/CIDR is not
  listed** → outbound SG blocking. The default egress is allow-all (a
  single `0.0.0.0/0` rule); if that rule is absent, check what remains.
- **NACL has an inbound ALLOW on the target port but no outbound ALLOW on
  ephemeral return ports (1024–65535)** → stateless-NACL blocker. TCP
  works at the SG level (stateful) but NACLs require both directions.
  The same applies in reverse: inbound ephemeral from the client must be
  allowed if the NACL on the source subnet is restricted.
- **NACL DENY rule with a lower rule number than the ALLOW** → explicit
  deny wins. Rule ordering matters; lower number = higher precedence.
- **Route table for the target subnet has no default route (no NAT, no
  IGW, no TGW)** → truly isolated private subnet; there is no path to
  anything outside the VPC. A missing `0.0.0.0/0 → nat-*` route in an
  otherwise-private subnet is a classic "can't reach Secrets Manager /
  ECR / S3" cause.
- **No VPC endpoint for the AWS service + no NAT gateway route** → the
  ECS task or Lambda cannot reach the AWS service privately. Add an
  interface endpoint (Secrets Manager, ECR API, ECR DKR, SSM, etc.) or
  a gateway endpoint (S3, DynamoDB), or route through a NAT.
- **All SG rules, NACLs, and routes look open** → the network layer is
  not blocking; pivot to the application layer (wrong hostname/port,
  TLS cert mismatch, DNS not resolving, app-level auth). Check
  `certificate-expiry` for TLS, `iam-permissions` for auth, or
  `database-connectivity` for DB-specific diagnostics.

Always present these as hypotheses with the evidence line that supports
them, never as a confirmed root cause.

> **Note:** This skill is read-only and reasons from configuration — it
> cannot run live packet captures or inject test traffic. For a definitive
> reachability verdict, a human can run **VPC Reachability Analyzer**
> (`ec2 start-network-insights-analysis`) in the console or via CLI.
