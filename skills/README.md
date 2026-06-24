# Relay investigation skill pack

Default, read-only investigation skills for Relay's AI tooling. When a node
handles a `TRIGGERED` incident it spawns **headless Claude Code** with this pack
mounted and a read-only AWS allow-list, hands it the incident context packet,
and collects structured findings (see `docs/AI.md §3`). The findings attach to
the incident timeline and forward to the central Hub.

These skills encode the *first 5–20 minutes* of mechanical triage — the gather
phase that dominates time-to-understanding — as vetted, deterministic probes so
the agent spends its reasoning budget on synthesis, not on remembering CLI flags.

## What's here

| Skill | Answers |
|---|---|
| [`ecs-investigation`](ecs-investigation/SKILL.md) | Is the ECS service healthy? Cluster / service / task / ALB target health, stopped-task reasons, deployment rollout state. |
| [`recent-changes`](recent-changes/SKILL.md) | What changed near the incident window — ECS deployments, CloudFormation stack updates, CloudTrail mutating events, recent deploys. The single highest-leverage triage question. |
| [`cloudwatch-alarm-context`](cloudwatch-alarm-context/SKILL.md) | The firing alarm's metric history, sibling alarms currently in ALARM, and recent error log lines for the resource. |
| [`certificate-expiry`](certificate-expiry/SKILL.md) | ACM cert status/expiry, ALB listener certs, and live TLS handshake expiry for an endpoint. |
| [`database-connectivity`](database-connectivity/SKILL.md) | RDS/Aurora availability, connection-count saturation, recent failovers/reboots, and the security-group path from app to DB. |
| [`iam-permissions`](iam-permissions/SKILL.md) | AccessDenied root cause — CloudTrail denied calls, the principal's attached/inline policies, and `iam simulate-principal-policy`. |
| [`network-connectivity`](network-connectivity/SKILL.md) | "Can't reach X" — security groups, NACLs, route tables, VPC endpoints, subnet/AZ reachability. |
| [`lambda-errors`](lambda-errors/SKILL.md) | Lambda error rate, throttles, timeouts, concurrency limits, and recent function/config changes. |

## Conventions (every skill follows these)

1. **Read-only, always.** Probes use only `describe*` / `list*` / `get*` /
   `lookup-events` / `filter-log-events` calls — never a mutating API. The
   agent's allow-list should enforce this too; the scripts are a vetted second
   line so a skill can't drift into a write. No skill ever changes account state.
2. **Shape.** Each skill is a directory with:
   - `SKILL.md` — YAML frontmatter (`name`, `description`) + when-to-use,
     inputs, the probe invocation, and **how to interpret** the output into
     hypotheses. The interpretation section is the real value: it maps raw
     output to likely causes.
   - `probe.sh` — a `bash` script wrapping the read-only CLI calls. Inputs come
     from environment variables (documented in its header); it prints
     human-readable sections **and** is safe to run with missing optional
     inputs (it skips a section and says so rather than failing).
3. **Inputs from the context packet.** Skills read what the node already knows:
   `RELAY_REGION`, `RELAY_ENVIRONMENT`, `RELAY_APP_NAME`, plus skill-specific
   hints (`RELAY_ECS_CLUSTER`, `RELAY_DB_IDENTIFIER`, …). When a hint is absent
   the probe discovers candidates (e.g. lists clusters and matches on app name)
   and says what it assumed — never silently guesses.
4. **Degrade gracefully.** A probe never aborts the whole investigation: each
   section is wrapped so an error (no permission, resource not found) prints a
   note and moves on. Mirrors the "AI augments, never gates" guarantee.
5. **Time-boxed.** Probes default to a tight lookback window
   (`RELAY_WINDOW_MINUTES`, default 60) so output stays scannable and API calls
   stay cheap.
6. **Findings, not verdicts.** SKILL.md interpretation always frames output as
   *hypotheses with evidence*, never a confirmed root cause. The human decides.

## Required read-only permissions

The agent's task role / allow-list needs (read-only) across the services the
enabled skills touch: `ecs:Describe*`/`List*`, `elasticloadbalancing:Describe*`,
`cloudwatch:Describe*`/`GetMetric*`, `logs:FilterLogEvents`/`Get*`/`Describe*`,
`cloudtrail:LookupEvents`, `cloudformation:Describe*`/`List*`,
`acm:Describe*`/`List*`, `rds:Describe*`, `ec2:Describe*`,
`iam:Get*`/`List*`/`SimulatePrincipalPolicy`, `lambda:Get*`/`List*`. Grant only
what the deployed skill set uses. None of these can mutate state.

## Local use

Each probe is runnable by hand for testing:

```bash
RELAY_REGION=us-east-1 RELAY_APP_NAME=checkout-api \
RELAY_ECS_CLUSTER=relay-hub ./skills/ecs-investigation/probe.sh
```

Teams extend the pack the same way they extend config/catalog: add a directory,
follow the conventions, open an MR.
