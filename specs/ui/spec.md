# Domain Spec: UI / Dashboard

**Owns:** the operator-facing web UI — the single-page `hub/dashboard.html` that
renders the big board, incidents, schedule, contacts, rules, metrics, and settings.

**Primary code:** `src/relay/hub/dashboard.html` (one ~4,500-line self-contained
page; inline markup/styles/JS). Served by `hub/app.py`. **Design contract:**
[`design-language.md`](design-language.md) — **binding** for every UI change.
**Related:** every domain with a UI surface describes its *data contract* in its
own spec; this spec + the design language own *look and behavior*.

## What it does now

A full-bleed, dark, dense Industrial Command Center dashboard with these views:

- **Big Board** — grid of per-app tiles (status LED + uptime), liveness-colored.
- **Incidents** — austere full-width table; click a row → incident drawer
  (timeline, properties, actions: ack / resolve / route / ignore / add responder).
- **Schedule** — role-aware grid with gap highlighting.
- **Contacts** — searchable directory (CRUD).
- **Rules** — UI-managed routing + ignore rules (DB-backed, deviation banner).
- **Metrics** — MTTR / time-to-ack / counts (flags synthetic data).
- **Settings** — GitLab token, ServiceNow creds, Teams webhook; Test buttons show raw responses.
- **Maintenance** — synthetic incident trigger + temporal purge.

## Invariants (from the design language — see that file for the full list)

- Full-bleed, dark-first, max data density; **no** rounded/pastel/shadow/centered-fixed-width.
- Monospace for all data/numbers; saturated semantic colors (red/yellow/green) for status.
- **Two palettes, non-overlapping:** Westport teal for chrome/identity
  (`docs/stylesheets/brand.css`), industrial palette for operational surfaces.
  Status is never teal.
- **No hidden critical info** behind hover/tooltips.

## How UI changes are verified

1. Conform to [`design-language.md`](design-language.md) — reviewed as a checklist.
2. **Exercise it in a browser** — `/dod` requires observing the real UI, not just
   green unit tests. A UI surface that violates the "never" list or mis-colors
   status is NEEDS-ACTION.

## In flight (the trial)

**[#20](https://github.com/Westport-Partners/relay/issues/20) — incident process-flow
timeline view.** New surface inside the incident drawer: an **escalation ladder
spine** (primary → secondary → manager; each step's notify-streams + timeout)
with the **actual events slotted onto it** by timestamp. Reached steps filled,
unreached steps ghosted; graceful fallback to today's flat timeline list when no
flow data exists. Data comes from [observability](../observability/spec.md)
(`GET /incidents/{id}/flow` or enriched detail). Visual must follow the design
language: monospace timestamps, vertical "now"/progress treatment consistent
with the schedule's red-line idiom, status colors for ack/escalate/resolve.

### Target sketch (expected ladder vs. actual)

```
 INCIDENT #4821  SEV2  api-gateway / prod                         [ACK] [RESOLVE]
 ───────────────────────────────────────────────────────────────────────────────
 EXPECTED LADDER                         ACTUAL
 ▌ STEP 1  primary    sms+email  5m   →  ● 14:02:11  page_sent  jdoe, asmith
 ▌                                       ○ 14:07:11  no ack — escalated
 ▌ STEP 2  secondary  sms+email  5m   →  ● 14:07:11  page_sent  rlee
 ▌                                       ● 14:09:43  ACK by rlee
 ▌ STEP 3  manager    sms        —    ░  (not reached — ghosted)
 ───────────────────────────────────────────────────────────────────────────────
                                         ● 14:31:02  RESOLVED by rlee
```

(Filled `●` = occurred; `○` = transition; `░` = ghosted/unreached. Red left-border
on active step, green on the acked step. Monospace timestamps.)

## Out of scope (non-goals)

- Manual schedule-override click-to-assign UI (status.md §3 roadmap, separate issue).
- Manual "start incident" button (status.md §1, [issue #24](https://github.com/Westport-Partners/relay/issues/24)).
