# Relay documentation capture — journey catalog

This is the **source of truth** for documentation screenshots and how-to videos.
Screenshots (Phase 3) and videos (Phase 4) both derive from the same journeys
defined here — one artifact, two fidelities.

Each journey lists: audience, backing doc page(s), the exact routes/SPA views and
actions, the demo-data prerequisites, and what a reader should take away.

## How captures are produced

- **Tool:** Playwright (headless Chromium), deterministic and scripted — no LLM in
  the loop. See `README.md` in this folder for how to run.
- **Target:** the offline demo container (`RELAY_DEMO=true docker compose up`),
  which self-populates a generic government-agency fleet (~39 tiles, 30 contacts,
  routing rules, a weekly schedule) with no AWS account. Auth is `dev` mode so
  write actions (ack/resolve/rules/schedule) don't 403.
- **Determinism:** the seed world is Faker-seeded, so the org tree, contacts, and
  tiles are identical run to run. The one moving part is the incident drip
  (`RELAY_DEMO_MODE=drip`); the capture harness seeds its own incidents via the
  API and does not depend on drip timing. For fully frozen captures, run with
  `RELAY_DEMO_MODE=off` and let the harness fire alarms itself.
- **Public-OSS safety:** the demo agency is generic and unnamed; no real account
  IDs, hostnames, or agency names appear. Account IDs in the seed are obviously
  fake (`111111111111` / `222222222222`).

## Screen inventory (ground-truth, from `src/relay/hub/app.py` + dashboard modules)

Single-page app; hash routes drive the left-nav views. Two overlays (drawers).

| SPA view | Hash route | Backing data |
|----------|-----------|--------------|
| Fleet big-board (landing) | `#/fleet` | `GET /fleet`, `GET /fleet/rollup`, SSE `GET /stream` |
| Incidents (Open / History tabs) | `#/incidents` | `GET /incidents`, `GET /incidents/history` |
| Metrics | `#/metrics` | `GET /metrics` |
| Contacts | `#/contacts` | `GET /contacts` |
| On-call | `#/oncall` | `GET /oncall` |
| Schedule (week grid) | `#/schedule` | `GET /schedule?week=`, `GET /schedule/overrides` |
| Rules (Ignore + Routing tables) | `#/rules` | `GET /routing-rules`, `GET /rules`, `*/deviation` |
| Maintenance | `#/maintenance` | synthetic-incident + purge tools |
| Settings | `#/settings` | `GET /settings` (secrets masked) |
| Tile detail drawer | (overlay on tile click) | `GET /fleet/tile?account_id=&app_name=`, `GET /oncall` |
| Incident detail drawer | `#/incident/<id>` (overlay) | `GET /incidents/{id}` + `/flow` `/brief` `/aar` |

Environment lens (`ALL / prod / test / dev`) is a sticky top-strip control present
on every view and composes with each screen's own filters.

---

## A. Setup & configuration journeys

Most of these are **terminal-only** (install, deploy, provision) and produce no
browser screenshots — the *act* of deploying is CLI. Their payoff screen is the
resulting dashboard, already covered by the operational journeys. They are listed
so the docs team knows which pages get terminal-cast/asciinema treatment instead
of browser captures.

| # | Journey | Audience | Doc | Capture type |
|---|---------|----------|-----|--------------|
| A1 | Install the toolchain (one-liner `install.sh`) | First-time installer | `install.md` | terminal |
| A2 | Run from a published artifact (compose / wheel) | Evaluator | `install.md` | terminal → dashboard |
| A3 | Manual audit-friendly install | Locked-down installer | `install.md` | terminal |
| A4 | Preflight readiness check | Any operator | `install.md` | terminal |
| A5 | Update an existing install | Existing operator | `install.md` | terminal |
| A6 | Fresh team deploy (Node + local Hub) | Team operator / SRE | `deploy.md` | terminal → dashboard |
| A7 | Scoped image redeploy (`compute` only) | Team operator | `deploy.md` | terminal |
| A8 | Deploy a federated Hub (org NOC) | Central SRE | `deploy.md` | terminal → big-board |
| A9 | Locked-down deploy (`relay-deploy-direct.sh`) | Regulated operator | `deploy.md` | terminal |
| A10 | Terraform / Terragrunt provisioning | Terraform team | `deploy.md` | terminal |
| A11 | Evaluation: provision data plane + local Hub on EC2 | Evaluator / SRE | `local-dev.md` | terminal → dashboard |
| A12 | Local dev inner loop (`docker compose up`, `relay-fire.sh`) | Contributor | `local-dev.md` | terminal → dashboard |
| A13 | Author config-as-code (escalation + routing YAML) | Team operator | `configure.md` | terminal/editor |
| A14 | BYOR / BYOV (paste inline IAM policy) | Platform/infra eng | `byor.md` | terminal + AWS console |

**Configuration journeys with a real Relay UI** (captured as browser shots — see
section B for the operational overlap):

- **A15 — Configure integrations via Settings** (`#/settings`): paste Teams
  webhook / GitLab token / ServiceNow creds at runtime, no redeploy. Backs
  `integrations.md`, `configure.md`. Note: in the current release GitLab/ServiceNow
  *saving* may be gated by `RELAY_INTEGRATIONS_LOCKED`; the fields still render for
  the screenshot. → captured as **S-SETTINGS**.
- **A16 — Manage routing & ignore rules in the UI** (`#/rules`): live rules with
  trigger counts, deviation banner, download-YAML round-trip. Backs `configure.md`.
  → captured as **S-RULES** / **S-RULES-DEVIATION**.

---

## B. Operational / day-to-day journeys (primary browser captures)

Ordered by capture priority. Each maps to one or more named screenshots (`S-*`)
and, in Phase 4, a paced video (`V-*`).

### B1 — Scan fleet health on the big-board  ★ hero shot
- **Audience:** on-call, team lead, central SRE · **Doc:** `operate.md`, `fleet-team-mockup.md`
- **View:** `#/fleet` (landing). Env lens in top strip; "Incidents only" filter.
- **Steps:** open dashboard → read tile colors + liveness badges (LIVE/STALE/LOST)
  → toggle env lens (ALL → prod) → toggle "Incidents only".
- **Prereq state:** multi-line org tree (present in seed), a mix of green / degraded
  / red / no-signal tiles, at least one open incident so red tiles show `● N·SEVx`.
- **Reader learns:** live always-on view of the whole fleet; dead apps stay visible;
  env is the outer container; org tree nests dynamically.
- **Screenshots:** `S-FLEET-ALL`, `S-FLEET-PROD` (env lens applied),
  `S-FLEET-INCIDENTS-ONLY`.

### B2 — Open a tile's detail drawer
- **Audience:** on-call, team lead, central SRE · **Doc:** `operate.md`, `scheduling.md`
- **View:** tile drawer overlay (click a tile). Backed by `/fleet/tile`, `/oncall`.
- **Steps:** click a degraded/red tile → read On-call now (primary/secondary/manager)
  → org hierarchy + metadata → AWS resource tags → open incidents list.
- **Prereq state:** a tile whose deployment has on-call resolved (needs the schedule
  seeded — see B8), org-path metadata, and ≥1 open incident.
- **Reader learns:** one click gives a full context card; on-call resolves live.
- **Screenshots:** `S-TILE-DRAWER`.

### B3 — Respond to an incident (ack / resolve)  ★
- **Audience:** on-call · **Doc:** `operate.md`, `scheduling.md`, `integrations.md`
- **View:** `#/incidents` list → incident drawer (`#/incident/<id>`).
- **Steps:** open Incidents (Open tab) → open a TRIGGERED incident → read Timeline +
  Properties + AI briefing pack → click Acknowledge (timer cancels) → click Resolve.
- **Prereq state:** ≥1 TRIGGERED incident with a timeline (harness fires alarms);
  write enabled (dev mode). AI briefing shows deterministic fallback if AI disabled.
- **Reader learns:** lifecycle TRIGGERED → ACKNOWLEDGED → RESOLVED → CLOSED; ack stops
  paging; resolve closes tickets + feeds MTTR.
- **Screenshots:** `S-INCIDENTS-LIST`, `S-INCIDENT-DETAIL`, `S-INCIDENT-ACK`,
  `S-INCIDENT-BRIEF` (AI pane).

### B4 — Silence noise by ignoring an alarm
- **Audience:** on-call, team lead · **Doc:** `operate.md`
- **View:** incident drawer → **Ignore…** form (`POST /incidents/{id}/ignore`).
- **Steps:** open a noisy incident → Ignore… → form pre-filled (precise match) →
  optionally broaden to prefix/app → save (creates rule + auto-resolves).
- **Prereq state:** an open incident; write enabled.
- **Reader learns:** ignore drops future matches at the Node — no page/ticket/metrics.
- **Screenshots:** `S-INCIDENT-IGNORE-FORM`.

### B5 — Create a routing rule from an incident
- **Audience:** team lead, central SRE · **Doc:** `operate.md`, `scheduling.md`
- **View:** incident drawer → **Routing…** form (`POST /incidents/{id}/route`).
- **Steps:** open incident → Routing… → adjust priority / match / severity /
  escalation policy / streams (team|central|both) → save (future alarms only).
- **Prereq state:** open incident; escalation policies configured (seed has them).
- **Reader learns:** routing rules don't resolve the incident; act only on future alarms.
- **Screenshots:** `S-INCIDENT-ROUTE-FORM`.

### B6 — Manage routing & ignore rules  ★  (also config journey A16)
- **Audience:** team lead, central SRE · **Doc:** `operate.md`, `configure.md`
- **View:** `#/rules` (two accordions: Ignore collapsed-first, Routing expanded-below).
- **Steps:** open Rules → Ignore accordion header shows rule count + aggregate alarms
  dropped (collapsed by default) → read Routing table below (priority/match/severity/
  policy/streams/match count/enabled) → observe deviation banner → expand the Ignore
  accordion to reveal its table (match/outcome/trigger count) → Download YAML.
- **Prereq state:** several live rules with non-zero counts (seed has 6 routing rules);
  a divergence from `routing.yaml` so the deviation banner shows (harness creates one
  rule via API to force deviation); write enabled.
- **Reader learns:** ignore and routing are separate pipeline stages; runtime rules live
  in DynamoDB; counts show effectiveness; GitOps round-trip via Download YAML.
- **Screenshots:** `S-RULES`, `S-RULES-DEVIATION`, `S-RULES-IGNORE` (accordion expanded).

### B7 — Set up a contact and availability
- **Audience:** on-call (self-service), team lead · **Doc:** `scheduling.md`, `operate.md`
- **View:** `#/contacts` + availability grid.
- **Steps:** open Contacts → create/edit a contact (name/email/phone, role eligibility)
  → set availability grid slots → set OOO range → send test page.
- **Prereq state:** several contacts (seed has 30); write enabled.
- **Reader learns:** identity is self-service (not AD); 3 shifts × 3 roles model.
- **Screenshots:** `S-CONTACTS`, `S-CONTACT-AVAILABILITY`.

### B8 — Build the weekly on-call schedule  ★
- **Audience:** team lead · **Doc:** `scheduling.md`, `operate.md`
- **View:** `#/schedule` (week grid). `POST /schedule/auto`.
- **Steps:** open Schedule (current week) → Auto-schedule → review grid → note red gap
  cells (uncovered slot/role) → resolve via availability or override.
- **Prereq state:** contacts with partial availability so auto-schedule yields a grid
  with some red gaps (harness runs auto-schedule for the current week — seed produces
  ~10 gaps). Write enabled.
- **Reader learns:** one-click auto-scheduling, no double-booking; gaps are a
  first-class warning to fix before the week.
- **Screenshots:** `S-SCHEDULE`, `S-SCHEDULE-GAPS` (gap cells in view),
  `S-ONCALL` (who's on now).

### B9 — Review incident metrics / KPIs
- **Audience:** team lead, central SRE, management · **Doc:** `operate.md`
- **View:** `#/metrics`.
- **Steps:** open Metrics → read MTTR / time-to-ack / counts by severity+period →
  change env lens to recompute per-environment.
- **Prereq state:** a history of resolved/closed incidents across severities+envs
  (harness fires + resolves a handful, incl. flagged synthetic which count in metrics).
- **Reader learns:** resolve feeds MTTR; env lens recomputes KPIs.
- **Screenshots:** `S-METRICS`, `S-METRICS-PROD`.

### B10 — Configure integrations via Settings  (config journey A15)
- **Audience:** team lead, central SRE · **Doc:** `integrations.md`, `configure.md`
- **View:** `#/settings`.
- **Steps:** open Settings → Teams webhook field + Test → GitLab token + Test token →
  ServiceNow instance/user/pass + Test connection. (Saving may be gated by
  `RELAY_INTEGRATIONS_LOCKED` in this release; fields still render.)
- **Prereq state:** running Hub, write enabled. Tokens shown masked.
- **Reader learns:** integrations are optional, runtime-configured, never block deploy;
  creds stored server-side encrypted in DynamoDB, masked on read.
- **Screenshots:** `S-SETTINGS`.

### B11 — Maintenance: synthetic incidents + purge
- **Audience:** operator, contributor · **Doc:** `operate.md` (maintenance)
- **View:** `#/maintenance`.
- **Steps:** open Maintenance → fire a synthetic test incident (app/severity/env) →
  review purge tool (before/after toggle, synthetic-only) → preview.
- **Prereq state:** write enabled; some incidents present so purge preview is non-trivial.
- **Reader learns:** synthetic incidents are flagged + counted in metrics; purge is
  temporally bounded and refuses unbounded deletes.
- **Screenshots:** `S-MAINTENANCE`.

---

## Capture manifest (screenshot → doc page mapping)

Screenshots land in `docs/assets/screenshots/<page>/<name>.png` so doc authors can
drop them into the matching page. Proposed mapping:

| Screenshot | Doc page(s) | Journey |
|-----------|-------------|---------|
| `S-FLEET-ALL`, `S-FLEET-PROD`, `S-FLEET-INCIDENTS-ONLY` | operate.md, index.md | B1 |
| `S-TILE-DRAWER` | operate.md | B2 |
| `S-INCIDENTS-LIST`, `S-INCIDENT-DETAIL`, `S-INCIDENT-ACK`, `S-INCIDENT-BRIEF` | operate.md | B3 |
| `S-INCIDENT-IGNORE-FORM` | operate.md | B4 |
| `S-INCIDENT-ROUTE-FORM` | operate.md | B5 |
| `S-RULES`, `S-RULES-IGNORE`, `S-RULES-DEVIATION` | operate.md, configure.md | B6 / A16 |
| `S-CONTACTS`, `S-CONTACT-AVAILABILITY` | scheduling.md | B7 |
| `S-SCHEDULE`, `S-SCHEDULE-GAPS`, `S-ONCALL` | scheduling.md | B8 |
| `S-METRICS`, `S-METRICS-PROD` | operate.md | B9 |
| `S-SETTINGS` | integrations.md, configure.md | B10 / A15 |
| `S-MAINTENANCE` | operate.md | B11 |

## Video plan (Phase 4)

Same journey scripts, run with Playwright `recordVideo` + paced steps. Proposed
how-to videos (one per high-value journey, ~30–60s each):

- `V-FLEET-TOUR` — big-board tour + env lens + tile drawer (B1+B2)
- `V-INCIDENT-RESPONSE` — open → ack → resolve, with AI briefing (B3)
- `V-NOISE-CONTROL` — ignore + routing rule from an incident, then Rules screen (B4+B5+B6)
- `V-SCHEDULING` — auto-schedule → resolve a gap → who's on now (B8)
- `V-SETTINGS` — wire an integration (B10)

Video files are written to `tools/docs-shots/videos/<journey-id>-<label>.webm`
(e.g. `B3-V-INCIDENT-RESPONSE.webm`). The id prefix keeps journeys that share a
label distinct. Videos are git-ignored — publish to the docs CDN / release assets.

Captions/narration approach TBD (Playwright records silent video; overlay captions
in post, or add an on-screen step banner the script toggles). The `beat()` helper
already paces steps (1.2s) when `--video` is set so viewers can follow. Decide the
caption approach before recording the final set.
