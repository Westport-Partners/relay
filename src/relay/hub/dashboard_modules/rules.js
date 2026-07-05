// Rules view — two separate tables, one per rule type, reflecting the two
// runtime stages. Ignore rules render FIRST in a collapsed-by-default accordion
// (they override every routing rule by short-circuiting the pipeline, so the
// section leads visually — but low-profile, since the drop-list is high-risk
// but rarely browsed). Routing rules render below in an accordion that is
// expanded by default (the common case operators work with).
// Each row carries _type ('routing'|'ignore'); edit/delete/toggle handlers
// dispatch by that tag. Both types share one DynamoDB table.
// Ported from dashboard_parts/32-view-rules.js.part (#33); split per #62.

import { esc } from './helpers.js';
import { CAN_WRITE, escalationPolicies, setEscalationPolicies } from './state.js';
import { routingRuleFormHtml, wireRoutingRuleForm, ignoreRuleFormHtml, wireIgnoreRuleForm } from './rule-forms.js';

// Module-local state (single-module, NOT exported per module map D2).
let routingData = [];        // routing-only cache (priority asc)
let ignoreData = [];         // ignore-only cache (most-triggered first)
let rulesFilterVal = '';     // current filter string
let newRuleType = 'routing'; // 'routing' | 'ignore' — type for the New rule form
let ignoreOpen = false;      // accordion state — ignore collapsed by default
let routingOpen = true;      // accordion state — routing expanded by default

export async function loadRules() {
  const view = document.getElementById('view-rules');
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';

  let routing = [], ignore = [], policies = [], routeDev = {}, ignoreDev = {};
  try {
    const [rr, ig, rp, rd, id] = await Promise.all([
      fetch('/routing-rules'),
      fetch('/ignore-rules'),
      fetch('/escalation-policies'),
      fetch('/routing-rules/deviation'),
      fetch('/ignore-rules/deviation'),
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
  // Two independent caches — no cross-type sort. Routing by priority asc;
  // ignore by most-triggered first (the drop-list's own precedence signal).
  routingData = routing
    .map(r => Object.assign({ _type: 'routing' }, r))
    .sort((a, b) => (a.priority || 0) - (b.priority || 0));
  ignoreData = ignore
    .map(r => Object.assign({ _type: 'ignore' }, r))
    .sort((a, b) => (b.trigger_count || 0) - (a.trigger_count || 0));

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
    devParts.push(`Ignore rules differ from baseline (DB ${esc(String(ignoreDev.db_count ?? '?'))}, file ${esc(String(ignoreDev.baseline_count ?? '?'))}) — <a href="/ignore-rules/download" style="color:var(--teal-light);text-decoration:underline;" download>download routing.yaml</a>`);
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
        <strong>Ignore</strong> rules run first as a short-circuit stage: a matched ignore rule drops the alarm entirely — never paged, never counted, no incident created — overriding every routing rule. They are binary (first match wins, no priority).
        <strong>Routing</strong> rules decide <em>how</em> a non-ignored alarm pages — its severity, escalation policy, and streams (Team / Central).
        They are evaluated in priority order (lowest first); the first match wins. Alarms matching no routing rule use the default policy and a derived severity (shown as <span style="color:var(--amber);">catch-all</span> on the incident).
        The two stages are shown as separate tables below.
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
      return fetch('/ignore-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    }, () => { formWrap.style.display = 'none'; loadRules(); });
  }
  const rt = document.getElementById('newrule-type-routing');
  const it = document.getElementById('newrule-type-ignore');
  if (rt) rt.addEventListener('click', () => { if (newRuleType !== 'routing') { newRuleType = 'routing'; renderNewRuleForm(formWrap); } });
  if (it) it.addEventListener('click', () => { if (newRuleType !== 'ignore')  { newRuleType = 'ignore';  renderNewRuleForm(formWrap); } });
}

// Match-chip HTML — shared across both tables (union of all matchers).
function matchChipsHtml(rule) {
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
  return `<div class="tag-grid" style="margin:0;">${matchParts.join('')}</div>`;
}

function writeActionsHtml(rule) {
  return CAN_WRITE
    ? `<button class="btn-sm btn-edit-anyrule" data-rid="${esc(rule.rule_id)}" data-rtype="${esc(rule._type)}">Edit</button>
       <button class="btn-sm btn-del-anyrule" data-rid="${esc(rule.rule_id)}" data-rtype="${esc(rule._type)}">Delete</button>`
    : `<button class="btn-sm" disabled style="opacity:.4;">Edit</button>
       <button class="btn-sm" disabled style="opacity:.4;">Delete</button>`;
}

function ruleMatchesFilter(r, q) {
  return (r.app_name || '').toLowerCase().includes(q) ||
    (r.alarm_name || '').toLowerCase().includes(q) ||
    (r.alarm_name_prefix || '').toLowerCase().includes(q) ||
    (r.alarm_name_regex || '').toLowerCase().includes(q) ||
    (r.namespace_prefix || '').toLowerCase().includes(q) ||
    (r.escalation_policy_id || '').toLowerCase().includes(q) ||
    (r.note || '').toLowerCase().includes(q) ||
    (r.environment || '').toLowerCase().includes(q);
}

// Routing table body — Priority · Match · Outcome · Count · Enabled · Actions.
function routingTableHtml(rules) {
  if (!rules.length) {
    return `<div style="color:var(--text-dim);padding:16px 14px;">${rulesFilterVal.trim() ? 'No routing rules match the filter.' : 'No routing rules configured.'}</div>`;
  }
  const rows = rules.map(rule => {
    const sevHtml = rule.severity_override
      ? `<span class="inc-sev ${esc(rule.severity_override)}" style="font-size:11px;">${esc(rule.severity_override)}</span>`
      : `<span style="color:var(--text-faint);font-size:11px;">derived sev</span>`;
    const pol = escalationPolicies.find(p => p.policy_id === rule.escalation_policy_id);
    const polHtml = pol
      ? `<span style="font-size:12px;">${esc(pol.name)}</span>`
      : `<span style="font-family:var(--mono);font-size:11px;color:var(--text-dim);">${esc(rule.escalation_policy_id || '—')}</span>`;
    const streams = Array.isArray(rule.streams) ? rule.streams : [];
    const streamsHtml = streams.map(s => `<span class="tag-chip"><span class="tag-v">${esc(s)}</span></span>`).join('');
    const outcomeHtml = `<div class="tag-grid" style="margin:0;align-items:center;">${sevHtml} ${polHtml} ${streamsHtml}</div>`;
    const enabledHtml = `<button class="btn-sm btn-toggle-routing" data-rid="${esc(rule.rule_id)}" data-enabled="${rule.enabled ? '1' : '0'}"
        style="min-width:44px;${rule.enabled ? '' : 'opacity:.55;'}" ${!CAN_WRITE ? 'disabled title="Read-only"' : ''}>${rule.enabled ? 'Yes' : 'No'}</button>`;
    return `<tr>
      <td style="font-family:var(--mono);font-size:12px;text-align:right;color:var(--text);"><span style="font-family:var(--mono);">${esc(String(rule.priority ?? ''))}</span></td>
      <td>${matchChipsHtml(rule)}</td>
      <td>${outcomeHtml}</td>
      <td style="font-family:var(--mono);font-size:12px;text-align:right;color:var(--text);">${rule.match_count || 0}</td>
      <td>${enabledHtml}</td>
      <td style="white-space:nowrap;"><div style="display:flex;gap:6px;">${writeActionsHtml(rule)}</div></td>
    </tr>
    <tr id="anyrule-edit-row-routing-${esc(rule.rule_id)}" style="display:none;">
      <td colspan="6" style="padding:0;"></td>
    </tr>`;
  }).join('');
  return `
    <table class="contacts-table" style="width:100%;">
      <thead>
        <tr>
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
}

// Ignore table body — Match · Outcome (drop + note) · Trigger count · Enabled · Actions.
// No Priority column: ignore is binary, first-match-wins in cache order.
function ignoreTableHtml(rules) {
  if (!rules.length) {
    return `<div style="color:var(--text-dim);padding:16px 14px;">${rulesFilterVal.trim() ? 'No ignore rules match the filter.' : 'No ignore rules configured.'}</div>`;
  }
  const rows = rules.map(rule => {
    const note = rule.note || '';
    const noteTrunc = note.length > 50 ? note.slice(0, 50) + '…' : note;
    const outcomeHtml = `<span style="color:var(--amber);font-size:11px;">drop</span>`
      + (note ? ` <span style="color:var(--text-dim);font-size:11px;font-family:var(--mono);" title="${esc(note)}">${esc(noteTrunc)}</span>` : '');
    const enabledHtml = `<span style="color:var(--text-dim);font-size:11px;">${rule.enabled === false ? 'No' : 'Yes'}</span>`;
    return `<tr>
      <td>${matchChipsHtml(rule)}</td>
      <td>${outcomeHtml}</td>
      <td style="font-family:var(--mono);font-size:12px;text-align:right;color:var(--text);">${rule.trigger_count || 0}</td>
      <td>${enabledHtml}</td>
      <td style="white-space:nowrap;"><div style="display:flex;gap:6px;">${writeActionsHtml(rule)}</div></td>
    </tr>
    <tr id="anyrule-edit-row-ignore-${esc(rule.rule_id)}" style="display:none;">
      <td colspan="5" style="padding:0;"></td>
    </tr>`;
  }).join('');
  return `
    <table class="contacts-table" style="width:100%;">
      <thead>
        <tr>
          <th>Match</th>
          <th>Outcome</th>
          <th style="text-align:right;">Trigger count</th>
          <th>Enabled</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

export function renderRulesTable() {
  const wrap = document.getElementById('rules-table-wrap');
  if (!wrap) return;
  const q = rulesFilterVal.trim().toLowerCase();
  const routingFiltered = q ? routingData.filter(r => ruleMatchesFilter(r, q)) : routingData;
  const ignoreFiltered  = q ? ignoreData.filter(r => ruleMatchesFilter(r, q))  : ignoreData;

  // Aggregate trigger count across ALL ignore rules (not just filtered) — the
  // header advertises total suppression volume regardless of the filter box.
  const ignoreTotalTriggers = ignoreData.reduce((sum, r) => sum + (r.trigger_count || 0), 0);
  const ignoreCountLabel = `${ignoreData.length} rule${ignoreData.length === 1 ? '' : 's'} · ${ignoreTotalTriggers} alarm${ignoreTotalTriggers === 1 ? '' : 's'} dropped`;
  const routingCountLabel = `${routingData.length} rule${routingData.length === 1 ? '' : 's'}`;

  const chevron = open => `<span class="rules-acc-chevron">${open ? '▾' : '▸'}</span>`;

  wrap.innerHTML = `
    <div class="rules-acc" data-acc="ignore">
      <button type="button" class="rules-acc-header" id="rules-acc-ignore-header" aria-expanded="${ignoreOpen}">
        ${chevron(ignoreOpen)}
        <span class="rules-acc-title" style="color:var(--amber);">Ignore rules</span>
        <span class="rules-acc-count">${esc(ignoreCountLabel)}</span>
      </button>
      <div class="rules-acc-body" id="rules-acc-ignore-body" style="display:${ignoreOpen ? 'block' : 'none'};">
        ${ignoreTableHtml(ignoreFiltered)}
      </div>
    </div>
    <div class="rules-acc" data-acc="routing">
      <button type="button" class="rules-acc-header" id="rules-acc-routing-header" aria-expanded="${routingOpen}">
        ${chevron(routingOpen)}
        <span class="rules-acc-title" style="color:var(--teal-light);">Routing rules</span>
        <span class="rules-acc-count">${esc(routingCountLabel)}</span>
      </button>
      <div class="rules-acc-body" id="rules-acc-routing-body" style="display:${routingOpen ? 'block' : 'none'};">
        ${routingTableHtml(routingFiltered)}
      </div>
    </div>`;

  // Accordion toggles — re-render to swap chevron + body visibility.
  const ignHeader = document.getElementById('rules-acc-ignore-header');
  if (ignHeader) ignHeader.addEventListener('click', () => { ignoreOpen = !ignoreOpen; renderRulesTable(); });
  const rtHeader = document.getElementById('rules-acc-routing-header');
  if (rtHeader) rtHeader.addEventListener('click', () => { routingOpen = !routingOpen; renderRulesTable(); });

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
      const endpoint = rtype === 'routing' ? '/routing-rules/' : '/ignore-rules/';
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
      const rule = (rtype === 'routing' ? routingData : ignoreData).find(r => r.rule_id === rid);
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
          return fetch('/ignore-rules/' + encodeURIComponent(rid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        }, () => { editRow.style.display = 'none'; loadRules(); });
      }
    });
  });
}
