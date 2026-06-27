# Dashboard JS Module Map

**Status:** Step 1 deliverable of [#33](https://github.com/Westport-Partners/relay/issues/33)
(convert dashboard JS fragments to native ES modules).
**Source of truth for the refactor** — every later step ports against this map.
Derived by grepping the actual fragment code (comments stripped), **not** from the
nav-map comment in `10-preamble-navmap.js.part`.

## Scope

The dashboard JS lives as 14 `*.js.part` fragments in
`src/relay/hub/dashboard_parts/`, string-concatenated by
`hub/app.py::_render_dashboard_html()` into **one inline `<script>` with a single
shared global scope**. This map inventories, per fragment: declared symbols (→
`export`s), cross-fragment references (→ `import`s), mutable state read/written,
and top-level on-load side effects — then derives the module dependency graph and
the cycle breaks needed for the ESM port.

CSS (`01-styles`), the HTML shell (`00`, `02`, `99`) are **out of scope** — they
stay assembled. Only the 14 `.js.part` fragments become modules.

## Headline findings (read these first)

1. **Every cross-fragment reference is inside a function body or event-handler
   closure — there are ZERO top-level (init-time) cross-fragment references.**
   This is the single most important fact for the port: ES modules resolve
   `import` bindings lazily at *call* time, so the dependency cycles below are all
   **call-time-safe** and do **not** need to be physically broken to avoid a
   temporal-dead-zone crash. They only need to be *expressible* (ESM permits
   circular `import`s).
2. **Reassigned `let` globals cannot be ported as plain `export let`.** A consumer
   that does `import { CAN_WRITE }` gets a *live read-only binding*; the owning
   module reassigning `CAN_WRITE = true` updates it for readers (live bindings are
   fine to *read* across modules), but a consumer can never *write* it, and value
   semantics surprise people. All mutable cross-module state therefore moves into
   **`state.js` accessed via getters/setters or a single mutable object** (Step 2
   decision). The full list of reassigned-and-shared globals is in
   [§ Mutable state](#mutable-state).
3. **`escalationPolicies` is written from TWO fragments** (`32-view-rules` *and*
   `24-drawer-incident-detail` line 379). It is the clearest proof that mutable
   state must be centralized in `state.js`, not "owned" by whichever view
   declared it. See [§ Mutable state](#mutable-state).
4. **`esc()` is the universal dependency** — referenced (code-only) in 10 of 14
   fragments, 200+ call sites. It and the other pure formatters belong in a
   zero-dependency `helpers.js` that everything imports and that imports nothing.

## Proposed target module set

| Module | From fragment(s) | Exports | Imports |
|---|---|---|---|
| `constants.js` | 20 (the maps) | `STATUS_ORDER`, `STATUS_LABEL`, `MARKER` | (none) |
| `helpers.js` | 20 + `fmtTime`/`fmtDetail` (25) + `metaValueHtml` (23) | `esc`, `fmtAge`, `ageClass`, `abbrAccount`, `buildTile`, `fmtTime`, `fmtDetail`, `metaValueHtml` | `constants.js` (buildTile uses STATUS_LABEL/MARKER) |
| `state.js` | the mutable globals (10, 22, 23, 26, 31, 32) | accessors for `CAN_WRITE`, `AUTH_SUBJECT`, `TEAM_TZ`, `activeFilter`, `lastPingAt`, `activeView`, `incidentsTab`, `editingContactId`, `contactSort`, `rulesData`, `rulesFilterVal`, `routingRulesData`, `escalationPolicies`, `newRuleType`, `currentRole`, `currentWeekStart`; `tiles` (Map, never reassigned → export as-is) | (none) |
| `auth.js` | 10 (auth half) | `initAuth`, `renderHubScope`, `gateWrite` | `helpers` (esc), `state` (CAN_WRITE/AUTH_SUBJECT/TEAM_TZ) |
| `stream.js` | 10 (SSE/conn half) | `connect`, `checkPingAlive`, `setConnStatus` | `state` (tiles, lastPingAt), `fleet` (renderAll) |
| `fleet.js` | 21 | `renderAll`, `wireTileActivation` | `helpers`, `constants`, `state`, `tile-drawer` (openTile) |
| `router.js` | 22 | `navTo`, `handleHash`, `wireNav` | `state` (activeView) + each view's `loadX` |
| `incidents.js` | 23 | `loadIncidents`, `metaValueHtml`* | `helpers`, `state`, `incident-drawer` |
| `incident-drawer.js` | 24 | `openIncident`, `renderIncident`, `closeDrawer`, `drawer`, `drawerOverlay` | `helpers`, `state`, `fleet` (renderAll), `incidents`, `rule-forms` |
| `tile-drawer.js` | 25 | `openTile`, `renderTile`, `loadTileIncidents`, `fmtTime`*, `fmtDetail`* | `helpers`, `constants`, `incident-drawer` |
| `contacts.js` | 26 | `loadContacts`, `renderContacts`, `showContactForm`, `confirmDeleteContact` | `helpers`, `state`, `schedule` (SCHED_ROLES/labels, getThisMonday) |
| `metrics.js` | 27 | `loadMetrics` | `helpers`, `router` (navTo) |
| `oncall.js` | 28 | `loadOncall` | `helpers`, `state` (TEAM_TZ), `router` (navTo) |
| `settings.js` | 29 | `loadSettings`, `renderConfigCard`, `renderSettings` | `helpers`, `state` |
| `maintenance.js` | 30 | `loadMaintenance`, `renderMaintenance` | `state` (CAN_WRITE) |
| `schedule.js` | 31 | `loadSchedule`, `renderSchedule`, `getThisMonday`, `SCHED_*` consts | `helpers`, `state` |
| `rule-forms.js` | 33 | `ignoreRuleFormHtml`, `wireIgnoreRuleForm`, `routingRuleFormHtml`, `wireRoutingRuleForm` | `helpers`, `state`, `rules` (escalationPolicies via state) |
| `rules.js` | 32 | `loadRules`, `renderRulesSection`, `renderNewRuleForm`, `renderRulesTable` | `helpers`, `state`, `rule-forms` |
| `main.js` (entry) | new | — | wires init: `initAuth()`, `connect()`, `setInterval(checkPingAlive)`, `wireNav()`, `wireTileActivation()`, hashchange + `handleHash()` |

\* `metaValueHtml` is declared in fragment 23 but is a pure formatter used by 23,
24, 25 → **relocate into `helpers.js`** so the drawers don't import the incidents
view. Likewise `fmtTime`/`fmtDetail` are declared in fragment 25 but used by 24
→ **relocate into `helpers.js`**. These three relocations remove three otherwise-
awkward edges (see graph). This is the only symbol *movement* the port performs;
everything else stays in its current fragment's successor module.

## Per-fragment inventory

Legend: **Exports** = top-level declarations (would-be `export`s). **Imports** =
symbols referenced in code (comments stripped) that are declared in another
fragment, with the owning fragment. **State** = mutable globals read/written.
**On-load** = statements that execute at script-eval time (become explicit calls
from `main.js`).

### `10-preamble-navmap.js.part` (271 lines)
Two distinct concerns → splits into **`auth.js`** + **`stream.js`** + seeds
**`state.js`**. The 130-line nav-map comment at the top is deleted in Step 5.
- **Exports:** `CAN_WRITE`*, `AUTH_SUBJECT`*, `TEAM_TZ`*, `initAuth`,
  `renderHubScope`, `gateWrite`, `tiles`, `activeFilter`*, `lastPingAt`*,
  `PING_TIMEOUT_MS`, `connStatus`, `connBanner`, `setConnStatus`,
  `checkPingAlive`, `connect`. (`*` = reassigned → moves to `state.js`.)
- **Imports:** `esc` (20); `renderAll` (21).
- **State:** writes `CAN_WRITE`, `AUTH_SUBJECT`, `TEAM_TZ` (initAuth);
  `activeFilter` (filter-btn handler); `lastPingAt` (SSE handlers).
- **On-load:** `initAuth();` (182), `setInterval(checkPingAlive, 5_000)` (211),
  `connect();` (258), `.filter-btn` click wiring (263). → all move to `main.js`.

### `20-shared-helpers.js.part` (74 lines)
Becomes **`helpers.js`** (+ `constants.js` for the three maps).
- **Exports:** `STATUS_ORDER`, `MARKER`, `STATUS_LABEL` (→ `constants.js`);
  `fmtAge`, `ageClass`, `abbrAccount`, `buildTile`, `esc` (→ `helpers.js`).
- **Imports:** none (this is the root of the graph).
- **State / On-load:** none.

### `21-view-fleet-big-board.js.part` (68 lines) → `fleet.js`
- **Exports:** `renderAll` (+ the IIFE `wireTileActivation`).
- **Imports:** `tiles`, `activeFilter` (10/state); `STATUS_ORDER`, `buildTile`
  (20); `openTile` (25).
- **On-load:** `setInterval(renderAll, 10_000)` (52); `wireTileActivation()` IIFE
  (56). → move the IIFE's body to an exported `wireTileActivation()` called from
  `main.js`; the interval can stay module-local (pure timer, no ordering risk).

### `22-shell-...-router....js.part` (57 lines) → `router.js`
- **Exports:** `activeView`* (→ state), `navTo`, `handleHash` (+ nav-wiring IIFE).
- **Imports (call-time):** every view's loader — `loadIncidents`(23),
  `openIncident`(24), `loadContacts`(26), `loadMetrics`(27), `loadOncall`(28),
  `loadSettings`(29), `loadMaintenance`(30), `loadSchedule`(31), `loadRules`(32).
- **State:** writes `activeView`.
- **On-load:** `.nav-btn[data-view]` wiring (10), `hashchange` listener (54),
  `handleHash()` (56). → `wireNav()` + listener move to `main.js`.

### `23-view-incidents.js.part` (84 lines) → `incidents.js`
- **Exports:** `incidentsTab`* (→ state), `loadIncidents`, `metaValueHtml`
  (→ relocate to `helpers.js`).
- **Imports:** `abbrAccount`, `fmtAge`, `esc` (20); `openIncident` (24).
- **State:** writes `incidentsTab`.

### `24-drawer-incident-detail.js.part` (427 lines) → `incident-drawer.js`
- **Exports:** `drawer`, `drawerOverlay`, `closeDrawer`, `openIncident`,
  `renderIncident`.
- **Imports:** `CAN_WRITE` (state); `esc` (20); `renderAll` (21); `activeView`
  (state); `loadIncidents`, `metaValueHtml` (23/helpers); `fmtDetail`, `fmtTime`
  (25/helpers); `escalationPolicies`, `loadRules` (32/state); `routingRuleFormHtml`,
  `wireRoutingRuleForm` (33).
- **State:** **writes `escalationPolicies`** (line 379) — second writer.
- **On-load:** `document` keydown→Escape→`closeDrawer` (8). → move to `main.js`.

### `25-drawer-fleet-tile-detail.js.part` (157 lines) → `tile-drawer.js`
- **Exports:** `openTile`, `renderTile`, `loadTileIncidents`, `fmtTime`
  (→ helpers), `fmtDetail` (→ helpers).
- **Imports:** `STATUS_LABEL`, `MARKER`, `fmtAge`, `esc` (20); `metaValueHtml`
  (23/helpers); `drawer`, `drawerOverlay`, `closeDrawer`, `openIncident` (24).
- **State / On-load:** none.

### `26-view-contacts.js.part` (458 lines) → `contacts.js`
- **Exports:** `editingContactId`* (→ state), `contactSort`* (→ state),
  `loadContacts`, `renderContacts`, `showContactForm`, `confirmDeleteContact`.
- **Imports:** `CAN_WRITE` (state); `esc` (20); `SCHED_ROLE_LABELS`, `getThisMonday`,
  `SCHED_ROLES` (31).
- **State:** writes `editingContactId`, `contactSort`.

### `27-view-metrics.js.part` (112 lines) → `metrics.js`
- **Exports:** `fmtDuration` (local — only used here, stays in `metrics.js`),
  `loadMetrics`.
- **Imports:** `esc` (20); `navTo` (22).

### `28-view-oncall.js.part` (64 lines) → `oncall.js` — **PILOT (Step 3)**
Smallest view, minimal deps → end-to-end proof of the module pipeline.
- **Exports:** `ONCALL_ROLE_ORDER`, `ONCALL_ROLE_LABELS` (local consts), `loadOncall`.
- **Imports:** `TEAM_TZ` (state); `esc` (20); `navTo` (22).
- **State / On-load:** none (reads TEAM_TZ only).

### `29-view-settings.js.part` (601 lines) → `settings.js`
- **Exports:** `loadSettings`, `renderConfigCard`, `renderSettings`.
- **Imports:** `CAN_WRITE` (state); `esc` (20).

### `30-view-maintenance.js.part` (284 lines) → `maintenance.js`
- **Exports:** `loadMaintenance`, `renderMaintenance`.
- **Imports:** `CAN_WRITE` (state). (Self-contained otherwise.)

### `31-view-schedule.js.part` (258 lines) → `schedule.js`
- **Exports:** `SCHED_DAYS`, `SCHED_DAY_LABELS`, `SCHED_SHIFTS`,
  `SCHED_SHIFT_LABELS`, `SCHED_ROLES`, `SCHED_ROLE_LABELS`, `currentRole`*
  (→ state), `teamNowParts`, `getThisMonday`, `shiftIndexForHour`, `addDays`,
  `fmtMonDate`, `currentWeekStart`* (→ state), `loadSchedule`, `renderSchedule`.
- **Imports:** `CAN_WRITE`, `TEAM_TZ`, `tiles` (state); `esc` (20).
- **State:** writes `currentRole`, `currentWeekStart`.
- **Note:** `contacts.js` imports `SCHED_ROLES`/`SCHED_ROLE_LABELS`/`getThisMonday`
  from here → schedule must export those (a contacts↔schedule edge, see graph).

### `32-view-rules.js.part` (328 lines) → `rules.js`
- **Exports:** `rulesData`*, `rulesFilterVal`*, `routingRulesData`*,
  `escalationPolicies`*, `newRuleType`* (ALL → state), `loadRules`,
  `renderRulesSection`, `renderNewRuleForm`, `renderRulesTable`.
- **Imports:** `CAN_WRITE` (state); `esc` (20); `routingRuleFormHtml`,
  `wireRoutingRuleForm`, `ignoreRuleFormHtml`, `wireIgnoreRuleForm` (33).
- **State:** writes all five of its declared globals; **shares `escalationPolicies`
  with fragment 24.**

### `33-shared-ignore-routing-rule-forms.js.part` (305 lines) → `rule-forms.js`
- **Exports:** `ignoreRuleFormHtml`, `wireIgnoreRuleForm`, `routingRuleFormHtml`,
  `wireRoutingRuleForm`.
- **Imports:** `CAN_WRITE` (state); `esc` (20); `escalationPolicies` (32/state);
  also calls `renderNewRuleForm`/`renderRulesTable` (32) inside success callbacks.
- **State / On-load:** none.

## Mutable state

Centralize ALL of these in `state.js`. Reassigned scalars (`let`) get getter/setter
pairs (or live in one exported mutable `state` object); the `tiles` Map is never
reassigned (only `.clear()/.set()`) so it can be exported directly.

| Symbol | Decl. fragment | Written by | Notes |
|---|---|---|---|
| `CAN_WRITE` | 10 | 10 (initAuth) | read by 24,26,29,30,31,32,33 — read-only everywhere but owner |
| `AUTH_SUBJECT` | 10 | 10 | display only |
| `TEAM_TZ` | 10 | 10 (initAuth) | read by 28,31 |
| `tiles` (Map) | 10 | 10 (SSE), 21,31 read | **never reassigned** → export object directly |
| `activeFilter` | 10 | 10 (filter btn) | read by 21 |
| `lastPingAt` | 10 | 10 (SSE) | module-local to stream really; keep in state for checkPingAlive |
| `activeView` | 22 | 22 (nav) | read by 24 |
| `incidentsTab` | 23 | 23 | local-ish; read only in 23 |
| `editingContactId` | 26 | 26 | local to contacts |
| `contactSort` | 26 | 26 | local to contacts |
| `rulesData` | 32 | 32 | local to rules |
| `rulesFilterVal` | 32 | 32 | local to rules |
| `routingRulesData` | 32 | 32 | read by incident drawer per nav-map (verify at port) |
| **`escalationPolicies`** | 32 | **32 AND 24 (line 379)** | **two writers → MUST be shared state, not view-owned** |
| `newRuleType` | 32 | 32 | local to rules |
| `currentRole` | 31 | 31 | local to schedule |
| `currentWeekStart` | 31 | 31 | local to schedule |

**Port heuristic:** a global written by exactly one module *and* read by no other
can become a module-local `let` (not exported) — that covers `incidentsTab`,
`editingContactId`, `contactSort`, `rulesData`, `rulesFilterVal`, `newRuleType`,
`currentRole`, `currentWeekStart`. Only the genuinely cross-module ones
(`CAN_WRITE`, `TEAM_TZ`, `AUTH_SUBJECT`, `tiles`, `activeFilter`, `activeView`,
`lastPingAt`, `escalationPolicies`, `routingRulesData`) need to live in `state.js`.
Confirm each read-set during the port (grep) before demoting to module-local.

## Top-level side effects (→ `main.js` entry, explicit init order)

These run today purely by concatenation order. The entry module must call them
**in this order** after all imports resolve:

1. `initAuth()` — fragment 10:182 (async; fire-and-forget as today).
2. `connect()` — fragment 10:258 (opens SSE).
3. `setInterval(checkPingAlive, 5_000)` — fragment 10:211.
4. `wireNav()` (the `.nav-btn[data-view]` listener) — fragment 22:10.
5. `window.addEventListener('hashchange', handleHash)` + `handleHash()` — 22:54-56.
6. `wireTileActivation()` — fragment 21:56 (delegated grid click/keydown).
7. `document.addEventListener('keydown', …Escape→closeDrawer)` — fragment 24:8.
8. `.filter-btn` click wiring — fragment 10:263.

The two `setInterval` *render refreshers* (10s fleet re-render at 21:52; 5s ping
check) may stay module-local — they're pure timers with no cross-module ordering
requirement — but listing them here keeps the init inventory complete.

## Module dependency graph

Arrows = "imports from". `helpers` and `constants` are the sinks (import nothing).

```
constants ◄── helpers ◄──────────────────────────┐
   ▲             ▲    ▲    ▲   ▲   ▲   ▲   ▲   ▲   │
   │             │    │    │   │   │   │   │   │   │
 fleet         (every view + both drawers + rule-forms import helpers)
   │
state ◄── auth, stream, fleet, router, incidents, incident-drawer,
          contacts, oncall, settings, maintenance, schedule, rules, rule-forms
          (state imports NOTHING — pure leaf)

router ──► (loaders of) incidents, incident-drawer, contacts, metrics,
           oncall, settings, maintenance, schedule, rules
metrics ──► router (navTo)        oncall ──► router (navTo)
fleet ──► tile-drawer (openTile)
tile-drawer ──► incident-drawer (openIncident, drawer, closeDrawer)
incident-drawer ──► fleet (renderAll), incidents (loadIncidents), rule-forms
incidents ──► incident-drawer (openIncident)
contacts ──► schedule (SCHED_ROLES, SCHED_ROLE_LABELS, getThisMonday)
rules ──► rule-forms ;  rule-forms ──► rules (renderNewRuleForm/renderRulesTable, escalationPolicies)
stream ──► fleet (renderAll)
main ──► everything (init only)
```

### Cycles (all CALL-TIME, none init-time → all safe under ESM)

ESM allows circular `import`s; a cycle only crashes if module **A** *uses* an
import from **B** at **top-level evaluation** before **B** has finished
initializing. **We verified there are zero top-level cross-fragment references**
(every use is inside a function/handler that runs after load), so all cycles below
are safe to express directly. Listed with the chosen break/handling anyway, so the
port is deliberate:

| # | Cycle | Why it exists | Resolution |
|---|---|---|---|
| C1 | `router` ↔ `metrics`, `router` ↔ `oncall` | router calls `loadMetrics`; metrics calls `navTo` | **Leave as-is** — both refs are call-time. ESM handles it. |
| C2 | `fleet` ↔ `tile-drawer` ↔ `incident-drawer` ↔ `fleet` | tiles open drawers; incident-drawer calls `renderAll` to refresh board after ack/resolve | **Leave as-is** (call-time). Optional: route the post-action refresh through an event/callback instead of importing `renderAll` — defer, not needed for correctness. |
| C3 | `incidents` ↔ `incident-drawer` | list opens drawer; drawer reloads list after action | **Leave as-is** (call-time). |
| C4 | `rules` ↔ `rule-forms` | rules renders forms; form success callbacks re-render the rules table | **Leave as-is** (call-time). `escalationPolicies` moves to `state.js`, which removes the *data* edge; the function-call edge stays and is safe. |
| C5 | `contacts` → `schedule` | contacts reuses schedule's role constants + `getThisMonday` | **Not a cycle** (one-way). Schedule must `export` those symbols. |

The only structural change that *removes* edges (rather than just tolerating them)
is relocating `metaValueHtml`, `fmtTime`, `fmtDetail` into `helpers.js` (kills the
24→23 and 24→25 and 25→23 helper edges) and moving `escalationPolicies` into
`state.js` (kills the 24→32 and 33→32 *data* edge). Everything else is left as a
safe call-time cyclic import.

## Recommended port order (feeds Step 3 & 4)

1. `constants.js`, `helpers.js`, `state.js` — leaves, no imports. (Step 3)
2. `main.js` skeleton + static mount + `<script type="module">`. (Step 3)
3. **Pilot: `oncall.js`** (64 lines, deps = helpers + state + router stub). Prove
   the pipeline end-to-end in a browser. (Step 3)
4. Then per-view, leaf-most first so each port's imports already exist (Step 4):
   `metrics` → `settings` → `maintenance` → `schedule` → `contacts` →
   `tile-drawer` → `incident-drawer` → `incidents` → `rule-forms` → `rules` →
   `fleet` → `auth` → `stream` → finalize `router` + `main`.
   (Cyclic pairs land within one or two adjacent commits; a temporary `import`
   of a not-yet-ported symbol is impossible because un-ported views still live in
   the assembled blob until their commit — see Step 4 transition note below.)

## LOCKED DECISIONS (Step 2 — supersede earlier "transition note")

After verifying the true cross-module reader-sets (grep, code-only), the design
is locked as follows:

### D1 — Migration strategy: "build dark, cut over atomically"

A classic global `<script>` and a `<script type="module">` **cannot share a
scope**, and a deferred module runs *after* the classic inline script, so an
incremental window-bridge would require editing every un-ported fragment and
juggling load order. Instead:

- Build all module files under `src/relay/hub/dashboard_modules/` **without
  loading them** — intermediate commits keep serving the existing assembled blob,
  so the page works at every commit and each module file is reviewable on its own.
- One **atomic cutover commit** swaps the inline `<script>…</script>` for
  `<script type="module" src="/static/dashboard/main.js"></script>`, removes the
  `.js.part` fragments from `manifest.txt`, and deletes the now-dead nav-map
  comment. The full browser exercise happens at this commit (all views at once).
- This trades per-view in-browser testing for zero bridge code and a trivial
  revert (the old fragments survive until the cutover). Node (`node --check`)
  syntax-validates each module as it's written; the assembly tests keep the
  CSS/shell concatenation honest throughout.

### D2 — `state.js` shape: `export let` + setters (NOT a big `state` object)

ESM live bindings mean a read-only `import { CAN_WRITE }` already reflects a
reassignment **in the owning module**, so every *reader* just imports the bare
symbol — **no rename, no churn at read sites** (the vast majority). Only the few
*external write* sites change, routed through setters exported from `state.js`
(an imported binding is read-only, so a non-owner cannot assign it directly).

`state.js` holds exactly the 6 genuinely cross-module symbols (verified reader-set
in parens):

| Symbol | Readers | Writer(s) | Mechanism |
|---|---|---|---|
| `CAN_WRITE` | drawer, contacts, settings, maintenance, schedule, rules, rule-forms | auth | `export let` + `setAuth()` |
| `TEAM_TZ` | oncall, schedule | auth | `export let` + `setAuth()` |
| `tiles` (Map) | fleet, schedule, stream | stream (`.set/.clear`) | `export const` (mutated in place) |
| `activeFilter` | fleet | main (filter btn) | `export let` + `setActiveFilter()` |
| `activeView` | incident-drawer | router | `export let` + `setActiveView()` |
| `escalationPolicies` | rule-forms | **rules + incident-drawer** | `export let` + `setEscalationPolicies()` |

Demoted to **module-local** (single-module, NOT exported): `AUTH_SUBJECT`,
`lastPingAt` (→ `auth`/`stream`); `routingRulesData`, `rulesData`, `rulesFilterVal`,
`newRuleType` (→ `rules`); `incidentsTab` (→ `incidents`); `editingContactId`,
`contactSort` (→ `contacts`); `currentRole`, `currentWeekStart` (→ `schedule`).

### D3 — Serving

`app.mount("/static/dashboard", StaticFiles(directory=_DASHBOARD_MODULES_DIR))`.
The dir ships in the wheel under `src/relay/hub/dashboard_modules/` (same package-
data discovery as `dashboard_parts/`). StaticFiles' default ETag/Last-Modified is
sufficient; no CDN, no build, offline preserved.
