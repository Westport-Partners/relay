// Shared mutable app state — only the symbols genuinely read across modules.
// Readers `import { CAN_WRITE }` etc. and get a live binding that reflects the
// owning setter (ESM live bindings). An imported binding is read-only, so writes
// from a non-owning module must route through the exported setters below.
//
// Single-module state (AUTH_SUBJECT, lastPingAt, routingRulesData, incidentsTab,
// editingContactId, contactSort, currentRole, currentWeekStart, …) deliberately
// lives module-local in its owning module, NOT here (#33 module map, D2).

// --- auth gate (written once by auth.js on /auth load) ---
export let CAN_WRITE = false;
export let TEAM_TZ = 'UTC';   // team wall-clock zone; schedule + oncall resolve against this

export function setAuth({ canWrite, teamTz } = {}) {
  if (canWrite !== undefined) CAN_WRITE = canWrite === true;
  if (teamTz) TEAM_TZ = teamTz;
}

// --- fleet board (tiles is mutated in place; never reassigned) ---
export const tiles = new Map();   // "account_id/app_name" -> tile data

export let activeFilter = 'all';  // 'all' | 'incidents'
export function setActiveFilter(v) { activeFilter = v; }

// --- nav ---
export let activeView = 'fleet';
export function setActiveView(v) { activeView = v; }

// --- rules: escalation policy cache, written by BOTH rules.js and
// incident-drawer.js, read by rule-forms.js → must be shared (#33 D2). ---
export let escalationPolicies = [];   // [{policy_id, name}]
export function setEscalationPolicies(v) { escalationPolicies = v || []; }
