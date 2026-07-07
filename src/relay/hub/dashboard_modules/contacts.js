// Contacts view — table with sortable columns, per-contact availability grid,
// OOO dates, role eligibility, test-page button, and inline add/edit/delete.
// Ported from dashboard_parts/26-view-contacts.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE } from './state.js';
import { SCHED_ROLES, SCHED_ROLE_LABELS, getThisMonday } from './schedule.js';

// Module-local state (single writer, never read across modules).
let contactSort = { key: 'name', dir: 1 }; // dir: 1 asc, -1 desc
// Client-side directory filters (applied over already-loaded data).
let contactFilter = { text: '', role: '', availOnly: false };

export async function loadContacts() {
  const view = document.getElementById('view-contacts');
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';
  let contacts = [];
  let avMap = new Map();
  let shiftCounts = new Map();
  try {
    const [r1, r2, r3] = await Promise.all([
      fetch('/contacts'),
      fetch('/availability'),
      fetch('/schedule?week=' + getThisMonday()),
    ]);
    if (!r1.ok) throw new Error('fetch failed');
    contacts = await r1.json();
    if (r2.ok) {
      const avList = await r2.json();
      avList.forEach(a => avMap.set(a.contact_id, a));
    }
    if (r3.ok) {
      const sched = await r3.json();
      (sched.slots || []).forEach(s => {
        if (s.contact_id) shiftCounts.set(s.contact_id, (shiftCounts.get(s.contact_id) || 0) + 1);
      });
    }
  } catch (_) {
    view.innerHTML = '<div style="color:var(--red);padding:20px;">Failed to load contacts.</div>';
    return;
  }
  renderContacts(contacts, avMap, shiftCounts);
}

// Roles a contact is eligible for, used for badges AND the role filter.
// An availability record with an explicit empty roles list means "no roles"
// (honored, not defaulted). A contact with no record at all has no roles.
function eligibleRoles(av) {
  if (av && Array.isArray(av.roles)) return av.roles;
  return [];
}

export function renderContacts(contacts, avMap = new Map(), shiftCounts = new Map()) {
  const view = document.getElementById('view-contacts');
  const addBtn = CAN_WRITE
    ? `<button class="btn-primary" id="btn-add-contact">+ Add contact</button>`
    : `<button class="btn-primary" disabled title="Read-only: authentication not configured" style="opacity:.45;cursor:not-allowed;">+ Add contact</button>`;

  // Sortable header: clickable th with an arrow indicator on the active column.
  const arrow = (key) => contactSort.key === key ? (contactSort.dir === 1 ? ' ▲' : ' ▼') : '';
  const th = (key, label, extra = '') =>
    `<th class="sortable" data-sort="${key}" style="cursor:pointer;user-select:none;${extra}">${label}${arrow(key)}</th>`;

  const roleOpts = SCHED_ROLES.map(r =>
    `<option value="${esc(r)}"${contactFilter.role === r ? ' selected' : ''}>${esc(SCHED_ROLE_LABELS[r] || r)}</option>`
  ).join('');

  view.innerHTML = `
    <div class="info-banner">&#128274; Contacts (name, email, phone) are stored only in this account&#39;s DynamoDB — never in Git.</div>
    <div class="view-toolbar">
      <h2>Contacts</h2>
      ${addBtn}
    </div>
    <div class="contacts-filterbar">
      <input type="search" id="contact-filter-text" class="contacts-filter-input"
        placeholder="Search name / email / phone" value="${esc(contactFilter.text)}">
      <label class="contacts-filter-label">Role
        <select id="contact-filter-role" class="contacts-filter-select">
          <option value="">Any</option>
          ${roleOpts}
        </select>
      </label>
      <label class="contacts-filter-check">
        <input type="checkbox" id="contact-filter-avail" ${contactFilter.availOnly ? 'checked' : ''}>
        Available only
      </label>
      <span id="contact-filter-count" class="contacts-filter-count"></span>
    </div>
    <div id="contact-form-area"></div>
    <div id="contacts-err" class="form-err" style="margin-bottom:8px;"></div>
    <table class="contacts-table">
      <thead><tr>
        ${th('name', 'Name')}
        ${th('email', 'Email')}
        ${th('phone', 'Phone')}
        <th>Roles</th>
        ${th('oncall', 'Available', 'text-align:center;')}
        ${th('shifts', 'Shifts (wk)', 'text-align:center;')}
        <th>Paging</th><th></th>
      </tr></thead>
      <tbody id="contacts-tbody"></tbody>
    </table>`;

  // --- rows are (re)rendered on every sort/filter change; the shell + toolbar
  //     above persist so the search box never loses focus. ---
  renderRows();

  function renderRows() {
    const DAYS = ['mon','tue','wed','thu','fri','sat','sun'];
    const DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const SHIFTS = ['night','day','evening'];
    const SHIFT_LABELS = ['Night','Day','Eve'];

    const sortVal = (c, key) => {
      const av = avMap.get(c.contact_id) || {};
      switch (key) {
        case 'name': return (c.name || '').toLowerCase();
        case 'email': return (c.email || '').toLowerCase();
        case 'phone': return (c.phone || '').toLowerCase();
        case 'oncall': return av.available === true ? 1 : 0;
        case 'shifts': return shiftCounts.get(c.contact_id) || 0;
        default: return '';
      }
    };

    // Apply client-side filters over the loaded data.
    const q = contactFilter.text.trim().toLowerCase();
    const filtered = contacts.filter(c => {
      const av = avMap.get(c.contact_id);
      if (q) {
        const hay = `${c.name || ''} ${c.email || ''} ${c.phone || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (contactFilter.role && !eligibleRoles(av).includes(contactFilter.role)) return false;
      if (contactFilter.availOnly && !(av && av.available === true)) return false;
      return true;
    });

    const sorted = filtered.slice().sort((a, b) => {
      const va = sortVal(a, contactSort.key), vb = sortVal(b, contactSort.key);
      if (va < vb) return -1 * contactSort.dir;
      if (va > vb) return 1 * contactSort.dir;
      return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
    });

    const countEl = document.getElementById('contact-filter-count');
    if (countEl) {
      countEl.textContent = sorted.length === contacts.length
        ? `${contacts.length} contacts`
        : `${sorted.length} of ${contacts.length}`;
    }

  const rows = sorted.length ? sorted.map(c => {
    const avRecord = avMap.get(c.contact_id);
    const av = avRecord || { available: false, slots: {}, ooo: null, roles: [] };
    const isAvail = av.available === true;
    const slots = av.slots || {};
    const ooo = av.ooo || null;
    // Badges reflect actual eligibility (empty = none). The expander checkboxes
    // below default to primary+secondary only when there's no record yet.
    const badgeRoles = eligibleRoles(avRecord);
    const editRoles = avRecord && Array.isArray(avRecord.roles) ? avRecord.roles : ['primary', 'secondary'];
    const nShifts = shiftCounts.get(c.contact_id) || 0;
    const onCallCell = isAvail
      ? '<span style="color:var(--green);">&#9679; Yes</span>'
      : '<span style="color:var(--text-dim);">&#9675; No</span>';
    const shiftsCell = nShifts > 0
      ? `<span style="color:var(--text);">${nShifts}</span>`
      : '<span style="color:var(--text-dim);">0</span>';
    const rolesCell = badgeRoles.length
      ? badgeRoles.map(r => `<span class="role-badge">${esc(SCHED_ROLE_LABELS[r] || r)}</span>`).join(' ')
      : '<span style="color:var(--text-faint);">—</span>';

    // Build grid cells HTML
    const gridCells = DAYS.map((day, di) => {
      const dayCells = SHIFTS.map((shift, si) => {
        const isOn = Array.isArray(slots[day]) && slots[day].includes(shift);
        return `<button class="avail-btn${isOn ? ' on' : ''}" data-day="${day}" data-shift="${shift}"
          ${!CAN_WRITE ? 'disabled' : ''}
          aria-pressed="${isOn}">${isOn ? '✓' : ''}</button>`;
      }).join('');
      return `<span class="avail-day-label">${DAY_LABELS[di]}</span>${dayCells}`;
    }).join('');

    const oooStart = ooo ? ooo.start : '';
    const oooEnd = ooo ? ooo.end : '';

    const copyableCell = (val) => val
      ? `${esc(val)}<button class="copy-btn" data-copy="${esc(val)}" title="Copy">⧉</button>`
      : '—';
    return `
    <tr data-cid="${esc(c.contact_id)}">
      <td>${esc(c.name || '—')}</td>
      <td>${copyableCell(c.email)}</td>
      <td>${copyableCell(c.phone)}</td>
      <td>${rolesCell}</td>
      <td style="text-align:center;">${onCallCell}</td>
      <td style="text-align:center;">${shiftsCell}</td>
      <td class="sub-cell" data-cid="${esc(c.contact_id)}">
        <span class="sub-action" data-cid="${esc(c.contact_id)}"><span style="color:var(--text-faint);font-size:11px;">…</span></span>
        <span class="test-page-result" data-cid="${esc(c.contact_id)}" style="font-size:11px;margin-left:6px;"></span>
      </td>
      <td style="white-space:nowrap;">
        <button class="btn-sm btn-edit-contact" data-cid="${esc(c.contact_id)}"
          ${!CAN_WRITE ? 'disabled title="Read-only: authentication not configured"' : ''}>Edit</button>
        &nbsp;
        <button class="btn-sm danger btn-del-contact" data-cid="${esc(c.contact_id)}"
          ${!CAN_WRITE ? 'disabled title="Read-only: authentication not configured"' : ''}>Delete</button>
        &nbsp;
        <button class="btn-sm btn-avail-toggle" data-cid="${esc(c.contact_id)}">On-call &#9656;</button>
      </td>
    </tr>
    <tr class="avail-expander-row" id="avail-row-${esc(c.contact_id)}">
      <td colspan="8">
        <div class="avail-panel">
          <div class="avail-panel-header">
            <span class="avail-panel-title">On-call availability — ${esc(c.name || c.contact_id)}</span>
            <button class="avail-close-btn" data-cid="${esc(c.contact_id)}" title="Close" aria-label="Close">&times;</button>
          </div>
          <div class="avail-toggle-row">
            <input type="checkbox" id="avail-master-${esc(c.contact_id)}" class="avail-master-chk" data-cid="${esc(c.contact_id)}"
              ${isAvail ? 'checked' : ''} ${!CAN_WRITE ? 'disabled' : ''}>
            <label for="avail-master-${esc(c.contact_id)}" style="cursor:pointer;">Available for on-call</label>
          </div>
          <div class="avail-grid-wrap" style="${isAvail ? '' : 'opacity:0.4;pointer-events:none;'}">
            <div class="avail-grid">
              <span></span>
              ${SHIFT_LABELS.map(l => `<span class="avail-grid-header">${l}</span>`).join('')}
              ${gridCells}
            </div>
          </div>
          <div class="avail-roles-row" style="${isAvail ? '' : 'opacity:0.4;pointer-events:none;'}">
            <span style="color:var(--text-dim);">Eligible roles:</span>
            ${SCHED_ROLES.map(role => `
              <label class="avail-role-chip">
                <input type="checkbox" class="avail-role-chk" value="${esc(role)}"
                  ${editRoles.includes(role) ? 'checked' : ''} ${!CAN_WRITE ? 'disabled' : ''}>
                ${esc(SCHED_ROLE_LABELS[role] || role)}
              </label>`).join('')}
          </div>
          <div class="avail-ooo-row" style="${isAvail ? '' : 'opacity:0.4;pointer-events:none;'}">
            <span style="color:var(--text-dim);">Time off:</span>
            <input type="date" class="avail-ooo-start" value="${esc(oooStart)}" ${!CAN_WRITE ? 'disabled' : ''}>
            <span style="color:var(--text-dim);">to</span>
            <input type="date" class="avail-ooo-end" value="${esc(oooEnd)}" ${!CAN_WRITE ? 'disabled' : ''}>
            <button class="btn-sm avail-ooo-clear" data-cid="${esc(c.contact_id)}" ${!CAN_WRITE ? 'disabled' : ''}>Clear</button>
          </div>
          <div class="avail-save-row">
            <button class="btn-primary avail-save-btn" data-cid="${esc(c.contact_id)}"
              style="font-size:12px;padding:4px 14px;" ${!CAN_WRITE ? 'disabled title="Read-only: authentication not configured"' : ''}>Save</button>
            <span class="avail-saved-msg" id="avail-saved-${esc(c.contact_id)}"></span>
          </div>
        </div>
      </td>
    </tr>`;
  }).join('') : `<tr><td colspan="8" style="color:var(--text-dim);padding:20px 10px;">${contacts.length ? 'No contacts match the filters.' : 'No contacts yet.'}</td></tr>`;

    const tbody = document.getElementById('contacts-tbody');
    if (tbody) tbody.innerHTML = rows;
    wireRowHandlers();
  } // end renderRows

  // Wire sortable headers — toggle direction if same column, else asc. Only
  // the rows re-render (renderRows), so the filter inputs keep focus.
  view.querySelectorAll('th.sortable').forEach(h => {
    h.addEventListener('click', () => {
      const key = h.dataset.sort;
      if (contactSort.key === key) contactSort.dir *= -1;
      else contactSort = { key, dir: 1 };
      renderRows();
    });
  });

  // Wire filter toolbar — each input updates contactFilter then re-renders rows.
  const textInp = document.getElementById('contact-filter-text');
  if (textInp) {
    textInp.addEventListener('input', () => { contactFilter.text = textInp.value; renderRows(); });
  }
  const roleSel = document.getElementById('contact-filter-role');
  if (roleSel) {
    roleSel.addEventListener('change', () => { contactFilter.role = roleSel.value; renderRows(); });
  }
  const availChk = document.getElementById('contact-filter-avail');
  if (availChk) {
    availChk.addEventListener('change', () => { contactFilter.availOnly = availChk.checked; renderRows(); });
  }

  const addBtnEl = document.getElementById('btn-add-contact');
  if (addBtnEl) addBtnEl.addEventListener('click', () => showContactForm(null, null));

  function wireRowHandlers() {

  // Wire copy buttons (email / phone) — clipboard with brief inline confirm.
  view.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const val = btn.dataset.copy || '';
      const done = () => { const o = btn.textContent; btn.textContent = '✓'; setTimeout(() => { btn.textContent = o; }, 1200); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(val).then(done).catch(() => {});
      } else {
        const ta = document.createElement('textarea');
        ta.value = val; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (_) {}
        document.body.removeChild(ta);
      }
    });
  });

  document.querySelectorAll('.btn-edit-contact').forEach(btn => {
    btn.addEventListener('click', () => {
      const cid = btn.dataset.cid;
      const c = contacts.find(x => x.contact_id === cid);
      if (c) showContactForm(c, contacts);
    });
  });

  document.querySelectorAll('.btn-del-contact').forEach(btn => {
    btn.addEventListener('click', () => confirmDeleteContact(btn.dataset.cid));
  });

  // Per-row Subscribe / Test-page hydration. The Paging cell renders a "…"
  // placeholder in each row (renderRows) so the table paints immediately; this
  // fetch resolves SNS subscription state ONCE for all contacts (server lists
  // the topic once and matches on email), then swaps each cell to the right
  // button. Never on the page-load critical path — kicked off after the rows
  // exist. "Subscribed" tracks EMAIL subscription to the paging topic; direct
  // SMS is a separate opt-in (#83).
  hydrateSubscriptions();
  async function hydrateSubscriptions() {
    let statuses = {}, available = false;
    try {
      const r = await fetch('/contacts/subscriptions');
      if (r.ok) { const b = await r.json(); statuses = b.statuses || {}; available = b.available === true; }
    } catch (_) { /* leave cells as "…"; degrade to Test page below */ }
    document.querySelectorAll('.sub-action').forEach(cell => {
      const cid = cell.dataset.cid;
      // Default to a Test-page button when subscription state is unknown/N-A
      // (no topic wired, no email, or the query failed) — never worse than today.
      const st = statuses[cid] || (available ? 'unsubscribed' : 'unknown');
      if (st === 'unsubscribed') {
        cell.innerHTML = `<button class="btn-sm btn-subscribe-contact" data-cid="${esc(cid)}"
          ${!CAN_WRITE ? 'disabled title="Read-only: authentication not configured"' : ''}>Subscribe</button>`;
        const sb = cell.querySelector('.btn-subscribe-contact');
        if (sb) sb.addEventListener('click', () => subscribeContact(cid, sb));
      } else if (st === 'pending') {
        cell.innerHTML = `<span style="color:var(--amber);font-size:11px;" title="Confirmation email sent — awaiting click">&#9679; Pending confirm</span>`;
      } else {
        // confirmed | no_email | unknown → offer Test page (existing behavior).
        cell.innerHTML = `<button class="btn-sm btn-test-contact" data-cid="${esc(cid)}"
          ${!CAN_WRITE ? 'disabled title="Read-only: authentication not configured"' : ''}>Test page</button>`;
        const tb = cell.querySelector('.btn-test-contact');
        if (tb) wireTestButton(tb);
      }
    });
  }

  async function subscribeContact(cid, btn) {
    if (!CAN_WRITE) return;
    const resultEl = document.querySelector(`.test-page-result[data-cid="${CSS.escape(cid)}"]`);
    btn.disabled = true;
    btn.textContent = 'Subscribing…';
    if (resultEl) resultEl.textContent = '';
    try {
      const r = await fetch('/contacts/' + encodeURIComponent(cid) + '/subscribe', { method: 'POST' });
      const body = await r.json().catch(() => ({}));
      if (r.ok && body.ok) {
        const cell = btn.closest('.sub-action');
        if (cell) cell.innerHTML = `<span style="color:var(--amber);font-size:11px;" title="Confirmation email sent — awaiting click">&#9679; Pending confirm</span>`;
      } else if (r.status === 403) {
        if (resultEl) { resultEl.textContent = '✗ not authorised'; resultEl.style.color = 'var(--red)'; }
        btn.disabled = false; btn.textContent = 'Subscribe';
      } else {
        if (resultEl) { resultEl.textContent = '✗ ' + (body.detail || 'failed'); resultEl.style.color = 'var(--red)'; }
        btn.disabled = false; btn.textContent = 'Subscribe';
      }
    } catch (_) {
      if (resultEl) { resultEl.textContent = '✗ network error'; resultEl.style.color = 'var(--red)'; }
      btn.disabled = false; btn.textContent = 'Subscribe';
    }
    if (resultEl) setTimeout(() => { resultEl.textContent = ''; }, 8000);
  }

  function wireTestButton(btn) {
    btn.addEventListener('click', async () => {
      if (!CAN_WRITE) return;
      const cid = btn.dataset.cid;
      const resultEl = document.querySelector(`.test-page-result[data-cid="${CSS.escape(cid)}"]`);
      btn.disabled = true;
      btn.textContent = 'Testing…';
      if (resultEl) resultEl.textContent = '';
      try {
        const r = await fetch('/contacts/' + encodeURIComponent(cid) + '/test', { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (r.status === 403) {
          if (resultEl) { resultEl.textContent = '✗ not authorised'; resultEl.style.color = 'var(--red)'; }
        } else if (r.status === 404) {
          if (resultEl) { resultEl.textContent = '✗ contact not found'; resultEl.style.color = 'var(--red)'; }
        } else if (body.ok) {
          const chs = body.channels || {};
          const sent = Object.entries(chs).filter(([,v]) => v).map(([k]) => k);
          const msg = sent.length ? '✓ sent (' + sent.join(', ') + ')' : '✓ sent';
          if (resultEl) { resultEl.textContent = msg; resultEl.style.color = 'var(--green)'; }
        } else {
          if (resultEl) { resultEl.textContent = '✗ failed'; resultEl.style.color = 'var(--red)'; }
        }
      } catch (_) {
        if (resultEl) { resultEl.textContent = '✗ network error'; resultEl.style.color = 'var(--red)'; }
      }
      btn.disabled = false;
      btn.textContent = 'Test page';
      // Auto-clear result after 8s
      if (resultEl) setTimeout(() => { resultEl.textContent = ''; }, 8000);
    });
  }

  // Wire availability expanders
  document.querySelectorAll('.btn-avail-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const cid = btn.dataset.cid;
      const row = document.getElementById('avail-row-' + cid);
      if (!row) return;
      const isOpen = row.classList.toggle('open');
      btn.textContent = 'On-call ' + (isOpen ? '▾' : '▸');
    });
  });

  // Wire explicit close button on each availability expander panel.
  document.querySelectorAll('.avail-close-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const cid = btn.dataset.cid;
      const row = document.getElementById('avail-row-' + cid);
      if (row) row.classList.remove('open');
      const toggle = document.querySelector(`.btn-avail-toggle[data-cid="${CSS.escape(cid)}"]`);
      if (toggle) toggle.textContent = 'On-call ▸';
    });
  });

  // Wire master on-call toggle — grey out grid/ooo when off
  document.querySelectorAll('.avail-master-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      const cid = chk.dataset.cid;
      const row = document.getElementById('avail-row-' + cid);
      if (!row) return;
      const on = chk.checked;
      // Enable/disable all three sub-sections together (grid, roles, OOO).
      ['.avail-grid-wrap', '.avail-roles-row', '.avail-ooo-row'].forEach(sel => {
        const el = row.querySelector(sel);
        if (el) el.style.cssText = on ? '' : 'opacity:0.4;pointer-events:none;';
      });
    });
  });

  // Wire avail grid buttons (toggle on/off)
  document.querySelectorAll('.avail-btn').forEach(btn => {
    if (btn.disabled) return;
    btn.addEventListener('click', () => {
      const isOn = btn.classList.toggle('on');
      btn.setAttribute('aria-pressed', isOn);
      btn.textContent = isOn ? '✓' : '';
    });
  });

  // Wire OOO clear
  document.querySelectorAll('.avail-ooo-clear').forEach(btn => {
    btn.addEventListener('click', () => {
      const cid = btn.dataset.cid;
      const row = document.getElementById('avail-row-' + cid);
      if (!row) return;
      const startInp = row.querySelector('.avail-ooo-start');
      const endInp   = row.querySelector('.avail-ooo-end');
      if (startInp) startInp.value = '';
      if (endInp)   endInp.value   = '';
    });
  });

  // Wire save buttons
  document.querySelectorAll('.avail-save-btn').forEach(btn => {
    if (!CAN_WRITE) return;
    btn.addEventListener('click', async () => {
      const cid = btn.dataset.cid;
      const row = document.getElementById('avail-row-' + cid);
      if (!row) return;
      const masterChk = row.querySelector('.avail-master-chk');
      const gridBtns  = row.querySelectorAll('.avail-btn');
      const startInp  = row.querySelector('.avail-ooo-start');
      const endInp    = row.querySelector('.avail-ooo-end');
      const savedMsg  = document.getElementById('avail-saved-' + cid);

      const available = masterChk ? masterChk.checked : false;
      const slots = {};
      gridBtns.forEach(gb => {
        if (gb.classList.contains('on')) {
          const d = gb.dataset.day, s = gb.dataset.shift;
          if (!slots[d]) slots[d] = [];
          slots[d].push(s);
        }
      });
      const oooStart = startInp ? startInp.value : '';
      const oooEnd   = endInp   ? endInp.value   : '';
      const ooo = (oooStart && oooEnd) ? { start: oooStart, end: oooEnd } : null;

      const roles = Array.from(row.querySelectorAll('.avail-role-chk'))
        .filter(chk => chk.checked).map(chk => chk.value);

      btn.disabled = true;
      btn.textContent = 'Saving…';
      if (savedMsg) { savedMsg.textContent = ''; savedMsg.style.color = 'var(--green)'; }
      try {
        const r = await fetch('/availability/' + encodeURIComponent(cid), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ available, slots, ooo, roles }),
        });
        if (r.ok) {
          // Update the in-memory availability record so the Available column,
          // role badges, and filters reflect the save without a full reload,
          // then re-render the rows from the refreshed avMap.
          avMap.set(cid, { contact_id: cid, available, slots, ooo, roles });
          renderRows();
          // renderRows() rebuilt the tbody, so re-open this contact's expander
          // and show the confirmation on the freshly rendered elements.
          const newRow = document.getElementById('avail-row-' + cid);
          if (newRow) newRow.classList.add('open');
          const newToggle = document.querySelector(`.btn-avail-toggle[data-cid="${CSS.escape(cid)}"]`);
          if (newToggle) newToggle.textContent = 'On-call ▾';
          const newMsg = document.getElementById('avail-saved-' + cid);
          if (newMsg) {
            newMsg.textContent = '✓ saved';
            newMsg.style.color = 'var(--green)';
            setTimeout(() => { if (newMsg) newMsg.textContent = ''; }, 4000);
          }
          return; // btn was detached by the re-render; nothing more to reset.
        } else {
          const body = await r.json().catch(() => ({}));
          if (savedMsg) {
            savedMsg.textContent = r.status === 403 ? '✗ not authorised' : ('✗ ' + (body.detail || 'Error ' + r.status));
            savedMsg.style.color = 'var(--red)';
          }
        }
      } catch (_) {
        if (savedMsg) { savedMsg.textContent = '✗ network error'; savedMsg.style.color = 'var(--red)'; }
      }
      btn.disabled = false;
      btn.textContent = 'Save';
    });
  });
  } // end wireRowHandlers
} // end renderContacts

export function showContactForm(contact, allContacts) {
  const area = document.getElementById('contact-form-area');
  const isEdit = !!contact;
  area.innerHTML = `
    <form class="contact-form" id="contact-form">
      <div style="font-size:13px;font-weight:600;color:#fff;">${isEdit ? 'Edit contact' : 'Add contact'}</div>
      <label>Name
        <input name="name" type="text" required value="${isEdit ? esc(contact.name || '') : ''}" placeholder="Alice Smith">
      </label>
      <label>Email
        <input name="email" type="email" value="${isEdit ? esc(contact.email || '') : ''}" placeholder="alice@example.com">
      </label>
      <label>Phone
        <input name="phone" type="tel" value="${isEdit ? esc(contact.phone || '') : ''}" placeholder="+1-555-0100">
      </label>
      ${isEdit ? '' : `
      <div class="contact-form-roles">
        <span class="contact-form-roles-label">Eligible roles <span style="color:var(--text-faint);text-transform:none;letter-spacing:0;">(optional)</span></span>
        <div class="contact-form-roles-chips">
          ${SCHED_ROLES.map(role => `
            <label class="avail-role-chip">
              <input type="checkbox" class="new-contact-role-chk" value="${esc(role)}">
              ${esc(SCHED_ROLE_LABELS[role] || role)}
            </label>`).join('')}
        </div>
      </div>`}
      <div id="contact-form-err" class="form-err"></div>
      <div class="form-actions">
        <button type="submit" class="btn-primary">${isEdit ? 'Save' : 'Create'}</button>
        <button type="button" class="btn-sm" id="btn-cancel-contact">Cancel</button>
      </div>
    </form>`;

  document.getElementById('btn-cancel-contact').addEventListener('click', () => { area.innerHTML = ''; });

  document.getElementById('contact-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const contactId = isEdit ? contact.contact_id : ('cnt-' + Math.random().toString(36).slice(2, 9));
    const payload = {
      contact_id: contactId,
      name: fd.get('name') || '',
      email: fd.get('email') || '',
      phone: fd.get('phone') || '',
    };
    const errEl = document.getElementById('contact-form-err');
    errEl.textContent = '';
    try {
      const r = await fetch('/contacts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        // On create, seed an availability record carrying the chosen eligible
        // roles (two-call flow). An explicit empty selection is honored as
        // "no roles" — the contact is still created. Failure here is non-fatal:
        // the contact exists; roles can be set later in the expander.
        if (!isEdit) {
          const roles = Array.from(document.querySelectorAll('.new-contact-role-chk'))
            .filter(chk => chk.checked).map(chk => chk.value);
          try {
            await fetch('/availability/' + encodeURIComponent(contactId), {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ available: false, slots: {}, ooo: null, roles }),
            });
          } catch (_) { /* non-fatal: contact created; roles set later */ }
        }
        area.innerHTML = '';
        loadContacts();
      } else {
        const body = await r.json().catch(() => ({}));
        errEl.textContent = r.status === 403 ? 'Not authorised.' : (body.detail || ('Error ' + r.status));
      }
    } catch (_) { errEl.textContent = 'Network error.'; }
  });
}

export async function confirmDeleteContact(contactId) {
  const errEl = document.getElementById('contacts-err');
  if (errEl) errEl.textContent = '';
  // Inline confirm: replace the row's delete button text.
  const btn = document.querySelector(`.btn-del-contact[data-cid="${CSS.escape(contactId)}"]`);
  if (!btn) return;
  if (btn.dataset.confirming) {
    btn.dataset.confirming = '';
    btn.textContent = 'Delete';
    btn.classList.remove('danger');
    return;
  }
  btn.dataset.confirming = '1';
  btn.textContent = 'Confirm?';
  btn.classList.add('danger');
  // Auto-reset after 4s.
  setTimeout(() => {
    if (btn.dataset.confirming) { btn.dataset.confirming = ''; btn.textContent = 'Delete'; }
  }, 4000);
  btn.addEventListener('click', async function handler() {
    if (!btn.dataset.confirming) return;
    btn.removeEventListener('click', handler);
    try {
      const r = await fetch('/contacts/' + encodeURIComponent(contactId), { method: 'DELETE' });
      if (r.ok) {
        loadContacts();
      } else {
        const body = await r.json().catch(() => ({}));
        if (errEl) errEl.textContent = r.status === 403 ? 'Not authorised.' : (body.detail || ('Error ' + r.status));
      }
    } catch (_) { if (errEl) errEl.textContent = 'Network error.'; }
  }, { once: true });
}
