# Relay — Operate an Incident Prompt

You are helping the user work a live incident using the Relay dashboard and HTTP API. This covers the big-board, incident lifecycle actions (acknowledge/resolve/ignore/route), the AI briefing pack, and the Settings screen for integration credentials.

Canonical reference: [`docs/operate.md`](../docs/operate.md).

---

## Accessing the dashboard

The dashboard URL is the `DashboardUrl` output from the deployed stack:

```bash
jq -r '.RelayComputeStack.DashboardUrl' cdk.outputs.json
```

Locally (demo mode or compose): `http://localhost:8080/`

**Auth modes:**

| Mode | Behavior |
|------|----------|
| `none` (default) | Read-only; all write endpoints return 403 |
| `alb` | OIDC authentication enforced by the ALB; writes enabled |
| `dev` | Fixed dev user injected; writes enabled (never in production) |

Write operations (acknowledge, resolve, ignore, route) require `alb` or `dev` mode.

---

## The fleet big-board

The landing view is a tile grid — one tile per tracked deployment, color-coded by current severity:

- **Green** — healthy, no open incidents
- **Yellow/Orange** — active incident at lower severity
- **Red** — active SEV1/SEV2 incident, or no-signal (app is silent)

The board updates live via a server-sent events (SSE) stream (`GET /stream`). No page reload needed.

**Environment filter:** The top strip carries a sticky `ALL / prod / test / dev` lens. It scopes the fleet board, incidents list, and metrics to a single environment. The selection persists across reloads.

**Click any tile** to open the tile detail drawer: current on-call (primary/secondary/manager), org hierarchy, AWS resource tags, and open incidents for that deployment.

---

## Incident lifecycle

```
TRIGGERED → ACKNOWLEDGED → (ESCALATED) → RESOLVED → CLOSED
```

| State | Meaning |
|-------|---------|
| `TRIGGERED` | Alarm received; escalation policy started |
| `ACKNOWLEDGED` | Claimed; escalation halted |
| `ESCALATED` | No ack within step timeout; next step fired |
| `RESOLVED` | Closed; external tickets auto-closed |
| `CLOSED` | Terminal; contributes to MTTR metrics |

---

## Actions on an incident

Open the incident from the tile drawer or the Incidents list to access the detail view.

### Acknowledge

Cancels the pending escalation timer — no further pages are sent. The incident moves to `ACKNOWLEDGED`.

**UI:** Click **Acknowledge** in the incident detail view.

**API:**

```bash
curl -X POST "http://<DashboardUrl>/incidents/<id>/acknowledge"
```

### Resolve

Closes the incident, triggers automatic closure of any linked GitLab issue or ServiceNow record, and drives MTTR metrics. Moves the incident to `CLOSED`.

**UI:** Click **Resolve** in the incident detail view.

**API:**

```bash
curl -X POST "http://<DashboardUrl>/incidents/<id>/resolve"
```

### Ignore

Creates a persistent ignore rule pre-filled from this incident (precise match by default; you can broaden to `alarm_name_prefix` or whole-app/env before saving). Auto-resolves the triggering incident. Future alarms matching the rule are dropped at the Node before they become incidents — no page, no ticket, no federation.

**UI:** Click **Ignore…** in the incident detail view, adjust the match criteria, save.

**API:**

```bash
curl -X POST "http://<DashboardUrl>/incidents/<correlation_id>/ignore" \
  -H "Content-Type: application/json" \
  -d '{"note": "Low-CPU idle alarm — no action needed"}'
```

### Route

Creates a routing rule pre-filled from this incident's alarm. Unlike Ignore, Route does **not** auto-resolve the current incident — it only affects future alarms that match.

**UI:** Click **Routing…** (same panel as Ignore, toggled).

**API:**

```bash
curl -X POST "http://<DashboardUrl>/incidents/<id>/route" \
  -H "Content-Type: application/json" \
  -d '{"priority": 10, "severity": "SEV2", "escalation_policy": "p2-high"}'
```

---

## Reading the AI briefing pack and AAR

When `RELAY_AI_ENABLED=true`, Relay asynchronously generates an AI briefing (t=0) and after-action review (post-resolution). These are clearly labeled AI-generated and attached after the fact — paging is never delayed waiting for AI.

```bash
# AI briefing pack (available shortly after TRIGGERED)
curl "http://<DashboardUrl>/incidents/<id>/brief"

# After-action review (available after RESOLVED)
curl "http://<DashboardUrl>/incidents/<id>/aar"
```

Both are also visible in the incident detail view in the dashboard.

---

## Rules screen

The **Rules** nav item shows two tables: **Routing rules** (expanded by default) and **Ignore rules** (collapsed, showing aggregate drop count in the header). From here:

- View match counts per rule to see which rules are actually firing.
- Create, edit, enable/disable, or delete routing and ignore rules.
- Use the **Download YAML** button to regenerate a `rules:` or `ignore:` block you can paste back into `routing.yaml` to re-sync Git with the live DynamoDB state.
- Check the **deviation banner** — it appears when the live DB rules differ from the `routing.yaml` seed loaded at boot.

---

## Viewing who is on call

```bash
curl "http://<DashboardUrl>/oncall"
# Returns the current on-call person per role (primary, secondary, manager)
```

---

## Metrics

```bash
curl "http://<DashboardUrl>/metrics"
# Returns MTTR, time-to-ack, incident counts by severity
```

The **Metrics** tab in the dashboard shows the same data. Apply the environment filter to scope KPIs to one environment.

---

## Settings screen — wiring integrations

Integration credentials are stored in DynamoDB (masked on read). No redeploy is needed to update them.

| Integration | Action |
|---|---|
| MS Teams webhook | `PUT /settings/teams-webhook` or Settings screen |
| Test Teams webhook | `POST /settings/teams-webhook/test` |
| GitLab token | `PUT /settings/gitlab-token` or Settings screen |
| Test GitLab token | `POST /settings/gitlab-token/test` |

The webhook URL and token are read fresh on every event — updates take effect immediately with no restart.

> Note: GitLab and ServiceNow integrations are marked "pending validation" in the current release. See [`docs/integrations.md`](../docs/integrations.md) for current status.

---

## Useful API endpoints (quick reference)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/incidents` | Open incidents |
| GET | `/incidents/history` | All incidents including resolved/closed |
| GET | `/incidents/<id>` | Incident detail + full timeline |
| GET | `/fleet` | All fleet tiles |
| GET | `/oncall` | Who is on call right now |
| GET | `/metrics` | MTTR, time-to-ack, incident counts |
| GET | `/routing-rules` | Live routing rules with match counts |
| GET | `/rules` | Live ignore rules with trigger counts |
| GET | `/health` | Container liveness check |

Full API reference: [`docs/operate.md`](../docs/operate.md).
