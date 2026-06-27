// Rules view — unified table over both routing and ignore rule types.
// Each row carries _type ('routing'|'ignore'); a Type column distinguishes
// them. Both types share one DynamoDB table; this screen is the single place
// to view/create/edit/delete either kind. New-rule defaults to a routing rule
// but a Type toggle in the form switches to ignore.
// Ported from dashboard_parts/32-view-rules.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE, escalationPolicies, setEscalationPolicies } from './state.js';
import { routingRuleFormHtml, wireRoutingRuleForm, ignoreRuleFormHtml, wireIgnoreRuleForm } from './rule-forms.js';

// Module-local state (single-module, NOT exported per module map D2).
let rulesData = [];          // combined cached rows, each tagged with _type
let rulesFilterVal = '';     // current filter string
let routingRulesData = [];   // routing-only cache (used by incident drawer)
let newRuleType = 'routing'; // 'routing' | 'ignore' — type for the New rule form

export async function loadRules() {
  const view = document.getElementById('view-rules');
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';

  let routing = [], ignore = [], policies = [], routeDev = {}, ignoreDev = {};
  try {
    const [rr, ig, rp, rd, id] = await Promise.all([
      fetch('/routing-rules'),
      fetch('/rules'),
      fetch('/escalation-policies'),
      fetch('/routing-rules/deviation'),
      fetch('/rules/deviation'),
    ]);
    if (rr.ok) { const d = await rr.json(); routing = d.rules || []; }
    if (ig.ok) { const d = await ig.json(); ignore  = d.rules || []; }
    if (rp.ok) { const d = await rp.json(); policies = d.policies || []; }
    if (rd.ok) routeDev = await rd.json();
    if (id.ok) ignoreDev = await id.json();
  } catch (_) {
    view.innerHTML = '<div style="color:var(--red);padding:20px;">Failed to load rules.</div>';
    return;
  }

  // Setter path — rules.js is a non-owner writer for escalationPolicies (state.js owns it).
  setEscalationPolicies(policies);
  routingRulesData = routing.slice();
  // Tag each row with its kind so the unified table can branch per row.
  const combined = []
    .concat(routing.map(r => Object.assign({ _type: 'routing' }, r)))
    .concat(ignore.map(r  => Object.assign({ _type: 'ignore'  }, r)));
  // Routing first (priority asc), then ignore (most-triggered first).
  combined.sort((a, b) => {
    if (a._type !== b._type) return a._type === 'routing' ? -1 : 1;
    if (a._type === 'routing') return (a.priority || 0) - (b.priority || 0);
    return (b.trigger_count || 0) - (a.trigger_count || 0);
  });
  rulesData = combined;

  renderRulesSection(routeDev, ignoreDev);
}

export function renderRulesSection(routeDev, ignoreDev) {
  const view = document.getElementById('view-rules');

  const readOnlyNote = !CAN_WRITE
    ? '<div class="info-banner" style="border-left-color:var(--amber);">&#128274; Read-only — authentication not configured. Write access is required to manage rules.</div>'
    : '';

  const devParts = [];
  if (routeDev && routeDev.deviates) {
    devParts.push(`Routing rules differ from baseline (DB ${esc(String(routeDev.db_count ?? '?'))}, file ${esc(String(routeDev.baseline_count ?? '?'))}) — <a href="/routing-rules/download" style="color:var(--teal-light);text-decoration:underline;" download>download routing-rules.yaml</a>`);
  }
  if (ignoreDev && ignoreDev.deviates) {
    devParts.push(`Ignore rules differ from baseline (DB ${esc(String(ignoreDev.db_count ?? '?'))}, file ${esc(String(ignoreDev.baseline_count ?? '?'))}) — <a href="/rules/download" style="color:var(--teal-light);text-decoration:underline;" download>download routing.yaml</a>`);
  }
  const devBanner = devParts.length
    ? `<div class="info-banner" style="border-left-color:var(--amber);margin-bottom:14px;">&#9888; ${devParts.join('<br>')} to persist the current rule set to your repository.</div>`
    : '';

  const newRuleBtn = CAN_WRITE
    ? `<button class="btn-primary" id="btn-new-rule">+ New rule</button>`
    : `<button class="btn-primary" disabled title="Read-only: authentication not configured" style="opacity:.45;cursor:not-allowed;">+ New rule</button>`;

  view.innerHTML = `
    <div class="view-toolbar"><h2>Rules</h2>${newRuleBtn}</div>
    ${readOnlyNote}
    <div class="settings-card" style="max-width:none;">
      <div class="settings-card-title">About rules</div>
      <div class="info-banner">
        <strong>Routing</strong> rules decide <em>how</em> an alarm pages — its severity, escalation policy, and streams (Team / Central).
        They are evaluated in priority order (lowest first); the first match wins. Alarms matching no routing rule use the default policy and a derived severity (shown as <span style="color:var(--amber);">catch-all</span> on the incident).
        <strong>Ignore</strong> rules drop matching alarms entirely — never paged, never counted, no incident created.
        Both live in one table; use the Type column to tell them apart.
      </div>
    </div>
    ${devBanner}
    <div id="rules-new-form" style="display:none;margin-bottom:14px;"></div>
    <div style="margin-bottom:10px;">
      <input type="text" id="rules-filter" placeholder="Filter by match / policy / note…" value="${esc(rulesFilterVal)}"
        style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);width:100%;max-width:400px;">
    </div>
    <div id="rules-table-wrap"></div>`;

  // Wire new-rule button — opens a form whose Type toggle picks routing|ignore.
  const newBtn = document.getElementById('btn-new-rule');
  if (newBtn && CAN_WRITE) {
    newBtn.addEventListener('click', () => {
      const formWrap = document.getElementById('rules-new-form');
      if (formWrap.style.display !== 'none') { formWrap.style.display = 'none'; return; }
      formWrap.style.display = 'block';
      newRuleType = 'routing';
      renderNewRuleForm(formWrap);
    });
  }

  const filterEl = document.getElementById('rules-filter');
  if (filterEl) {
    filterEl.addEventListener('input', () => {
      rulesFilterVal = filterEl.value;
      renderRulesTable();
    });
  }

  renderRulesTable();
}

// New-rule form with a Type toggle (Routing | Ignore) at the top.
export function renderNewRuleForm(formWrap) {
  const toggle = `
    <div style="margin-bottom:12px;">
      <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">Rule type</div>
      <div class="maint-toggle-group">
        <button class="maint-toggle-btn${newRuleType==='routing'?' active':''}" id="newrule-type-routing" type="button">Routing &mdash; how it pages</button>
        <button class="maint-toggle-btn${newRuleType==='ignore'?' active':''}" id="newrule-type-ignore" type="button">Ignore &mdash; drop it</button>
      </div>
    </div>`;
  const inner = document.createElement('div');
  if (newRuleType === 'routing') {
    inner.innerHTML = routingRuleFormHtml(null);
    formWrap.innerHTML = toggle;
    formWrap.appendChild(inner);
    wireRoutingRuleForm(inner, null, async (body) => {
      return fetch('/routing-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    }, () => { formWrap.style.display = 'none'; loadRules(); });
  } else {
    inner.innerHTML = ignoreRuleFormHtml(null);
    formWrap.innerHTML = toggle;
    formWrap.appendChild(inner);
    wireIgnoreRuleForm(inner, null, async (body) => {
      return fetch('/rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    }, () => { formWrap.style.display = 'none'; loadRules(); });
  }
  const rt = document.getElementById('newrule-type-routing');
  const it = document.getElementById('newrule-type-ignore');
  if (rt) rt.addEventListener('click', () => { if (newRuleType !== 'routing') { newRuleType = 'routing'; renderNewRuleForm(formWrap); } });
  if (it) it.addEventListener('click', () => { if (newRuleType !== 'ignore')  { newRuleType = 'ignore';  renderNewRuleForm(formWrap); } });
}

export function renderRulesTable() {
  const wrap = document.getElementById('rules-table-wrap');
  if (!wrap) return;
  const q = rulesFilterVal.trim().toLowerCase();
  const filtered = q
    ? rulesData.filter(r =>
        (r.app_name || '').toLowerCase().includes(q) ||
        (r.alarm_name || '').toLowerCase().includes(q) ||
        (r.alarm_name_prefix || '').toLowerCase().includes(q) ||
        (r.alarm_name_regex || '').toLowerCase().includes(q) ||
        (r.namespace_prefix || '').toLowerCase().includes(q) ||
        (r.escalation_policy_id || '').toLowerCase().includes(q) ||
        (r.note || '').toLowerCase().includes(q) ||
        (r.environment || '').toLowerCase().includes(q))
    : rulesData;

  if (!filtered.length) {
    wrap.innerHTML = '<div style="color:var(--text-dim);padding:20px 0;">' + (q ? 'No rules match the filter.' : 'No rules configured.') + '</div>';
    return;
  }

  const rows = filtered.map(rule => {
    const isRouting = rule._type === 'routing';
    const typeChip = isRouting
      ? '<span class="tag-chip" style="border-color:var(--teal);"><span class="tag-v" style="color:var(--teal-light);">routing</span></span>'
      : '<span class="tag-chip" style="border-color:var(--amber);"><span class="tag-v" style="color:var(--amber);">ignore</span></span>';

    // Match chips — union of both rule kinds' matchers.
    const matchParts = [];
    if (rule.app_name)          matchParts.push(`<span class="tag-chip"><span class="tag-k">app</span><span class="tag-v">${esc(rule.app_name)}</span></span>`);
    if (rule.alarm_name_prefix) matchParts.push(`<span class="tag-chip"><span class="tag-k">prefix</span><span class="tag-v">${esc(rule.alarm_name_prefix)}</span></span>`);
    else if (rule.alarm_name)   matchParts.push(`<span class="tag-chip"><span class="tag-k">alarm</span><span class="tag-v">${esc(rule.alarm_name)}</span></span>`);
    if (rule.alarm_name_regex)  matchParts.push(`<span class="tag-chip"><span class="tag-k">regex</span><span class="tag-v">${esc(rule.alarm_name_regex)}</span></span>`);
    if (rule.namespace_prefix)  matchParts.push(`<span class="tag-chip"><span class="tag-k">ns</span><span class="tag-v">${esc(rule.namespace_prefix)}</span></span>`);
    if (rule.environment)       matchParts.push(`<span class="tag-chip"><span class="tag-k">env</span><span class="tag-v">${esc(rule.environment)}</span></span>`);
    if (rule.account_id)        matchParts.push(`<span class="tag-chip"><span class="tag-k">acct</span><span class="tag-v">${esc(rule.account_id)}</span></span>`);
    const tagsObj = (rule.tag_filters && typeof rule.tag_filters === 'object') ? rule.tag_filters
                  : (rule.tags && typeof rule.tags === 'object') ? rule.tags : {};
    Object.entries(tagsObj).forEach(([k, v]) =>
      matchParts.push(`<span class="tag-chip"><span class="tag-k">${esc(k)}</span><span class="tag-v">${esc(String(v))}</span></span>`));
    if (!matchParts.length) matchParts.push('<span style="color:var(--text-faint);font-size:11px;">any</span>');

    // Priority (routing only)
    const priorityHtml = isRouting
      ? `<span style="font-family:var(--mono);">${esc(String(rule.priority ?? ''))}</span>`
      : '<span style="color:var(--text-faint);">—</span>';

    // Outcome column: routing → sev + policy + streams; ignore → "drop".
    let outcomeHtml;
    if (isRouting) {
      const sevHtml = rule.severity_override
        ? `<span class="inc-sev ${esc(rule.severity_override)}" style="font-size:11px;">${esc(rule.severity_override)}</span>`
        : `<span style="color:var(--text-faint);font-size:11px;">derived sev</span>`;
      const pol = escalationPolicies.find(p => p.policy_id === rule.escalation_policy_id);
      const polHtml = pol
        ? `<span style="font-size:12px;">${esc(pol.name)}</span>`
        : `<span style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${esc(rule.escalation_policy_id || '—')}</span>`;
      const streams = Array.isArray(rule.streams) ? rule.streams : [];
      const streamsHtml = streams.map(s => `<span class="tag-chip"><span class="tag-v">${esc(s)}</span></span>`).join('');
      outcomeHtml = `<div class="tag-grid" style="margin:0;align-items:center;">${sevHtml} ${polHtml} ${streamsHtml}</div>`;
    } else {
      const note = rule.note || '';
      const noteTrunc = note.length > 50 ? note.slice(0, 50) + '…' : note;
      outcomeHtml = `<span style="color:var(--amber);font-size:11px;">drop</span>`
        + (note ? ` <span style="color:var(--text-dim);font-size:11px;font-family:var(--mono);" title="${esc(note)}">${esc(noteTrunc)}</span>` : '');
    }

    // Count column: routing match_count / ignore trigger_count.
    const count = isRouting ? (rule.match_count || 0) : (rule.trigger_count || 0);

    // Enabled toggle (routing has live enable/disable; ignore shows state only).
    const enabledHtml = isRouting
      ? `<button class="btn-sm btn-toggle-routing" data-rid="${esc(rule.rule_id)}" data-enabled="${rule.enabled ? '1' : '0'}"
          style="min-width:44px;${rule.enabled ? '' : 'opacity:.55;'}" ${!CAN_WRITE ? 'disabled title="Read-only"' : ''}>${rule.enabled ? 'Yes' : 'No'}</button>`
      : `<span style="color:var(--text-dim);font-size:11px;">${rule.enabled === false ? 'No' : 'Yes'}</span>`;

    const writeActions = CAN_WRITE
      ? `<button class="btn-sm btn-edit-anyrule" data-rid="${esc(rule.rule_id)}" data-rtype="${esc(rule._type)}">Edit</button>
         <button class="btn-sm btn-del-anyrule" data-rid="${esc(rule.rule_id)}" data-rtype="${esc(rule._type)}">Delete</button>`
      : `<button class="btn-sm" disabled style="opacity:.4;">Edit</button>
         <button class="btn-sm" disabled style="opacity:.4;">Delete</button>`;

    return `<tr>
      <td>${typeChip}</td>
      <td style="font-family:var(--mono);font-size:12px;text-align:right;color:var(--text);">${priorityHtml}</td>
      <td><div class="tag-grid" style="margin:0;">${matchParts.join('')}</div></td>
      <td>${outcomeHtml}</td>
      <td style="font-family:var(--mono);font-size:12px;text-align:right;color:var(--text);">${count}</td>
      <td>${enabledHtml}</td>
      <td style="white-space:nowrap;"><div style="display:flex;gap:6px;">${writeActions}</div></td>
    </tr>
    <tr id="anyrule-edit-row-${esc(rule._type)}-${esc(rule.rule_id)}" style="display:none;">
      <td colspan="7" style="padding:0;"></td>
    </tr>`;
  }).join('');

  wrap.innerHTML = `
    <table class="contacts-table" style="width:100%;">
      <thead>
        <tr>
          <th>Type</th>
          <th style="text-align:right;">Priority</th>
          <th>Match</th>
          <th>Outcome</th>
          <th style="text-align:right;">Count</th>
          <th>Enabled</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  // Routing enable/disable toggle
  wrap.querySelectorAll('.btn-toggle-routing').forEach(btn => {
    btn.addEventListener('click', async () => {
      const rid = btn.dataset.rid;
      const nowEnabled = btn.dataset.enabled === '1';
      btn.disabled = true;
      try {
        const r = await fetch('/routing-rules/' + encodeURIComponent(rid), {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: !nowEnabled }),
        });
        if (r.ok) { loadRules(); }
        else { const rb = await r.json().catch(() => ({})); alert(r.status === 403 ? 'Not authorised.' : (rb.detail || 'Error ' + r.status)); btn.disabled = false; }
      } catch (_) { alert('Network error — please retry.'); btn.disabled = false; }
    });
  });

  // Delete (dispatch to the right endpoint by type)
  wrap.querySelectorAll('.btn-del-anyrule').forEach(btn => {
    btn.addEventListener('click', async () => {
      const rid = btn.dataset.rid;
      const rtype = btn.dataset.rtype;
      const label = rtype === 'routing' ? 'routing' : 'ignore';
      if (!confirm('Delete this ' + label + ' rule? This cannot be undone.')) return;
      const endpoint = rtype === 'routing' ? '/routing-rules/' : '/rules/';
      btn.disabled = true;
      btn.textContent = 'Deleting…';
      try {
        const r = await fetch(endpoint + encodeURIComponent(rid), { method: 'DELETE' });
        if (r.ok) { loadRules(); }
        else { const rb = await r.json().catch(() => ({})); alert(r.status === 403 ? 'Not authorised.' : (rb.detail || 'Error ' + r.status)); btn.disabled = false; btn.textContent = 'Delete'; }
      } catch (_) { alert('Network error — please retry.'); btn.disabled = false; btn.textContent = 'Delete'; }
    });
  });

  // Edit (open the correct form for the row type)
  wrap.querySelectorAll('.btn-edit-anyrule').forEach(btn => {
    btn.addEventListener('click', () => {
      const rid = btn.dataset.rid;
      const rtype = btn.dataset.rtype;
      const editRow = document.getElementById('anyrule-edit-row-' + rtype + '-' + rid);
      if (!editRow) return;
      const isOpen = editRow.style.display !== 'none';
      if (isOpen) { editRow.style.display = 'none'; return; }
      wrap.querySelectorAll('tr[id^="anyrule-edit-row-"]').forEach(r => { r.style.display = 'none'; });
      editRow.style.display = 'table-row';
      const rule = rulesData.find(r => r.rule_id === rid && r._type === rtype);
      const td = editRow.querySelector('td');
      if (!td || !rule) return;
      td.style.padding = '10px 14px';
      if (rtype === 'routing') {
        td.innerHTML = routingRuleFormHtml(rule);
        wireRoutingRuleForm(td, rule, async (body) => {
          return fetch('/routing-rules/' + encodeURIComponent(rid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        }, () => { editRow.style.display = 'none'; loadRules(); });
      } else {
        td.innerHTML = ignoreRuleFormHtml(rule);
        wireIgnoreRuleForm(td, rule, async (body) => {
          return fetch('/rules/' + encodeURIComponent(rid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        }, () => { editRow.style.display = 'none'; loadRules(); });
      }
    });
  });
}
