# Domain Spec: UI / Dashboard

**Owns:** the operator-facing web UI — the single-page dashboard that renders the
big board, incidents, schedule, contacts, rules, metrics, and settings.

**Primary code:**
- **Behavior:** `src/relay/hub/dashboard_modules/` — native **ES modules** (one per
  view/drawer/concern, plus `helpers`/`state`/`constants` foundations and a `main.js`
  entry). The browser loads them directly via `<script type="module">`; the Hub
  serves them read-only at `/static/dashboard/`. **No build step, no bundler, no
  npm, no CDN** — vanilla browser ESM, offline, one wheel/container artifact.
- **Shell + CSS:** `src/relay/hub/dashboard_parts/` — ordered HTML/CSS fragments
  (document open, `<style>` sheet, body shell, document close) listed in
  `manifest.txt` and **assembled at serve time** by `hub/app.py`
  (`_render_dashboard_html`) into the page shell, which ends with
  `<script type="module" src="/static/dashboard/main.js">`.

**Design contract:**
[`design-language.md`](design-language.md) — **binding** for every UI change.
**Related:** every domain with a UI surface describes its *data contract* in its
own spec; this spec + the design language own *look and behavior*.

## What it does now

A full-bleed, dark, dense Industrial Command Center dashboard with these views:

- **Big Board** — per-app tiles **grouped by org hierarchy** (product-line section
  headers carrying a rollup status LED + `N red · N degraded · N unknown · N green`
  counts, worst-group-first, org-less tiles under "Ungrouped"). Tiles wrap the full app
  name (no truncation) and carry at-a-glance indicators: open-incident count + worst
  severity, on-call-gap warning, environment chip (non-prod emphasized), owner. Each
  group's grid is **space-adaptive** — `auto-fit` + a count-aware per-group `--tile-min`
  so few tiles grow to fill the row and many pack dense (no phantom empty columns),
  reflowing on resize. Group status/counts prefer `GET /fleet/rollup`, falling back to
  client-side compute. Click a tile → the detail drawer.
- **Incidents** — austere full-width table; click a row → incident drawer
  (timeline, properties, actions: ack / resolve / route / ignore / add responder).
- **Schedule** — role-aware grid with gap highlighting.
- **Contacts** — searchable/filterable directory (CRUD): text + role + available-only
  filters, per-contact role-eligibility badges, optional eligible-roles at create,
  inline availability expander (with close button).
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

## File structure (for editing)

**Behavior — ES modules in `src/relay/hub/dashboard_modules/`.** One module per
view/drawer/concern with explicit `import`/`export`. To change a view, edit its
module; its dependencies are declared at the top of the file, so an edit is bounded
and a module **cannot** silently depend on another view's internals.

- **Foundations (leaves):** `constants.js` (status taxonomy), `helpers.js` (pure
  presentation: `esc`, `fmtAge`, `fmtTime`, `fmtDetail`, `metaValueHtml`,
  `buildTile`, …), `state.js` (the few genuinely cross-module mutable globals as
  live-binding exports + setters — `CAN_WRITE`, `TEAM_TZ`, `tiles`, `activeFilter`,
  `activeView`, `escalationPolicies`).
- **Structure:** `auth.js`, `stream.js` (SSE), `fleet.js` (big board; org-grouped
  section render + per-group adaptive sizing + `/fleet/rollup` fetch),
  `fleet-groups.js` (pure leaf: `groupTiles(tiles, rollup)` → ordered org groups +
  Ungrouped, rollup-or-client status/counts), `router.js`
  (view-switch + hash deep-links), `main.js` (the entry module that runs init in
  order — loaded via `<script type="module">`).
- **Views / drawers / shared:** `incidents.js`, `incident-drawer.js`,
  `tile-drawer.js`, `contacts.js`, `metrics.js`, `oncall.js`, `settings.js`,
  `maintenance.js`, `schedule.js`, `rules.js`, `rule-forms.js`.

The full per-module export/import inventory and dependency graph is in
[`js-module-map.md`](js-module-map.md) — the source of truth for the JS layout.
Import cycles between views/drawers are expected and **safe** (every cross-module
reference is call-time, not load-time); ESM permits them.

**Shell + CSS — fragments in `src/relay/hub/dashboard_parts/`.** Four ordered
fragments listed in `manifest.txt`: `00-doc-open` (doctype/head), `01-styles`
(the `<style>` sheet), `02-body-shell` (the markup, ending with the module
`<script>` tag), `99-doc-close`. To change layout or styling, edit the relevant
fragment.

**Invariants** (locked by `tests/test_hub_dashboard.py::TestDashboardAssembly`):
the assembled shell is a single well-formed document with exactly one `<style>`
pair and exactly one `<script type="module">` tag (**no inline `<script>`**); the
module entry `main.js` ships in the package; and every relative `import` between
modules resolves to a real exported symbol.

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
