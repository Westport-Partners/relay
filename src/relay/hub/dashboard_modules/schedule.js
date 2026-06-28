// Schedule view — per-role 7x3 shift grid with auto-schedule, prev/next week nav,
// role tabs, and coverage/gap reporting. Also exports the role/day/shift constants
// and date helpers that contacts.js imports.
// Ported from dashboard_parts/31-view-schedule.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE, TEAM_TZ, tiles } from './state.js';

export const SCHED_DAYS = ['mon','tue','wed','thu','fri','sat','sun'];
export const SCHED_DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
export const SCHED_SHIFTS = ['night','day','evening'];
export const SCHED_SHIFT_LABELS = ['Night (00-08)','Day (08-16)','Evening (16-24)'];
// On-call roles shown as tabs on the Schedule grid (one 7x3 grid per role).
export const SCHED_ROLES = ['primary','secondary','manager'];
export const SCHED_ROLE_LABELS = { primary: 'Primary', secondary: 'Secondary', manager: 'Manager' };
let currentRole = 'primary';  // active role tab

// Wall-clock parts (date + hour + weekday) of an instant in the team timezone.
// Uses Intl so it works for any IANA zone without a date library. Falls back to
// UTC if TEAM_TZ is somehow invalid.
export function teamNowParts() {
  const now = new Date();
  let parts;
  try {
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: TEAM_TZ, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', hour12: false, weekday: 'short',
    });
    parts = {};
    fmt.formatToParts(now).forEach(p => { parts[p.type] = p.value; });
  } catch (_) {
    // Invalid zone — fall back to UTC parts.
    return {
      date: now.toISOString().slice(0, 10),
      hour: now.getUTCHours(),
      dow: now.getUTCDay(),
    };
  }
  // 'en-CA' gives YYYY-MM-DD ordering for date parts.
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  let hour = parseInt(parts.hour, 10);
  if (hour === 24) hour = 0; // some engines emit '24' for midnight
  const dowMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  return { date, hour, dow: dowMap[parts.weekday] ?? 0 };
}

export function getThisMonday() {
  // The week's Monday in the TEAM timezone (RELAY_TZ), matching the server's
  // monday_of() applied to team-local time. Pure date math on the team-local
  // calendar date avoids the UTC/local desync that broke the grid before.
  const { date, dow } = teamNowParts();
  const diff = (dow === 0) ? -6 : (1 - dow); // back up to Monday
  return addDays(date, diff);
}

// Which shift (0=night,1=day,2=evening) covers a given hour-of-day.
export function shiftIndexForHour(hour) {
  if (hour < 8) return 0;
  if (hour < 16) return 1;
  return 2;
}

export function addDays(isoDate, n) {
  const d = new Date(isoDate + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function fmtMonDate(isoDate) {
  const d = new Date(isoDate + 'T00:00:00Z');
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

// Null until first viewed, so it picks up TEAM_TZ resolved by initAuth() rather
// than the module-load default. Prev/Next set it to a concrete week thereafter.
let currentWeekStart = null;

export async function loadSchedule() {
  const view = document.getElementById('view-schedule');
  if (currentWeekStart === null) currentWeekStart = getThisMonday();
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';

  // Fetch contacts for name resolution and current schedule
  let contacts = [], schedData = null;
  try {
    const [rc, rs] = await Promise.all([
      fetch('/contacts'),
      fetch('/schedule?week=' + currentWeekStart),
    ]);
    if (rc.ok) contacts = await rc.json();
    if (rs.ok) schedData = await rs.json();
  } catch (_) {
    view.innerHTML = '<div style="color:var(--red);padding:20px;">Failed to load schedule.</div>';
    return;
  }

  renderSchedule(view, schedData, contacts);
}

export function renderSchedule(view, schedData, contacts) {
  const nameMap = new Map(contacts.map(c => [c.contact_id, c.name || c.contact_id]));

  // Build toolbar
  const autoBtn = CAN_WRITE
    ? `<button class="btn-primary" id="btn-auto-schedule" style="font-size:12px;padding:4px 14px;">&#9654; Auto-schedule</button>`
    : `<button class="btn-primary" disabled title="Read-only: authentication not configured" style="opacity:.45;cursor:not-allowed;font-size:12px;padding:4px 14px;">&#9654; Auto-schedule</button>`;

  const allSlots = schedData && Array.isArray(schedData.slots) ? schedData.slots : [];
  const covByRole = (schedData && schedData.coverage_by_role) || {};
  // Roles present in this schedule (fall back to the default trio).
  const roles = (schedData && Array.isArray(schedData.roles) && schedData.roles.length)
    ? schedData.roles : SCHED_ROLES;
  if (!roles.includes(currentRole)) currentRole = roles[0] || 'primary';

  // Slots for the active role only (legacy rows without a role => primary).
  const slots = allSlots.filter(s => (s.role || 'primary') === currentRole);

  // Role tabs, each showing that role's covered/total + a gap dot.
  const tabsHtml = `<div class="sched-role-tabs">${roles.map(role => {
    const ct = covByRole[role] || null;
    const isGap = ct && ct[0] < ct[1];
    const cntTxt = ct ? ` <span class="sched-tab-cov${isGap ? ' gap' : ''}">${ct[0]}/${ct[1]}</span>` : '';
    return `<button class="sched-role-tab${role === currentRole ? ' active' : ''}" data-role="${esc(role)}">${esc(SCHED_ROLE_LABELS[role] || role)}${cntTxt}</button>`;
  }).join('')}</div>`;

  // Per-role coverage line for the active role.
  const ct = covByRole[currentRole] || (schedData ? [slots.filter(s => s.contact_id).length, slots.length] : [0, 21]);
  const roleGaps = ct[1] - ct[0];
  const gapsHtml = roleGaps > 0
    ? `<span style="color:var(--red);font-weight:700;">${roleGaps} gap${roleGaps !== 1 ? 's' : ''}</span>`
    : `<span style="color:var(--green);">0 gaps</span>`;
  const coverageHtml = `<div class="schedule-coverage">${esc(SCHED_ROLE_LABELS[currentRole] || currentRole)}: ${ct[0]} / ${ct[1]} shifts covered &nbsp;&middot;&nbsp; ${gapsHtml}</div>`;

  // Per-person tally chips for the active role.
  const roleCounts = {};
  slots.forEach(s => { if (s.contact_id) roleCounts[s.contact_id] = (roleCounts[s.contact_id] || 0) + 1; });
  const tallyHtml = Object.keys(roleCounts).length
    ? `<div class="schedule-tallies">${Object.entries(roleCounts).map(([cid, n]) =>
        `<span class="tally-chip">${esc(nameMap.get(cid) || cid)}: ${n}</span>`
      ).join('')}</div>`
    : '';

  // Build 7x3 grid for the active role.
  let gridHtml = '';
  if (allSlots.length === 0) {
    gridHtml = `<div class="schedule-empty-msg">No schedule yet &mdash; click Auto-schedule to generate one.</div>`;
  } else {
    const slotMap = new Map();
    slots.forEach(s => slotMap.set(s.date + '|' + s.shift, s));

    const headerRow = `<tr>
      <th class="day-label">Day</th>
      ${SCHED_SHIFT_LABELS.map(l => `<th>${esc(l)}</th>`).join('')}
    </tr>`;

    // Highlight the cell that is "on call right now" in the team timezone.
    const tnow = teamNowParts();
    const nowShiftIdx = shiftIndexForHour(tnow.hour);

    const dataRows = SCHED_DAYS.map((day, di) => {
      const date = addDays(currentWeekStart, di);
      const isToday = date === tnow.date;
      const cells = SCHED_SHIFTS.map((shift, si) => {
        const isNow = isToday && si === nowShiftIdx;
        const nowCls = isNow ? ' sched-now' : '';
        const s = slotMap.get(date + '|' + shift);
        if (!s) return `<td class="sched-empty${nowCls}">—</td>`;
        const ovTag = s.overridden ? ' <span class="sched-ovr-tag" title="Ad-hoc override">cover</span>' : '';
        if (s.contact_id === null || s.contact_id === undefined) {
          return `<td class="sched-gap${nowCls}">GAP${ovTag}</td>`;
        }
        return `<td class="sched-assigned${nowCls}">${esc(nameMap.get(s.contact_id) || s.contact_id)}${isNow ? ' <span class="sched-now-tag">now</span>' : ''}${ovTag}</td>`;
      }).join('');
      return `<tr${isToday ? ' class="sched-today-row"' : ''}>
        <td class="day-label">${esc(SCHED_DAY_LABELS[di])} <span style="font-size:10px;color:var(--text-dim);">${esc(fmtMonDate(date))}</span></td>
        ${cells}
      </tr>`;
    }).join('');

    gridHtml = `
      <div class="schedule-grid-wrap">
        <table class="schedule-table">
          <thead>${headerRow}</thead>
          <tbody>${dataRows}</tbody>
        </table>
      </div>`;
  }

  view.innerHTML = `
    <div class="view-toolbar"><h2>Schedule</h2></div>
    <div class="schedule-toolbar">
      <button class="btn-sm" id="btn-week-prev">&#8592; Prev</button>
      <span class="schedule-week-label">Week of ${esc(fmtMonDate(currentWeekStart))}</span>
      <button class="btn-sm" id="btn-week-next">Next &#8594;</button>
      ${autoBtn}
      <span id="schedule-auto-err" style="font-size:12px;color:var(--red);"></span>
      <span style="font-size:11px;color:var(--text-dim);margin-left:auto;">Times shown in ${esc(TEAM_TZ)}</span>
    </div>
    ${schedData && allSlots.length ? tabsHtml : ''}
    ${schedData ? coverageHtml : ''}
    ${schedData ? tallyHtml : ''}
    ${gridHtml}`;

  // Role tab switching — re-render in place (no refetch needed).
  view.querySelectorAll('.sched-role-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      currentRole = tab.dataset.role;
      renderSchedule(view, schedData, contacts);
    });
  });

  // Prev/Next week
  document.getElementById('btn-week-prev').addEventListener('click', () => {
    currentWeekStart = addDays(currentWeekStart, -7);
    loadSchedule();
  });
  document.getElementById('btn-week-next').addEventListener('click', () => {
    currentWeekStart = addDays(currentWeekStart, 7);
    loadSchedule();
  });

  // Auto-schedule
  const autoEl = document.getElementById('btn-auto-schedule');
  if (autoEl && CAN_WRITE) {
    autoEl.addEventListener('click', async () => {
      const errEl = document.getElementById('schedule-auto-err');
      autoEl.disabled = true;
      autoEl.textContent = 'Scheduling…';
      if (errEl) errEl.textContent = '';
      try {
        const r = await fetch('/schedule/auto?week=' + currentWeekStart, { method: 'POST' });
        if (r.ok) {
          const data = await r.json();
          // Re-fetch contacts in case they changed, then re-render
          const rc = await fetch('/contacts');
          const ctcts = rc.ok ? await rc.json() : contacts;
          renderSchedule(view, data, ctcts);
        } else {
          const body = await r.json().catch(() => ({}));
          if (errEl) errEl.textContent = r.status === 403 ? '✗ not authorised' : ('✗ ' + (body.detail || 'Error ' + r.status));
          autoEl.disabled = false;
          autoEl.textContent = '▶ Auto-schedule';
        }
      } catch (_) {
        if (errEl) errEl.textContent = '✗ network error';
        autoEl.disabled = false;
        autoEl.textContent = '▶ Auto-schedule';
      }
    });
  }
}

// Clicking a fleet tile with open incidents jumps to that app's incidents.
// Guarded: a load-time listener must not throw if the container is absent,
// or it breaks the whole module graph.
const fleetGroups = document.getElementById('fleet-groups');
if (fleetGroups) {
  fleetGroups.addEventListener('click', e => {
    const tileEl = e.target.closest('.tile');
    if (!tileEl) return;
    const t = tiles.get(tileEl.dataset.key);
    if (t && t.open_incidents > 0) {
      document.querySelector('.nav-btn[data-view="incidents"]').click();
    }
  });
}
