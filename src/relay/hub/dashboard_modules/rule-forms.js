// Shared form renderers and wirers for ignore rules and routing rules.
// Used by rules.js (new/edit forms in the Rules view) and incident-drawer.js
// (quick "Add rule…" panel on an open incident).
// Ported from dashboard_parts/33-shared-ignore-routing-rule-forms.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE, escalationPolicies } from './state.js';

//
// DE-DUPLICATION ANALYSIS (Phase 1 finding):
//
// There are three ignore-form entry points:
//   A. ignoreRuleFormHtml / wireIgnoreRuleForm  (this section, lines ~4364/4420)
//      — generic class-based selectors (.ir-app, .ir-alarm, .ir-env, .ir-acct)
//      — has account_id field; note is optional
//      — no re-render on preset change; wireIgnoreRuleForm mutates DOM in-place
//      — used by: renderNewRuleForm (new rule), renderRulesTable (edit row)
//
//   B. incident drawer renderIgnoreForm (line ~1916, inside wireRuleButton IIFE)
//      — id-based selectors (ign-app, ign-alarm, ign-env)
//      — incident-context pre-fill (inc.alarm_name, inc.app_name, inc.environment)
//      — re-renders panel.innerHTML on every preset-button click (reactive UX)
//      — "All from app in ENV" preset button text shows live env value
//      — has live match-preview section (ign-preview): server match counts via
//        the shared helpers below, plus a local "matches this incident?" check
//      — note is required (submit blocked if empty)
//      — no account_id field
//      — posts to /incidents/{id}/ignore (not caller-provided submitFn)
//
// Why they are NOT merged:
//   - Different DOM selectors (class vs id) → merging requires changing one
//     side, which would change rendered output or introduce new query logic.
//   - Different re-render strategy (full innerHTML re-render vs. in-place DOM
//     mutation) → sharing a single wiring function would require a boolean
//     branch for every behavioural difference, making the code less clear.
//   - Different required fields and submit target → a shared submitFn wrapper
//     would need drawer-specific context (inc reference) passed in, making
//     the signature awkward and introducing closure-capture coupling.
//   - The incident drawer form is intentionally context-sensitive (pre-fills
//     from the live incident and shows match preview) — that logic does not
//     belong in a generic rule builder.
//
// Safe shared extraction (done): the live match-count preview — fetchIgnorePreview
// / ignorePreviewHtml / debounce below. Pure request + string building with no DOM
// assumptions, so both forms call it without either giving up its own selectors,
// re-render strategy, or submit target.
//
// Safe shared extraction (done in Phase 1):
//   The field-row CSS pattern (label / input with var(--bg) / var(--mono) etc.)
//   is identical in markup but is an inline style string, not a helper call.
//   Extracting a `formFieldHtml(label, attrs, style)` helper would save ~6
//   repeated inline style strings but is cosmetic. Left for Phase 2 to avoid
//   any risk of changed attribute order affecting CSS specificity or rendering.
//
// TODO (Phase 2): extract the shared field-row HTML builder and align the
// two forms on class-based selectors so both can share wireIgnoreRuleForm.
//
// --- shared live-preview plumbing (used by BOTH ignore forms) --------------
//
// The preview endpoint is the one piece both forms genuinely share: identical
// request shape, identical rendering, no DOM assumptions. Extracting it here
// does not re-open the merge argument above — the renderers and wirers stay
// separate, only the count line is common.

// Trailing-edge debounce. Local to this module because helpers.js (which we do
// not own) has no debounce and the only callers are the two ignore forms.
export function debounce(fn, ms = 250) {
  let timer = null;
  return function (...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn.apply(this, args); }, ms);
  };
}

// POST /ignore-rules/preview — read-only "what would this rule catch?" probe.
// Resolves null on ANY failure (non-200, bad JSON, network) so a missing or
// older Hub simply degrades to "unavailable" instead of breaking the form.
export async function fetchIgnorePreview(body) {
  try {
    const r = await fetch('/ignore-rules/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

// Render the live match-count line for an ignore-rule preview.
//   data === undefined → request in flight
//   data === null      → preview unavailable (never shown as an error)
//   data.no_matcher    → rule has no matcher fields yet
// Open incidents are called out in amber: creating the rule resolves them.
export function ignorePreviewHtml(data) {
  const dim = (t) => `<span style="color:var(--text-dim);">${esc(t)}</span>`;
  if (data === undefined) return dim('Checking…');
  if (data === null)      return dim('Live match count unavailable.');
  if (data.no_matcher)    return dim('Add a matcher field to see how many incidents this would affect.');
  const open  = Number(data.open_count)  || 0;
  const past  = Number(data.past_count)  || 0;
  const total = Number(data.total_count) || (open + past);
  const colour = open > 0 ? 'var(--amber)' : 'var(--text-dim)';
  const counts = `<span style="color:${colour};">Matches ${esc(String(open))} open`
    + ` &middot; ${esc(String(past))} past (${esc(String(total))} total)</span>`;
  const consequence = open > 0
    ? `<div style="color:var(--amber);margin-top:2px;">Creating this rule will resolve `
      + `${esc(String(open))} open incident${open === 1 ? '' : 's'}.</div>`
    : '';
  const samples = Array.isArray(data.samples) ? data.samples.slice(0, 5) : [];
  const sampleHtml = samples.length
    ? `<div style="margin-top:4px;color:var(--text-dim);font-family:var(--mono);font-size:10px;">`
      + samples.map(s => esc(s.alarm_name || s.correlation_id || '—')).join('<br>')
      + `</div>`
    : '';
  return counts + consequence + sampleHtml;
}

// Shared form renderer for both New Rule and Edit Rule panels.
// rule = null → blank form; rule = object → prefill from existing rule.
// A NEW rule defaults to the 'prefix' scope (the recommended choice — one rule
// covers a family of alarms); an EXISTING rule still derives its scope from
// whichever matcher field it was saved with.
export function ignoreRuleFormHtml(rule) {
  const r = rule || {};
  const preset = rule
    ? (r.alarm_name_prefix ? 'prefix' : (r.alarm_name ? 'exact' : 'app'))
    : 'prefix';
  const alarmVal = r.alarm_name_prefix || r.alarm_name || '';
  const alarmLabel = preset === 'prefix' ? 'Alarm name prefix' : 'Alarm name';
  const alarmDisabled = preset === 'app' ? 'disabled style="opacity:0.4;"' : '';
  const title = rule ? 'Edit Ignore Rule' : 'New Ignore Rule';
  return `
    <div class="settings-card" style="max-width:none;background:var(--surface-2);">
      <div class="settings-card-title">${esc(title)}</div>
      <div style="margin-bottom:10px;">
        <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">Match scope</div>
        <div class="maint-toggle-group">
          <button class="maint-toggle-btn${preset==='prefix'?' active':''}" data-irpreset="prefix" type="button">Alarm name prefix (recommended)</button>
          <button class="maint-toggle-btn${preset==='exact'?' active':''}" data-irpreset="exact" type="button">This exact alarm</button>
          <button class="maint-toggle-btn${preset==='app'?' active':''}" data-irpreset="app" type="button">All from app</button>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;max-width:520px;margin-bottom:10px;">
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          App name
          <input type="text" class="ir-app" value="${esc(r.app_name||'')}" placeholder="my-app"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          <span class="ir-alarm-label">${esc(alarmLabel)}</span>
          <input type="text" class="ir-alarm" value="${esc(alarmVal)}" placeholder="${esc(alarmVal)}"
            ${alarmDisabled}
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Environment
          <input type="text" class="ir-env" value="${esc(r.environment||'')}" placeholder="prod"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Account ID
          <input type="text" class="ir-acct" value="${esc(r.account_id||'')}" placeholder="123456789012"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
      </div>
      <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;margin-bottom:10px;max-width:520px;">
        Reason / note
        <textarea class="ir-note" rows="2" placeholder="e.g. known flapping alarm — suppressed permanently"
          style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);resize:vertical;">${esc(r.note||'')}</textarea>
      </label>
      <div class="ir-preview" style="font-size:11px;margin-bottom:10px;font-family:var(--mono);"></div>
      <div class="settings-row">
        <button class="btn-primary ir-submit">${esc(rule ? 'Save changes' : 'Create rule')}</button>
        <button class="btn-sm ir-cancel">Cancel</button>
        <span class="ir-err" style="font-size:12px;color:var(--red);"></span>
      </div>
    </div>`;
}

// Wire up a rendered ignoreRuleFormHtml form inside container el.
// submitFn: async (body) => Response
// onSuccess: (data) => void — called with the parsed response body on success
export function wireIgnoreRuleForm(el, existingRule, submitFn, onSuccess) {
  let formPreset = existingRule
    ? (existingRule.alarm_name_prefix ? 'prefix' : (existingRule.alarm_name ? 'exact' : 'app'))
    : 'prefix';
  let lastAutoNote = '';
  // Bumped on every preview request; a late response with a stale token is
  // dropped so a slow round-trip can't overwrite a newer count.
  let previewToken = 0;

  function generateNote() {
    const appVal   = (el.querySelector('.ir-app')  || {}).value || '';
    const alarmV   = (el.querySelector('.ir-alarm') || {}).value || '';
    const envVal   = (el.querySelector('.ir-env')  || {}).value || '';
    const envClause = envVal.trim() ? ' in ' + envVal.trim() : '';
    if (formPreset === 'exact' && alarmV.trim()) {
      return 'Ignore alarm ' + alarmV.trim() + envClause;
    }
    if (formPreset === 'prefix' && alarmV.trim()) {
      return "Ignore alarms matching prefix '" + alarmV.trim() + "'" + envClause;
    }
    if (formPreset === 'app' && appVal.trim()) {
      return 'Ignore all alarms from ' + appVal.trim() + envClause;
    }
    return 'New ignore rule';
  }

  // Single source of truth for the matcher fields, so the preview probe and the
  // real submit can never disagree about what the rule would match.
  function matcherBody() {
    const appVal  = (el.querySelector('.ir-app')   || {}).value || '';
    const alarmV  = (el.querySelector('.ir-alarm') || {}).value || '';
    const envVal  = (el.querySelector('.ir-env')   || {}).value || '';
    const acctVal = (el.querySelector('.ir-acct')  || {}).value || '';
    const body = {};
    if (appVal.trim())  body.app_name    = appVal.trim();
    if (envVal.trim())  body.environment = envVal.trim();
    if (acctVal.trim()) body.account_id  = acctVal.trim();
    if (formPreset === 'exact'  && alarmV.trim()) body.alarm_name        = alarmV.trim();
    if (formPreset === 'prefix' && alarmV.trim()) body.alarm_name_prefix = alarmV.trim();
    return body;
  }

  // Live "how many incidents would this catch?" line under the note field.
  async function refreshPreview() {
    const prevEl = el.querySelector('.ir-preview');
    if (!prevEl) return;
    const token = ++previewToken;
    prevEl.innerHTML = ignorePreviewHtml(undefined);
    const data = await fetchIgnorePreview(matcherBody());
    if (token !== previewToken) return;   // a newer edit already superseded this
    prevEl.innerHTML = ignorePreviewHtml(data);
  }
  const refreshPreviewDebounced = debounce(refreshPreview, 250);

  function refreshAutoNote() {
    const noteEl = el.querySelector('.ir-note');
    if (!noteEl) return;
    const current = noteEl.value;
    if (current === '' || current === lastAutoNote) {
      lastAutoNote = generateNote();
      noteEl.value = lastAutoNote;
    }
  }

  function updatePresetUi() {
    el.querySelectorAll('[data-irpreset]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.irpreset === formPreset);
    });
    const alarmInput = el.querySelector('.ir-alarm');
    const alarmLabel = el.querySelector('.ir-alarm-label');
    if (alarmInput) {
      const isApp = formPreset === 'app';
      alarmInput.disabled = isApp;
      alarmInput.style.opacity = isApp ? '0.4' : '';
    }
    if (alarmLabel) {
      alarmLabel.textContent = formPreset === 'prefix' ? 'Alarm name prefix' : 'Alarm name';
    }
    refreshAutoNote();
    refreshPreviewDebounced();
  }

  el.querySelectorAll('[data-irpreset]').forEach(btn => {
    btn.addEventListener('click', () => { formPreset = btn.dataset.irpreset; updatePresetUi(); });
  });

  ['.ir-app', '.ir-alarm', '.ir-env'].forEach(sel => {
    const input = el.querySelector(sel);
    if (input) input.addEventListener('input', refreshAutoNote);
  });

  // Every matcher field feeds the live count (account_id included — it narrows
  // the match just as much as the others).
  ['.ir-app', '.ir-alarm', '.ir-env', '.ir-acct'].forEach(sel => {
    const input = el.querySelector(sel);
    if (input) input.addEventListener('input', refreshPreviewDebounced);
  });

  // Seed the note on first wire if the field is blank (new rule only).
  const noteEl = el.querySelector('.ir-note');
  if (noteEl) {
    noteEl.placeholder = 'e.g. Ignore alarm CpuHigh in prod';
    if (!existingRule && noteEl.value === '') {
      lastAutoNote = generateNote();
      noteEl.value = lastAutoNote;
    } else {
      lastAutoNote = noteEl.value;
    }
  }

  // First count on wire — an edit form already has matchers, a new one usually
  // does not (and gets the "add a matcher field" hint back).
  refreshPreview();

  const cancelBtn = el.querySelector('.ir-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', onSuccess);

  const submitBtn = el.querySelector('.ir-submit');
  const errEl = el.querySelector('.ir-err');
  if (submitBtn && CAN_WRITE) {
    submitBtn.addEventListener('click', async () => {
      const noteVal = (el.querySelector('.ir-note') || {}).value || '';
      if (errEl) errEl.textContent = '';
      const body = matcherBody();
      if (noteVal.trim()) body.note = noteVal.trim();
      submitBtn.disabled = true;
      submitBtn.textContent = 'Saving…';
      try {
        const r = await submitFn(body);
        if (r.ok) {
          const data = await r.json().catch(() => ({}));
          const n = data.matched_incident_count || 0;
          if (n > 0 && errEl) {
            errEl.style.color = 'var(--green)';
            errEl.textContent = 'Rule created — resolved ' + n + ' existing incident' + (n === 1 ? '' : 's');
            setTimeout(() => onSuccess(data), 2000);
          } else {
            onSuccess(data);
          }
        } else {
          const rb = await r.json().catch(() => ({}));
          const msgs = { 403: 'Not authorised.', 404: 'Rule not found.', 422: rb.detail || 'Invalid rule — include at least one matcher field.' };
          if (errEl) { errEl.style.color = ''; errEl.textContent = msgs[r.status] || ('Error ' + r.status); }
          submitBtn.disabled = false;
          submitBtn.textContent = existingRule ? 'Save changes' : 'Create rule';
        }
      } catch (_) {
        if (errEl) { errEl.style.color = ''; errEl.textContent = 'Network error — please retry.'; }
        submitBtn.disabled = false;
        submitBtn.textContent = existingRule ? 'Save changes' : 'Create rule';
      }
    });
  }
}

export function routingRuleFormHtml(rule) {
  const r = rule || {};
  const title = rule ? 'Edit Routing Rule' : 'New Routing Rule';
  const priority = r.priority != null ? r.priority : 50;
  const streams = Array.isArray(r.streams) ? r.streams : ['TEAM', 'CENTRAL'];
  const teamChk  = streams.includes('TEAM')    ? 'checked' : '';
  const centralChk = streams.includes('CENTRAL') ? 'checked' : '';
  const enabledChk = (r.enabled === false) ? '' : 'checked';
  const sevOptions = ['', 'SEV1', 'SEV2', 'SEV3', 'SEV4'].map(s => {
    const sel = (r.severity_override || '') === s ? 'selected' : '';
    return `<option value="${esc(s)}" ${sel}>${s || '(derived — no override)'}</option>`;
  }).join('');
  const polOptions = escalationPolicies.length
    ? escalationPolicies.map(p => {
        const sel = r.escalation_policy_id === p.policy_id ? 'selected' : '';
        return `<option value="${esc(p.policy_id)}" ${sel}>${esc(p.name)}</option>`;
      }).join('')
    : `<option value="" disabled>No escalation policies configured.</option>`;
  const polRequired = !escalationPolicies.length ? 'disabled' : '';
  return `
    <div class="settings-card" style="max-width:none;background:var(--surface-2);">
      <div class="settings-card-title">${esc(title)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;max-width:600px;margin-bottom:10px;">
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Priority <span style="text-transform:none;font-size:10px;color:var(--text-faint);">(required, lower = evaluated first)</span>
          <input type="number" class="rr-priority" value="${esc(String(priority))}" min="1" max="9999"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Escalation policy <span style="text-transform:none;font-size:10px;color:var(--red);">required</span>
          <select class="rr-policy" ${polRequired}
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:inherit;">
            ${polOptions}
          </select>
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Alarm name prefix
          <input type="text" class="rr-prefix" value="${esc(r.alarm_name_prefix||'')}" placeholder="MyApp-"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Alarm name regex
          <input type="text" class="rr-regex" value="${esc(r.alarm_name_regex||'')}" placeholder="^MyApp-.*-High$"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Namespace prefix
          <input type="text" class="rr-ns" value="${esc(r.namespace_prefix||'')}" placeholder="AWS/Lambda"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Severity override
          <select class="rr-sev"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:inherit;">
            ${sevOptions}
          </select>
        </label>
      </div>
      <div style="display:flex;gap:20px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
        <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;">Streams</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;">
          <input type="checkbox" class="rr-stream-team" ${teamChk}> TEAM
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;">
          <input type="checkbox" class="rr-stream-central" ${centralChk}> CENTRAL
        </label>
        <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-left:20px;">Enabled</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;">
          <input type="checkbox" class="rr-enabled" ${enabledChk}>
        </label>
      </div>
      <div class="settings-row">
        <button class="btn-primary rr-submit">${esc(rule ? 'Save changes' : 'Create routing rule')}</button>
        <button class="btn-sm rr-cancel">Cancel</button>
        <span class="rr-err" style="font-size:12px;color:var(--red);"></span>
      </div>
    </div>`;
}

export function wireRoutingRuleForm(el, existingRule, submitFn, onSuccess) {
  const cancelBtn = el.querySelector('.rr-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', onSuccess);

  const submitBtn = el.querySelector('.rr-submit');
  const errEl = el.querySelector('.rr-err');
  if (submitBtn && CAN_WRITE) {
    submitBtn.addEventListener('click', async () => {
      const priorityVal = parseInt((el.querySelector('.rr-priority') || {}).value || '', 10);
      const policyVal   = (el.querySelector('.rr-policy')  || {}).value || '';
      const prefixVal   = (el.querySelector('.rr-prefix')  || {}).value || '';
      const regexVal    = (el.querySelector('.rr-regex')   || {}).value || '';
      const nsVal       = (el.querySelector('.rr-ns')      || {}).value || '';
      const sevVal      = (el.querySelector('.rr-sev')     || {}).value || '';
      const teamVal     = !!(el.querySelector('.rr-stream-team')    || {}).checked;
      const centralVal  = !!(el.querySelector('.rr-stream-central') || {}).checked;
      const enabledVal  = !!(el.querySelector('.rr-enabled')        || {}).checked;
      if (errEl) errEl.textContent = '';
      if (!policyVal) {
        if (errEl) errEl.textContent = 'Escalation policy is required.';
        return;
      }
      if (!priorityVal || isNaN(priorityVal)) {
        if (errEl) errEl.textContent = 'Priority is required (positive integer).';
        return;
      }
      const body = { priority: priorityVal, escalation_policy_id: policyVal, enabled: enabledVal };
      if (prefixVal.trim()) body.alarm_name_prefix = prefixVal.trim();
      if (regexVal.trim())  body.alarm_name_regex  = regexVal.trim();
      if (nsVal.trim())     body.namespace_prefix  = nsVal.trim();
      if (sevVal)           body.severity_override = sevVal;
      const streams = [];
      if (teamVal)    streams.push('TEAM');
      if (centralVal) streams.push('CENTRAL');
      if (streams.length) body.streams = streams;
      submitBtn.disabled = true;
      submitBtn.textContent = 'Saving…';
      try {
        const r = await submitFn(body);
        if (r.ok) {
          onSuccess();
        } else {
          const rb = await r.json().catch(() => ({}));
          const msgs = { 403: 'Not authorised.', 404: 'Rule not found.', 422: rb.detail || 'Invalid rule — check regex syntax and required fields.' };
          if (errEl) errEl.textContent = msgs[r.status] || ('Error ' + r.status);
          submitBtn.disabled = false;
          submitBtn.textContent = existingRule ? 'Save changes' : 'Create routing rule';
        }
      } catch (_) {
        if (errEl) errEl.textContent = 'Network error — please retry.';
        submitBtn.disabled = false;
        submitBtn.textContent = existingRule ? 'Save changes' : 'Create routing rule';
      }
    });
  }
}
