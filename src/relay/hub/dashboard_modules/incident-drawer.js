// Incident detail drawer — full incident view with ack/resolve buttons,
// AI brief/AAR on demand, and the "Add rule…" panel for quick ignore/routing
// rule creation from the open incident context.
// Ported from dashboard_parts/24-drawer-incident-detail.js.part (#33).

import { esc, fmtTime, fmtDetail, metaValueHtml } from './helpers.js';
import { CAN_WRITE, activeView, escalationPolicies, setEscalationPolicies } from './state.js';
import { renderAll } from './fleet.js';
import { loadIncidents } from './incidents.js';
import { loadRules } from './rules.js';
import { routingRuleFormHtml, wireRoutingRuleForm } from './rule-forms.js';

export const drawer = document.getElementById('drawer');
export const drawerOverlay = document.getElementById('drawer-overlay');
drawerOverlay.addEventListener('click', closeDrawer);

export function closeDrawer() {
  drawer.classList.remove('open');
  drawerOverlay.classList.remove('open');
}

export async function openIncident(correlationId) {
  drawer.innerHTML = '<span class="close">&times;</span><div style="color:var(--text-dim);">Loading…</div>';
  drawer.querySelector('.close').addEventListener('click', closeDrawer);
  drawer.classList.add('open');
  drawerOverlay.classList.add('open');
  let inc;
  try {
    const r = await fetch('/incidents/' + encodeURIComponent(correlationId));
    if (!r.ok) throw new Error('not found');
    inc = await r.json();
  } catch (e) {
    drawer.innerHTML = '<span class="close">&times;</span><p style="color:var(--red);">Incident not found.</p>';
    drawer.querySelector('.close').addEventListener('click', closeDrawer);
    return;
  }
  renderIncident(inc);
}

export function renderIncident(inc) {
  const tl = Array.isArray(inc.timeline) ? inc.timeline.slice() : [];
  tl.sort((a, b) => new Date(a.occurred_at) - new Date(b.occurred_at));
  const tlHtml = tl.length ? tl.map(ev => `
    <div class="tl-event">
      <div class="tl-type">${esc(ev.event_type || 'event')}${ev.stream ? ' · ' + esc(ev.stream) : ''}</div>
      <div class="tl-meta">${esc(fmtTime(ev.occurred_at))}${ev.actor ? ' · ' + esc(ev.actor) : ''}${fmtDetail(ev.detail)}</div>
    </div>`).join('') : '<div style="color:var(--text-dim);">No timeline events recorded.</div>';

  const path = Array.isArray(inc.service_path) && inc.service_path.length
    ? inc.service_path.join(' › ') : (inc.deployment_id || '—');

  const ackable = inc.state === 'TRIGGERED' || inc.state === 'ESCALATED';
  const ackBtnHtml = `<button class="btn-ack" id="btn-ack-inc"${!CAN_WRITE ? ' disabled title="Read-only: authentication not configured"' : ''}>Acknowledge</button>`;
  const resolvable = inc.state !== 'RESOLVED' && inc.state !== 'CLOSED';
  const resolveBtnHtml = `<button class="btn-resolve" id="btn-resolve-inc"${!CAN_WRITE ? ' disabled title="Read-only: authentication not configured"' : ''}>Resolve</button>`;

  drawer.innerHTML = `
    <span class="close">&times;</span>
    <div class="drawer-header-row">
      <div>
        <h2>${esc(inc.app_name)} <span class="inc-sev ${esc(inc.severity)}" style="font-size:11px;">${esc(inc.severity || '')}</span>${inc.synthetic ? ' <span class="badge-synthetic">TEST</span>' : ''}</h2>
        <div class="sub">${esc(inc.correlation_id)}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        ${ackable ? ackBtnHtml : ''}
        ${resolvable ? resolveBtnHtml : ''}
        <button class="btn-sm" id="btn-rule-inc"${!CAN_WRITE ? ' disabled title="Read-only: authentication not configured"' : ''}>Add rule&hellip;</button>
        <button class="btn-sm" id="btn-inc-brief">AI brief</button>
        <button class="btn-sm" id="btn-inc-aar">After-action report</button>
      </div>
    </div>
    <div id="inc-ai-panel" class="inc-ai-panel" style="display:none;"></div>
    <div id="inc-rule-panel" style="display:none;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;margin:10px 0;"></div>
    <div id="drawer-ack-err"></div>
    <div id="drawer-resolve-note" style="font-size:11px;color:var(--text-dim);margin:4px 0 8px;" ${resolvable ? '' : 'hidden'}>
      Note: Resolving the Relay incident does not clear the underlying CloudWatch alarm.
    </div>
    <div class="kv">
      <span class="k">State</span><span>${esc(inc.state || '')}</span>
      <span class="k">Environment</span><span>${esc(inc.environment || '')}${inc.environment_inferred ? ' (inferred)' : ''}</span>
      <span class="k">Service</span><span>${esc(path)}</span>
      <span class="k">Alarm</span><span>${esc(inc.alarm_name || '—')}</span>
      <span class="k">Account / Region</span><span>${esc(inc.account_id)} · ${esc(inc.region || '')}</span>
      <span class="k">Created</span><span>${esc(fmtTime(inc.created_at))}</span>
      <span class="k">Acknowledged</span><span>${inc.acknowledged_by ? esc(inc.acknowledged_by) + ' · ' + esc(fmtTime(inc.acknowledged_at)) : '—'}</span>
      <span class="k">Routed by</span><span>${(function(){
        // Routing provenance: explicit rule vs catch-all default. Lets a responder
        // decide whether to create/edit a routing rule for this alarm.
        if (inc.routing_rule_id) {
          return '<span class="tag-chip"><span class="tag-k">rule</span><span class="tag-v">' + esc(inc.routing_rule_id) + '</span></span>'
            + (inc.routing_reason ? ' <span style="color:var(--text-faint);font-size:11px;">' + esc(inc.routing_reason) + '</span>' : '');
        }
        return '<span style="color:var(--amber);">catch-all default</span>'
          + ' <span style="color:var(--text-faint);font-size:11px;">no routing rule matched — using default policy + derived severity</span>';
      })()}</span>
    </div>
    ${(function() {
      // --- Resolved metadata section ---
      const dm = (inc.deployment_metadata && typeof inc.deployment_metadata === 'object') ? inc.deployment_metadata : {};
      const dmRows = [];
      for (const [k, v] of Object.entries(dm)) {
        if (v == null || typeof v === 'object') continue;
        dmRows.push(`<span class="k">${esc(k)}</span><span>${metaValueHtml(k, v)}</span>`);
      }
      // --- Resource tags section ---
      const rt = (inc.tags && typeof inc.tags === 'object') ? inc.tags : {};
      const rtChips = Object.entries(rt).map(([k, v]) =>
        `<span class="tag-chip"><span class="tag-k">${esc(k)}</span><span class="tag-v">${esc(String(v))}</span></span>`
      ).join('');
      return (dmRows.length ? `<div class="section-title">Resolved metadata</div><div class="kv">${dmRows.join('')}</div>` : '')
        + (rtChips ? `<div class="section-title">Resource tags</div><div class="tag-grid">${rtChips}</div>` : '');
    })()}
    <div class="section-title">Timeline</div>
    <div class="timeline">${tlHtml}</div>`;
  drawer.querySelector('.close').addEventListener('click', closeDrawer);

  const ackBtn = document.getElementById('btn-ack-inc');
  if (ackBtn && CAN_WRITE && ackable) {
    ackBtn.addEventListener('click', async () => {
      ackBtn.disabled = true;
      ackBtn.textContent = 'Acknowledging…';
      const errEl = document.getElementById('drawer-ack-err');
      errEl.textContent = '';
      try {
        const r = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id) + '/acknowledge', { method: 'POST' });
        if (r.ok) {
          // Re-fetch and re-render; also refresh incidents list if visible.
          const updated = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id));
          if (updated.ok) renderIncident(await updated.json());
          if (activeView === 'incidents') loadIncidents();
        } else {
          const body = await r.json().catch(() => ({}));
          const msgs = { 403: 'Not authorised to acknowledge.', 404: 'Incident not found.', 409: body.detail || 'Cannot acknowledge in current state.' };
          errEl.innerHTML = '<div class="drawer-inline-err">' + esc(msgs[r.status] || ('Error ' + r.status)) + '</div>';
          ackBtn.disabled = false;
          ackBtn.textContent = 'Acknowledge';
        }
      } catch (_) {
        document.getElementById('drawer-ack-err').innerHTML = '<div class="drawer-inline-err">Network error — please retry.</div>';
        ackBtn.disabled = false;
        ackBtn.textContent = 'Acknowledge';
      }
    });
  }

  const resolveBtn = document.getElementById('btn-resolve-inc');
  if (resolveBtn && CAN_WRITE && resolvable) {
    resolveBtn.addEventListener('click', async () => {
      resolveBtn.disabled = true;
      resolveBtn.textContent = 'Resolving…';
      const errEl = document.getElementById('drawer-ack-err');
      errEl.textContent = '';
      try {
        const r = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id) + '/resolve', { method: 'POST' });
        if (r.ok) {
          const updated = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id));
          if (updated.ok) renderIncident(await updated.json());
          if (activeView === 'incidents') loadIncidents();
        } else {
          const body = await r.json().catch(() => ({}));
          const msgs = { 403: 'Not authorised to resolve.', 404: 'Incident not found.' };
          errEl.innerHTML = '<div class="drawer-inline-err">' + esc(msgs[r.status] || ('Error ' + r.status)) + '</div>';
          resolveBtn.disabled = false;
          resolveBtn.textContent = 'Resolve';
        }
      } catch (_) {
        document.getElementById('drawer-ack-err').innerHTML = '<div class="drawer-inline-err">Network error — please retry.</div>';
        resolveBtn.disabled = false;
        resolveBtn.textContent = 'Resolve';
      }
    });
  }

  // AI brief / AAR — load on demand into the panel. Both degrade gracefully
  // server-side (deterministic output when no model), so always available.
  function wireAiButton(btnId, path, label) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const panel = document.getElementById('inc-ai-panel');
      panel.style.display = 'block';
      panel.innerHTML = '<div style="color:var(--text-dim);">Generating ' + esc(label) + '…</div>';
      try {
        const r = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id) + path);
        if (!r.ok) throw new Error('http ' + r.status);
        const data = await r.json();
        const tag = data.ai_generated
          ? '<span class="ai-tag">AI-generated</span>'
          : '<span class="ai-tag auto">auto-generated</span>';
        panel.innerHTML = '<div class="inc-ai-head">' + esc(label) + ' ' + tag + '</div>'
          + '<pre class="inc-ai-md">' + esc(data.markdown || '') + '</pre>';
      } catch (_) {
        panel.innerHTML = '<div class="drawer-inline-err">Failed to generate ' + esc(label) + '.</div>';
      }
    });
  }
  wireAiButton('btn-inc-brief', '/brief', 'AI brief');
  wireAiButton('btn-inc-aar', '/aar', 'After-action report');

  // Ignore… button — toggle an inline form panel (no modal).
  // Add rule… button — one panel that authors EITHER an ignore rule OR a
  // routing rule, chosen by an Action toggle. Ignore is the default (the
  // common case from an incident: "stop showing me this"). Both actions store
  // into the same rules table (IGNORE#/ROUTING# rows) via their endpoints.
  (function wireRuleButton() {
    const ruleBtn = document.getElementById('btn-rule-inc');
    if (!ruleBtn) return;
    const panel = document.getElementById('inc-rule-panel');
    // Action state: 'ignore' | 'route'
    let ruleAction = 'ignore';
    // Ignore preset state: 'exact' | 'prefix' | 'app'
    let ignPreset = 'exact';

    function actionToggleHtml() {
      return `
        <div style="margin-bottom:12px;">
          <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">Action</div>
          <div class="maint-toggle-group">
            <button class="maint-toggle-btn${ruleAction==='ignore'?' active':''}" id="rule-action-ignore" type="button">Ignore &mdash; drop this alarm</button>
            <button class="maint-toggle-btn${ruleAction==='route'?' active':''}" id="rule-action-route" type="button">Route &mdash; send it somewhere</button>
          </div>
        </div>`;
    }

    function wireActionToggle() {
      const ign = document.getElementById('rule-action-ignore');
      const rte = document.getElementById('rule-action-route');
      if (ign) ign.addEventListener('click', () => { if (ruleAction !== 'ignore') { ruleAction = 'ignore'; renderPanel(); } });
      if (rte) rte.addEventListener('click', () => { if (ruleAction !== 'route')  { ruleAction = 'route';  renderPanel(); } });
    }

    async function renderPanel() {
      if (ruleAction === 'route') {
        await renderRouteForm();
      } else {
        renderIgnoreForm();
      }
    }

    function matchPreview(preset, appName, alarmVal, env) {
      // Client-side AND-match replicating server ignore logic:
      // account_id exact + app_name exact + alarm/prefix/none + env exact.
      const incAcct = inc.account_id || '';
      const incApp  = inc.app_name  || '';
      const incAlarm= inc.alarm_name|| '';
      const incEnv  = inc.environment || '';
      const aMatch = !appName  || appName  === incApp;
      const eMatch = !env      || env      === incEnv;
      let alarmMatch = true;
      if (preset === 'exact')  alarmMatch = !alarmVal || alarmVal === incAlarm;
      if (preset === 'prefix') alarmMatch = !alarmVal || incAlarm.startsWith(alarmVal);
      // preset 'app': no alarm constraint
      return aMatch && eMatch && alarmMatch;
    }

    function renderIgnoreForm() {
      const alarmLabel = ignPreset === 'prefix' ? 'Alarm name prefix' : 'Alarm name';
      const alarmPlaceholder = ignPreset === 'prefix' ? (inc.alarm_name || '') : (inc.alarm_name || '');
      const alarmValue = ignPreset === 'app' ? '' : (inc.alarm_name || '');
      const alarmDisabled = ignPreset === 'app';

      panel.innerHTML = `
        ${actionToggleHtml()}
        <div style="margin-bottom:10px;">
          <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">Match scope</div>
          <div class="maint-toggle-group">
            <button class="maint-toggle-btn${ignPreset==='exact'?' active':''}" id="ign-preset-exact" type="button">This exact alarm</button>
            <button class="maint-toggle-btn${ignPreset==='prefix'?' active':''}" id="ign-preset-prefix" type="button">Alarm name prefix</button>
            <button class="maint-toggle-btn${ignPreset==='app'?' active':''}" id="ign-preset-app" type="button">All from app in ${esc(inc.environment||'env')}</button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;max-width:520px;margin-bottom:10px;">
          <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
            App name
            <input type="text" id="ign-app" value="${esc(inc.app_name||'')}"
              style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
          </label>
          <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
            ${esc(alarmLabel)}
            <input type="text" id="ign-alarm" value="${esc(alarmValue)}" placeholder="${esc(alarmPlaceholder)}"
              ${alarmDisabled ? 'disabled' : ''}
              style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);${alarmDisabled?'opacity:0.4;':''}" >
          </label>
          <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
            Environment
            <input type="text" id="ign-env" value="${esc(inc.environment||'')}"
              style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);">
          </label>
        </div>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;margin-bottom:10px;">
          Reason / note (required)
          <textarea id="ign-note" rows="2" placeholder="e.g. known flapping alarm — suppressed permanently"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);resize:vertical;"></textarea>
        </label>
        <div id="ign-preview" style="font-size:11px;margin-bottom:10px;font-family:var(--mono);"></div>
        <div class="settings-row">
          <button class="btn-primary" id="btn-ign-submit">Create ignore rule</button>
          <button class="btn-sm" id="btn-ign-cancel">Cancel</button>
        </div>
        <div id="ign-err"></div>`;

      wireActionToggle();

      // Preset buttons
      ['exact','prefix','app'].forEach(p => {
        const el = document.getElementById('ign-preset-' + p);
        if (el) el.addEventListener('click', () => { ignPreset = p; renderIgnoreForm(); });
      });

      // Live preview
      function updatePreview() {
        const appVal   = (document.getElementById('ign-app')   || {}).value || '';
        const alarmV   = (document.getElementById('ign-alarm') || {}).value || '';
        const envVal   = (document.getElementById('ign-env')   || {}).value || '';
        const prevEl   = document.getElementById('ign-preview');
        if (!prevEl) return;
        const matches = matchPreview(ignPreset, appVal, ignPreset==='app'?'':alarmV, envVal);
        if (matches) {
          prevEl.innerHTML = '<span style="color:var(--green);">&#10003; Will match this incident</span>';
        } else {
          prevEl.innerHTML = '<span style="color:var(--amber);">&#9888; Does not match this incident &mdash; check your fields</span>';
        }
      }
      ['ign-app','ign-alarm','ign-env'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updatePreview);
      });
      updatePreview();

      // Cancel
      const cancelBtn = document.getElementById('btn-ign-cancel');
      if (cancelBtn) cancelBtn.addEventListener('click', () => { panel.style.display = 'none'; });

      // Submit
      const submitBtn = document.getElementById('btn-ign-submit');
      if (submitBtn && CAN_WRITE) {
        submitBtn.addEventListener('click', async () => {
          const appVal  = (document.getElementById('ign-app')   || {}).value || '';
          const alarmV  = (document.getElementById('ign-alarm') || {}).value || '';
          const envVal  = (document.getElementById('ign-env')   || {}).value || '';
          const noteVal = (document.getElementById('ign-note')  || {}).value || '';
          const errEl   = document.getElementById('ign-err');
          if (!noteVal.trim()) {
            if (errEl) errEl.innerHTML = '<div class="drawer-inline-err">Reason / note is required.</div>';
            return;
          }
          submitBtn.disabled = true;
          submitBtn.textContent = 'Creating…';
          const body = {};
          if (noteVal.trim()) body.note = noteVal.trim();
          if (appVal.trim())  body.app_name = appVal.trim();
          if (envVal.trim())  body.environment = envVal.trim();
          if (ignPreset === 'exact'  && alarmV.trim()) body.alarm_name = alarmV.trim();
          if (ignPreset === 'prefix' && alarmV.trim()) body.alarm_name_prefix = alarmV.trim();
          try {
            const r = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id) + '/ignore', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            });
            if (r.ok) {
              closeDrawer();
              if (activeView === 'incidents') loadIncidents();
              renderAll();
            } else {
              const rb = await r.json().catch(() => ({}));
              const msgs = { 403: 'Not authorised to create ignore rules.', 404: 'Incident not found.', 422: rb.detail || 'Invalid rule — include at least one matcher field.' };
              if (errEl) errEl.innerHTML = '<div class="drawer-inline-err">' + esc(msgs[r.status] || ('Error ' + r.status)) + '</div>';
              submitBtn.disabled = false;
              submitBtn.textContent = 'Create ignore rule';
            }
          } catch (_) {
            if (errEl) errEl.innerHTML = '<div class="drawer-inline-err">Network error — please retry.</div>';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create ignore rule';
          }
        });
      }
    }

    async function renderRouteForm() {
      // Ensure escalation policies are loaded.
      if (!escalationPolicies.length) {
        try {
          const rp = await fetch('/escalation-policies');
          // Setter path — incident-drawer.js is a non-owner writer for escalationPolicies.
          if (rp.ok) { const d = await rp.json(); setEscalationPolicies(d.policies || []); }
        } catch (_) {}
      }
      panel.innerHTML = `
        ${actionToggleHtml()}
        ${routingRuleFormHtml({
          alarm_name_prefix: inc.alarm_name || '',
          severity_override: inc.severity || '',
          streams: ['TEAM', 'CENTRAL'],
          enabled: true,
          priority: 50,
        })}`;
      wireActionToggle();
      // Replace the inner cancel behaviour to close the panel.
      const innerCancel = panel.querySelector('.rr-cancel');
      if (innerCancel) {
        innerCancel.replaceWith(innerCancel.cloneNode(true));
        panel.querySelector('.rr-cancel').addEventListener('click', () => { panel.style.display = 'none'; });
      }
      // Wire the form submit to POST /incidents/{id}/route.
      wireRoutingRuleForm(panel, null, async (body) => {
        const r = await fetch('/incidents/' + encodeURIComponent(inc.correlation_id) + '/route', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        return r;
      }, () => {
        panel.style.display = 'none';
        const errEl = document.getElementById('drawer-ack-err');
        if (errEl) errEl.innerHTML = '<div style="color:var(--green);font-size:12px;padding:4px 0;">&#10003; Routing rule created.</div>';
        if (activeView === 'rules') loadRules();
      });
    }

    ruleBtn.addEventListener('click', async () => {
      const isOpen = panel.style.display !== 'none';
      if (isOpen) {
        panel.style.display = 'none';
      } else {
        panel.style.display = 'block';
        ruleAction = 'ignore';
        ignPreset = 'exact';
        await renderPanel();
      }
    });
  })();
}
