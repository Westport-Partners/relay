# On-Call Scheduling and Escalation

Relay manages who gets paged through two interlocking systems: a **schedule** (who is covering each shift) and an **escalation policy** (the ordered sequence of pages if nobody acknowledges). This document covers both and how they connect.

---

## The On-Call Model

### Shifts

The day is divided into three fixed 8-hour shifts, interpreted in the team timezone (`RELAY_TZ`):

| Shift | Hours (local time) |
|-------|--------------------|
| Night | 00:00 – 08:00 |
| Day | 08:00 – 16:00 |
| Evening | 16:00 – 24:00 |

Each (day, shift) slot is covered by **one person per role**:

| Role | Responsibility |
|------|---------------|
| **Primary** | First to be paged |
| **Secondary** | Paged if primary does not acknowledge |
| **Manager** | Escalation backstop; paged last |

### Availability and eligibility

Each contact sets:

- **Availability grid** — which (day-of-week, shift) slots they are willing to cover, configured on the Schedule or Contacts screen (`PUT /availability/{contact_id}`)
- **Out-of-office (OOO) range** — a single date range during which they are excluded from paging, regardless of the grid
- **Role eligibility** — which roles (primary, secondary, manager) they can fill

These settings are stored in DynamoDB and take effect immediately. They are not version-controlled config files.

---

## Auto-Schedule

`POST /schedule/auto` generates the schedule for the current week (or a specified week) in a single step. The algorithm:

- Considers only contacts whose availability grid covers the slot and who are not in an OOO period
- Enforces **no double-booking**: the primary and secondary for the same (day, shift) are always different people
- Balances load across eligible contacts within the week (greedy, single-week balance; multi-week fairness is on the roadmap)

You can trigger auto-schedule from the **Schedule** screen or directly via the API. It overwrites the generated schedule; any existing overrides (see below) are preserved on top.

### Gap highlighting

After generation, any (day, shift, role) slot that has no assigned contact is flagged **red** in the schedule grid. A gap is a first-class operational warning — it means an alarm in that window would have no one to page for that role. Gaps should be resolved before the week starts, either by expanding availability or adding a manual override.

---

## Ad-Hoc Overrides ("cover-me")

Overrides let any contact be substituted into a specific (date, shift, role) slot on top of the generated schedule. Common uses: unexpected illness, time-off that was not captured in the OOO range, or a requested swap.

| Action | Endpoint |
|--------|---------|
| Create or update an override | `PUT /schedule/override` |
| Delete an override | `DELETE /schedule/override` |
| List all active overrides | `GET /schedule/overrides` |

Overrides are respected by paging resolution — when Relay resolves who is on call for a given role at a given moment it applies overrides before the base schedule. Overrides are shown distinctly on the schedule grid (the backend and API are fully implemented; direct cell-click authoring in the UI is on the roadmap).

---

## Who Is On Call Now

`GET /oncall` returns the currently on-call contact for each role, resolved against the active schedule and any overrides, in local time (`RELAY_TZ`).

The **tile detail drawer** (click any fleet tile) also shows the on-call snapshot for that deployment's owning team. For a federated hub this snapshot is pushed by the team's Node and is updated with each heartbeat.

`GET /schedule` returns the full week grid. Pass `?week=YYYY-MM-DD` (Monday of the week) to view future or past weeks.

---

## Escalation Policies

An escalation policy is an ordered list of **steps**. Each step specifies:

- **Roles and/or contacts to page** — typically a role (`primary`, `secondary`, `manager`); an explicit `contact_id` can be used as an escape-hatch for a named individual
- **Timeout** — how long to wait for an acknowledgement before advancing to the next step

Policies are defined in `escalation.yaml` (see `configure.md` for the full YAML schema).

### Key properties

**Policies page roles, not people.** The mapping from role → person is resolved at page time from the live schedule. This means a policy written once stays correct as the schedule changes.

**Acknowledging stops escalation.** The moment an incident is acknowledged (via the dashboard or `POST /incidents/{id}/acknowledge`), the pending escalation timer is cancelled. No further steps fire.

**Timers are durable.** Escalation deadlines are stored in DynamoDB and swept by the container on roughly a 30-second cycle. A container restart or redeploy does not lose a timer mid-incident — the sweep picks up where it left off.

### Example policy walk-through

```
Step 1: page role=primary, timeout=10m
Step 2: page role=secondary, timeout=15m
Step 3: page role=manager
```

1. Alarm fires at 14:32. Step 1 pages the on-call primary.
2. No acknowledgement by 14:42. Step 2 pages the on-call secondary.
3. No acknowledgement by 14:57. Step 3 pages the on-call manager.
4. Manager acknowledges at 15:01. Escalation stops; incident moves to `ACKNOWLEDGED`.

---

## How Routing, Severity, and Escalation Connect

The full flow from alarm to page:

```
Alarm received
    │
    ▼
Routing rule (routing.yaml)
    │  picks: severity tier + escalation policy name
    ▼
Escalation policy (escalation.yaml)
    │  step 1: page role=primary
    ▼
Schedule + overrides
    │  resolve: primary role → contact name + channel
    ▼
Page sent (Teams, SMS, etc.)
    │
    ├── Acknowledged within timeout → escalation stops
    │
    └── Timeout elapsed → advance to step 2 → repeat
```

**Severity tiers** (SEV1–SEV4) are assigned by the routing rule and influence default paging urgency. A SEV1 routing rule typically points to a policy with short timeouts; a SEV4 may use a policy that only pages primary with a longer window.

Routing rules are config-as-code in `routing.yaml`. See `configure.md` for the YAML structure and examples.

---

## Related

- **Configure escalation policies and routing rules** → `configure.md`
- **Dashboard screens (Schedule, Contacts, Settings)** → `operate.md`
- **Who's on call right now (API)** → `GET /oncall` (see the endpoint table in `operate.md`)
