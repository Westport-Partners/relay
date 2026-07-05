# Relay — Configure Prompt

You are helping the user configure a deployed Relay instance. Configuration covers routing and escalation policy YAML (config-as-code seed), contacts and schedules (DynamoDB only), GitLab as a config source, and optional OIDC authentication.

**Critical rule before touching any config: no PII in Git, ever.** Config YAML files reference opaque `contact_id` values only — never names, emails, or phone numbers. Those live in DynamoDB in the user's own account.

Canonical references: [`docs/configure.md`](../docs/configure.md), [`config/README.md`](../config/README.md).

---

## The config/PII split

| Data | Where | Rule |
|------|-------|------|
| Escalation policies, routing rules | Git (`config/escalation.yaml`, `config/routing.yaml`) — startup seed | Reference `contact_id` values only; no PII |
| Routing + ignore rules at runtime | DynamoDB (UI-managed); the YAML is a seed only — **DB wins at runtime** | Instant edits without redeploy |
| On-call schedules, availability | DynamoDB only | Never in Git |
| Contact PII (name, email, phone) | DynamoDB, encrypted at rest | Never in Git |
| Integration credentials | DynamoDB settings table, set on the Settings screen | No secret to pre-create; Settings value overrides any env var fallback |

---

## Step 1 — Copy the example config files

```bash
cp config/escalation.example.yaml config/escalation.yaml
cp config/routing.example.yaml    config/routing.yaml
```

Existing files are never overwritten by the installer or `relay-update.sh`. Edit the copies.

---

## Step 2 — Edit escalation policies

`config/escalation.yaml` defines named escalation policies. Each policy is a list of steps; each step fires when the previous step's timeout elapses without acknowledgment.

Minimal example (SEV1 policy):

```yaml
escalation_policies:

  - name: p1-critical
    severity: SEV1
    steps:
      - step: 1
        label: "Page primary on-call"
        notify:
          role: primary
          channel: [sms, email]
        ack_timeout_minutes: 5

      - step: 2
        label: "Escalate to secondary"
        notify:
          role: secondary
          channel: [sms, email]
        ack_timeout_minutes: 5

      - step: 3
        label: "Repeat all contacts"
        notify:
          roles: [primary, secondary, manager]
          channel: [sms, email]
        repeat_every_minutes: 15
```

Valid `role` values: `primary`, `secondary`, `manager`. Valid `channel` values: `sms`, `email`. Escalation timers are DynamoDB deadlines — they survive a container restart.

---

## Step 3 — Edit routing rules

`config/routing.yaml` maps alarm metadata to severity and policy. Rules are evaluated in ascending `priority` order (lower number first); first match wins. The list must be sorted ascending.

```yaml
routing_rules:

  - name: database-critical
    priority: 10
    match:
      namespace: "AWS/RDS"
      alarm_tags:
        Environment: production
    route:
      severity: SEV1
      escalation_policy: p1-critical
      streams: [team, central]

  - name: default
    priority: 1000
    match: {}
    route:
      severity: SEV2
      escalation_policy: p2-high
      streams: [team, central]
```

**Match fields** (all optional, ANDed): `alarm_name_prefix`, `alarm_name_pattern`, `namespace`, `alarm_tags`, `source` (`cloudwatch` or `synthetic`).

**Route fields:** `severity` (SEV1–SEV4), `escalation_policy`, `streams` (`team`, `central`, or both), `tags`.

**Severity tiers:**

| Tier | Default ack window | Behavior |
|---|---|---|
| SEV1 | ~5 min | Fast page; repeating pages until acked |
| SEV2 | ~15 min | Pages primary + secondary |
| SEV3 | — | Email / team-stream notification; no SMS |
| SEV4 | — | Logged; no page, no email |

### DB wins at runtime

The `routing_rules:` and `ignore:` blocks in `routing.yaml` are **startup seeds only**. On first boot, if DynamoDB is empty, Relay seeds it from the YAML. After that, DynamoDB is the runtime source of truth; UI edits take effect instantly. A changed `routing.yaml` on restart does **not** clobber live UI edits.

To resync Git with the live DB state:

```bash
# Regenerate the rules: block from DynamoDB
curl -s "http://<DashboardUrl>/routing-rules/download"

# Regenerate the ignore: block
curl -s "http://<DashboardUrl>/rules/download"
```

---

## Step 4 — Rebuild and redeploy (if baking config into the image)

If `RELAY_CONFIG_SOURCE=local` (config baked into the image), rebuild and redeploy the compute stack after editing YAML:

```bash
RELAY_CONFIG_DIR=config \
  export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"

RELAY_DEPLOY_TYPE=team \
RELAY_TEAM_NAME=<team> \
RELAY_HUB_IMAGE_URI=$RELAY_HUB_IMAGE_URI \
RELAY_STACK_SELECTOR=compute \
./scripts/relay-deploy.sh
```

If using GitLab config source (see Step 7), no redeploy is needed for config changes.

---

## Step 5 — Add contacts

Contacts (name, email, phone) are stored in DynamoDB only. Use the Contacts screen in the dashboard or the seed script for bulk import:

```bash
# Bulk import from a JSON file (format documented in scripts/relay-seed-contacts.sh)
./scripts/relay-seed-contacts.sh
```

Contacts are never committed to Git. Reference them in escalation policies by their opaque `contact_id` only.

---

## Step 6 — Build the on-call schedule

Open the **Scheduling** screen in the dashboard:

1. Set each contact's **availability** (which shifts they can cover) and any **OOO** periods.
2. Click **Auto-schedule** to generate the week's schedule. The scheduler fills all three roles (primary, secondary, manager) across the three shifts.
3. Review **gap highlighting** — yellow cells indicate shifts without a qualified contact.
4. Create **ad-hoc overrides** (drag a name onto a cell) to handle edge cases.

Schedules live entirely in DynamoDB. See [`docs/scheduling.md`](../docs/scheduling.md).

---

## Step 7 — GitLab config source (optional)

To load `routing.yaml` and `escalation.yaml` from a GitLab repository at container startup (enables merge-request review workflows without rebuilding the image):

1. Store a GitLab personal access token in AWS Secrets Manager under the name `relay/gitlab-token` (or the name you set for `RELAY_GITLAB_SECRET_NAME`). Required scope: `read_repository`.
2. Set at deploy time (or as container environment variables):
   - `RELAY_CONFIG_SOURCE=gitlab`
   - `RELAY_GITLAB_REPO=<project-id-or-path>` (e.g. `my-group/relay-config`)
   - Optionally `RELAY_GITLAB_SECRET_NAME=relay/gitlab-token` (default)
   - Optionally `RELAY_GITLAB_BASE_URL=https://gitlab.example.com` for self-hosted instances

The container fetches and caches config at startup; no webhook is needed.

---

## Step 8 — OIDC authentication (optional)

By default the dashboard is read-only (`RELAY_AUTH_MODE=none`). To enable write operations with ALB-enforced OIDC:

```bash
# Interactive helper — prompts for IdP details, updates the ALB listener rule
./scripts/relay-setup-oidc.sh
```

Set `RELAY_AUTH_MODE=alb` and optionally `RELAY_UI_AUTH_MODE=alb` at deploy time to enforce authentication. See [`config/README.md`](../config/README.md) (auth section) for the full OIDC configuration knobs.

---

## Federation gate (federated-hub topology only)

If this team node forwards up to a federated Hub (`RELAY_HUB_SCOPE=local-federated`), add a `federation:` block to `routing.yaml` to control which incidents cross the second hop:

```yaml
federation:
  min_severity: SEV2
  forward_states: [TRIGGERED, ESCALATED]
  overrides:
    - name: dev-apps
      environment: dev
      forward: never
```

There are no `RELAY_FORWARD_*` env vars — the `federation:` block is the only path. See [`docs/configure.md`](../docs/configure.md) for the full schema.

---

## Key environment variables reference

For a full table see [`docs/configure.md`](../docs/configure.md). Most commonly needed:

| Variable | Description |
|---|---|
| `RELAY_TABLE_NAME` | DynamoDB table name (set by deploy) |
| `RELAY_CONFIG_SOURCE` | `local` \| `gitlab` |
| `RELAY_CONFIG_DIR` | Local config directory path (default `config`) |
| `RELAY_AUTH_MODE` | `none` \| `alb` \| `dev` |
| `RELAY_TZ` | IANA timezone for schedule resolution |
| `RELAY_AI_ENABLED` | `true` to enable AI briefings/AARs |
| `RELAY_AI_PROVIDER` | `bedrock` \| `bedrock-converse` \| `openai` |
