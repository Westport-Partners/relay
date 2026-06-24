# Config-as-code model

Relay's operational configuration lives in this directory as YAML files, version-controlled in Git
alongside the application code. This page explains what belongs here, what does not, and how to
make changes safely.

---

## The golden rule: no PII in Git

Config files reference contacts only indirectly — escalation policies page by
on-call **role** (primary / secondary / manager), and roles resolve to a person
via the generated **schedule**. Actual names, email addresses, and phone numbers
live **only in DynamoDB in your team's account**.

| Data | Where it lives | Why |
|------|---------------|-----|
| Escalation policies (page which role, wait, escalate) | `config/escalation.yaml` in Git | Version-controlled, reproducible, reviewable |
| Routing rules | `config/routing.yaml` in Git | Same |
| On-call schedule (who holds each role/shift) | DynamoDB (built on the Schedule screen) | Changes weekly; edited in the UI, not via Git MRs |
| Contact name, email, phone | DynamoDB `relay-contacts` table | Stays in your account; encrypted at rest (KMS); never in a shared repo |

This split gives you the best of both worlds: operational rules are auditable and clone-deployable;
PII is account-local and trivially editable without a Git MR.

---

## Files in this directory

| File | Purpose |
|------|---------|
| `escalation.yaml` | Escalation policies — page the primary role, wait N minutes, escalate to secondary, escalate to manager |
| `routing.yaml` | Routing rules — map alarm metadata (name pattern, namespace, tags) to severity and escalation policy |

> **On-call scheduling is not a Git file.** Who holds each role (primary /
> secondary / manager) for each shift is set on the **Schedule** screen and
> stored in DynamoDB — it changes too often for Git MRs and references PII-bearing
> contacts. Escalation policies page a *role*; the schedule resolves the role to
> the current person at page time.

The `.example.yaml` files in this directory are templates. Copy them to the non-example name and
edit for your team:

```bash
cp config/escalation.example.yaml config/escalation.yaml
cp config/routing.example.yaml    config/routing.yaml
```

---

## Managing contacts (contact_id values)

Before you can build a schedule you need contacts. Add contacts via the Relay CLI or
the team setup UI. Only do this after deploying the stack (contacts are stored in your
account's DynamoDB table).

```bash
# Add a contact — returns a contact_id
relay contacts add --name "Jane Smith" --email "jane@example.com" --phone "+15555550100"
# -> created contact cnt_abc123

# List contacts (shows IDs and names, but not full PII by default)
relay contacts list

# Remove a contact
relay contacts remove cnt_abc123
```

Assign these contacts to roles and shifts on the **Schedule** screen. The PII never appears in Git.

---

## Making a config change

How you apply a change depends on which config source you're using (see
[How Relay loads config at runtime](#how-relay-loads-config-at-runtime) below).

### GitLab config source

1. Edit the relevant YAML file locally on a feature branch.
2. Open a Merge Request in GitLab for review.
3. After merge, Relay picks up the change:
   - The container fetches the updated config from GitLab using the token stored in Secrets Manager.
   - Config is parsed and cached in memory, refreshed periodically (and on an explicit reload).
   - No re-deploy or container restart is required for config-only changes.

### Local bundled YAML config source

1. Edit the YAML files in the `config/` directory (or your `RELAY_CONFIG_DIR`).
2. Rebuild and redeploy the container image — the updated files are baked into the image at build
   time (`scripts/relay-build-hub-image.sh` overlays `RELAY_CONFIG_DIR`). A `compute`-only deploy
   is enough; the data plane is untouched.
3. The container loads config from the local filesystem on startup (`RELAY_CONFIG_SOURCE=local`).

This path requires a redeploy for config changes but has no external dependencies (no GitLab
token).

**Hard rule:** config is **read** from memory on the hot path. Never write to Git as part of
paging or incident handling. Git writes (e.g., runbook updates, post-incident notes) happen only
on slow/human-initiated paths.

---

## How Relay loads config at runtime

Relay supports two config sources, selected by the `RELAY_CONFIG_SOURCE` environment variable.

### GitLab config source (default when `relay:gitlab_repo` is set)

On startup, the container:
1. Retrieves the GitLab API token from AWS Secrets Manager.
2. Fetches `config/escalation.yaml` and `config/routing.yaml` from the
   configured GitLab repository over the API.
3. Parses and caches the config in memory.

Steps 2–3 repeat on refresh. Between refreshes, config is served from the
in-memory cache — no Git calls on the alarm-handling hot path.

**Best for:** teams that want MR-based review workflows and automatic webhook-driven refresh
without a re-deploy.

### Local bundled YAML (`RELAY_CONFIG_SOURCE=local`)

Set `RELAY_CONFIG_SOURCE=local` (and optionally `RELAY_CONFIG_DIR` to override the default
`config/` path) to use the `LocalConfigLoader`. On startup, the Lambda reads the YAML files
directly from the filesystem — no network calls, no GitLab token required.

Config is refreshed on each container start and on explicit reload. A config change requires a
container image rebuild + redeploy to take effect.

**Best for:** teams without GitLab, air-gapped accounts, or anyone who wants the simplest
possible setup with no external dependencies.

**Either way**, the PII rule applies: contact names, emails, and phone numbers must never appear
in any config file committed to version control.

---

## On-call schedule semantics

On-call coverage is **not** a Git file — it's built on the **Schedule** screen and
stored in DynamoDB. The model:

- The day is three fixed 8-hour shifts: night (00–08), day (08–16), evening (16–24).
- Each (day, shift) is covered by one person **per role**: primary, secondary, manager.
- Each person sets their availability grid (which day/shift slots they'll take), a
  single out-of-office range, and which roles they're eligible for. "Auto-schedule"
  fills the week, balanced across people; primary and secondary for a slot are never
  the same person.
- Times are interpreted in the team timezone (`RELAY_TZ`). Escalation policies page a
  role; the schedule resolves the role to the current on-call person at page time.

---

## Severity tiers

| Tier | Meaning | Default behavior |
|------|---------|-----------------|
| SEV1 | Critical — customer-impacting outage | Fast paging (5 min ack window), both streams, manager escalation |
| SEV2 | High — degraded functionality | Moderate paging (15 min ack window), both streams |
| SEV3 | Warning — elevated error rate or early degradation signal | Email only, team stream only |
| SEV4 | Low / informational | Logged and routed to team stream; no paging |

Tiers are assigned by `routing.yaml` rules based on alarm metadata. See
`routing.example.yaml` for examples.

---

## Deployment configuration (`deployment:` block)

The optional `deployment:` block in `environments.yaml` centralises infrastructure
knobs that the `scripts/relay-context.sh` helper translates into `-c relay:*` CDK
context flags. The running container ignores this block entirely.

| Field | CDK context key | Description |
|-------|----------------|-------------|
| `private_hosted_zone_id` | `relay:phz_id` | Route53 **private** hosted zone ID. When set together with `private_hosted_zone_name`, the stack issues an ACM cert and publishes the ALB record into the zone. |
| `private_hosted_zone_name` | `relay:phz_name` | Private zone name, e.g. `corp.example.internal`. The ALB becomes reachable at `relay.<zone_name>` over HTTPS. |
| `alb_subdomain` | `relay:alb_subdomain` | Left-most DNS label (default `relay`). Change to host multiple Relay stacks in the same zone. |
| `certificate_arn` | `relay:certificate_arn` | Explicit ACM certificate ARN. Overrides the PHZ-derived cert when you already manage one centrally. |
| `internal_alb` | `relay:internal_alb` | `true` (default) — ALB is internal, reachable only from inside the VPC/VPN. `false` — internet-facing. |

Each field also has a corresponding env var override (wins over the file value):
`RELAY_PHZ_ID`, `RELAY_PHZ_NAME`, `RELAY_ALB_SUBDOMAIN`, `RELAY_CERT_ARN`, `RELAY_INTERNAL_ALB`.

---

## UI authentication (`auth:` block)

The optional `auth:` block controls how the Hub dashboard authenticates users.

```yaml
auth:
  mode: null           # null | none | alb | dev
  access_control:
    enabled: false
    allowed_users: []  # OIDC usernames allowed to WRITE
```

### Auth modes

| Mode | Behaviour |
|------|-----------|
| `null` | Stack default: `dev` (write-capable) for non-prod; `none` (read-only) for prod. |
| `none` | No authentication — anonymous read-only access. |
| `alb` | ALB OIDC authentication. Every request is authenticated by the ALB before reaching the container. Set this after running `scripts/relay-setup-oidc.sh`. |
| `dev` | Dev/test write-capable mode with a synthetic user header. Not for production. |

### Fine-grained access control

When `mode: alb` is active, `access_control` lets you restrict **write** operations
to a named list of OIDC identities (GitHub logins, emails, or `sub` claims depending
on your IdP):

```yaml
auth:
  mode: alb
  access_control:
    enabled: true
    allowed_users:
      - "octocat"
      - "monalisa"
```

Any authenticated identity can still **read** the dashboard; only identities in
`allowed_users` may acknowledge incidents, update on-call, or trigger actions.
When `enabled: false` (the default), any authenticated identity may write.

---

## OIDC setup helper (`scripts/relay-setup-oidc.sh`)

`relay-setup-oidc.sh` automates the ALB listener update and config flip in one
command. It:

1. Discovers the Relay ALB HTTPS (port 443) listener (or accepts `--listener-arn`).
2. Adds an `authenticate-oidc` default action in front of the existing forward rule.
3. Updates `environments.yaml` to set `auth.mode: alb` (and optionally `access_control`).

**Prerequisite:** deploy Relay with a certificate first — either by setting
`deployment.private_hosted_zone_id`/`private_hosted_zone_name` or
`deployment.certificate_arn` in `environments.yaml` and running
`scripts/relay-deploy.sh`. Without an HTTPS listener the script will exit with a
clear error.

### GitHub example

```bash
# Register an OAuth App at https://github.com/settings/applications/new
# Homepage URL: https://relay.corp.example.internal
# Callback URL:  https://relay.corp.example.internal/oauth2/idpresponse

export RELAY_OIDC_CLIENT_SECRET="<your-github-client-secret>"

./scripts/relay-setup-oidc.sh \
  --client-id  "<your-github-client-id>" \
  --scopes     "read:user user:email" \
  --allowed-users "octocat,monalisa"

# After the script completes, redeploy the compute stack so the container picks
# up RELAY_AUTH_MODE=alb. It is a task-definition env var (not baked into the
# image), so no image rebuild is needed — the redeploy mints a new task-def
# revision and rolls the task:
RELAY_STACK_SELECTOR=compute ./scripts/relay-deploy.sh
```

### Custom IdP example

```bash
./scripts/relay-setup-oidc.sh \
  --idp               custom \
  --client-id         "<id>" \
  --client-secret     "<secret>" \
  --issuer            "https://accounts.example.com" \
  --authorization-endpoint "https://accounts.example.com/oauth2/authorize" \
  --token-endpoint    "https://accounts.example.com/oauth2/token" \
  --user-info-endpoint "https://accounts.example.com/oauth2/userinfo" \
  --scopes            "openid profile email"
```
