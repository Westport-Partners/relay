// Maintenance view — synthetic incident trigger + temporal purge tool.
// Ported from dashboard_parts/30-view-maintenance.js.part (#33).

import { CAN_WRITE } from './state.js';

export function loadMaintenance() {
  renderMaintenance();
}

export function renderMaintenance() {
  const view = document.getElementById('view-maintenance');

  const readOnlyAttr = !CAN_WRITE
    ? ' disabled title="Read-only: authentication not configured"'
    : '';
  const readOnlyNote = !CAN_WRITE
    ? '<div class="info-banner" style="border-left-color:var(--amber);">&#128274; Read-only — authentication not configured. Write access is required to use these tools.</div>'
    : '';

  view.innerHTML = `
    <div class="view-toolbar"><h2>Maintenance</h2></div>
    ${readOnlyNote}

    <!-- Card A: Trigger Synthetic Incident -->
    <div class="settings-card">
      <div class="settings-card-title">Trigger Synthetic Incident</div>
      <div class="info-banner">
        Fires a fake test incident through the full Relay pipeline (ingest, notifications, big-board).
        Synthetic incidents are clearly labelled <span class="badge-synthetic">TEST</span> everywhere they appear.
        They appear in the Incidents list, on the Fleet big-board, and in Metrics (so you can confirm the
        whole pipeline works) until resolved or purged below.
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;max-width:520px;margin-bottom:8px;">
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          App name (optional)
          <input type="text" id="maint-syn-app" placeholder="my-app"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);"
            ${readOnlyAttr}>
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Severity
          <select id="maint-syn-sev"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);"
            ${readOnlyAttr}>
            <option value="SEV1">SEV1</option>
            <option value="SEV2">SEV2</option>
            <option value="SEV3" selected>SEV3</option>
            <option value="SEV4">SEV4</option>
          </select>
        </label>
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;">
          Environment (optional)
          <input type="text" id="maint-syn-env" placeholder="prod"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);"
            ${readOnlyAttr}>
        </label>
      </div>
      <div class="settings-row">
        <button class="btn-primary" id="btn-maint-trigger"${readOnlyAttr}>Trigger test incident</button>
        <span id="maint-trigger-msg" class="settings-inline-msg"></span>
      </div>
    </div>

    <!-- Card B: Purge Incidents & Metrics -->
    <div class="settings-card">
      <div class="settings-card-title">Purge Incidents &amp; Metrics</div>
      <div class="info-banner" style="border-left-color:var(--red);">
        Permanently deletes incidents and their companion records (timeline, metrics, etc.) from storage.
        Use <strong>Preview</strong> first to see what would be affected before committing.
        This action cannot be undone.
      </div>

      <div style="margin-bottom:10px;">
        <div style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">Time bound</div>
        <div class="maint-toggle-group">
          <button class="maint-toggle-btn active" id="btn-maint-before" type="button">Before</button>
          <button class="maint-toggle-btn" id="btn-maint-after" type="button">After</button>
        </div>
      </div>

      <div style="margin-bottom:10px;">
        <label style="font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:0.06em;display:flex;flex-direction:column;gap:4px;max-width:320px;">
          <span id="maint-ts-label">Purge incidents created before</span>
          <input type="datetime-local" id="maint-ts"
            style="background:var(--bg);border:1px solid var(--border-strong);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:12px;font-family:var(--mono);"
            ${readOnlyAttr}>
        </label>
      </div>

      <div style="margin-bottom:14px;display:flex;align-items:center;gap:8px;">
        <input type="checkbox" id="maint-syn-only" ${readOnlyAttr ? 'disabled' : ''}>
        <label for="maint-syn-only" style="font-size:12px;color:var(--text);cursor:pointer;">
          Only synthetic/test incidents
        </label>
      </div>

      <div class="settings-row">
        <button class="btn-sm" id="btn-maint-preview"${readOnlyAttr}>Preview</button>
        <button class="btn-danger" id="btn-maint-purge"${readOnlyAttr}>Purge</button>
        <span id="maint-purge-msg" class="settings-inline-msg"></span>
      </div>
      <div id="maint-preview-result" style="margin-top:8px;font-size:12px;color:var(--text-dim);font-family:var(--mono);display:none;"></div>
    </div>`;

  // --- Card A: Trigger synthetic incident ---
  const triggerBtn = document.getElementById('btn-maint-trigger');
  const triggerMsg = document.getElementById('maint-trigger-msg');
  if (triggerBtn && CAN_WRITE) {
    triggerBtn.addEventListener('click', async () => {
      triggerMsg.textContent = '';
      triggerMsg.className = 'settings-inline-msg';
      triggerBtn.disabled = true;
      triggerBtn.textContent = 'Triggering…';

      const appName = (document.getElementById('maint-syn-app').value || '').trim();
      const severity = document.getElementById('maint-syn-sev').value || 'SEV3';
      const environment = (document.getElementById('maint-syn-env').value || '').trim();

      const body = {};
      if (appName)    body.app_name    = appName;
      if (severity)   body.severity    = severity;
      if (environment) body.environment = environment;

      try {
        const r = await fetch('/synthetic/incident', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (r.ok) {
          const data = await r.json().catch(() => ({}));
          const cid = data.correlation_id || '(unknown)';
          triggerMsg.textContent = '✓ triggered — ' + cid + ' · will appear in Incidents / Fleet';
          triggerMsg.className = 'settings-inline-msg ok';
          setTimeout(() => {
            if (triggerMsg) { triggerMsg.textContent = ''; triggerMsg.className = 'settings-inline-msg'; }
          }, 12000);
        } else if (r.status === 403) {
          triggerMsg.textContent = '✗ Not authorised.';
          triggerMsg.className = 'settings-inline-msg err';
        } else {
          const b = await r.json().catch(() => ({}));
          triggerMsg.textContent = '✗ ' + (b.detail || 'Error ' + r.status);
          triggerMsg.className = 'settings-inline-msg err';
        }
      } catch (_) {
        triggerMsg.textContent = '✗ Network error — please retry.';
        triggerMsg.className = 'settings-inline-msg err';
      }
      triggerBtn.disabled = false;
      triggerBtn.textContent = 'Trigger test incident';
    });
  }

  // --- Card B: Before/After toggle ---
  let purgeDirection = 'before'; // 'before' | 'after'
  const btnBefore = document.getElementById('btn-maint-before');
  const btnAfter  = document.getElementById('btn-maint-after');
  const tsLabel   = document.getElementById('maint-ts-label');

  if (btnBefore && btnAfter) {
    btnBefore.addEventListener('click', () => {
      purgeDirection = 'before';
      btnBefore.classList.add('active'); btnAfter.classList.remove('active');
      if (tsLabel) tsLabel.textContent = 'Purge incidents created before';
    });
    btnAfter.addEventListener('click', () => {
      purgeDirection = 'after';
      btnAfter.classList.add('active'); btnBefore.classList.remove('active');
      if (tsLabel) tsLabel.textContent = 'Purge incidents created after';
    });
  }

  // Helper: build purge body from current form state.
  function buildPurgeBody(dryRun) {
    const tsVal = (document.getElementById('maint-ts').value || '').trim();
    const synOnly = document.getElementById('maint-syn-only').checked;
    const tsIso = tsVal ? new Date(tsVal).toISOString() : null;
    return {
      before: purgeDirection === 'before' ? tsIso : null,
      after:  purgeDirection === 'after'  ? tsIso : null,
      synthetic_only: synOnly,
      dry_run: dryRun,
    };
  }

  // --- Card B: Preview ---
  const previewBtn = document.getElementById('btn-maint-preview');
  const purgeMsg   = document.getElementById('maint-purge-msg');
  const previewResult = document.getElementById('maint-preview-result');
  if (previewBtn && CAN_WRITE) {
    previewBtn.addEventListener('click', async () => {
      purgeMsg.textContent = '';
      purgeMsg.className = 'settings-inline-msg';
      if (previewResult) { previewResult.style.display = 'none'; previewResult.textContent = ''; }
      previewBtn.disabled = true;
      previewBtn.textContent = 'Previewing…';

      const payload = buildPurgeBody(true);
      try {
        const r = await fetch('/admin/purge', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          const txt = 'Would delete ' + (d.matched ?? '?') + ' incident(s) (' + (d.synthetic ?? '?') + ' synthetic), plus ' + (d.companions_deleted ?? '?') + ' companion record(s)';
          if (previewResult) { previewResult.textContent = txt; previewResult.style.display = 'block'; }
        } else if (r.status === 403) {
          purgeMsg.textContent = '✗ Not authorised.';
          purgeMsg.className = 'settings-inline-msg err';
        } else {
          const b = await r.json().catch(() => ({}));
          purgeMsg.textContent = '✗ ' + (b.detail || 'Error ' + r.status);
          purgeMsg.className = 'settings-inline-msg err';
        }
      } catch (_) {
        purgeMsg.textContent = '✗ Network error — please retry.';
        purgeMsg.className = 'settings-inline-msg err';
      }
      previewBtn.disabled = false;
      previewBtn.textContent = 'Preview';
    });
  }

  // --- Card B: Purge (destructive) ---
  const purgeBtn = document.getElementById('btn-maint-purge');
  if (purgeBtn && CAN_WRITE) {
    purgeBtn.addEventListener('click', async () => {
      purgeMsg.textContent = '';
      purgeMsg.className = 'settings-inline-msg';

      // Safety guard: refuse if no timestamp AND synthetic_only is unchecked.
      const tsVal = (document.getElementById('maint-ts').value || '').trim();
      const synOnly = document.getElementById('maint-syn-only').checked;
      if (!tsVal && !synOnly) {
        alert('Safety check: enter a timestamp or check "Only synthetic/test incidents" before purging.');
        return;
      }

      const payload = buildPurgeBody(false);
      const boundDesc = payload.before
        ? 'before ' + payload.before
        : payload.after
          ? 'after ' + payload.after
          : '(no time bound)';
      const scopeDesc = payload.synthetic_only ? 'synthetic incidents only' : 'all incidents';
      if (!confirm('Permanently delete ' + scopeDesc + ' ' + boundDesc + '?\n\nThis cannot be undone.')) return;

      purgeBtn.disabled = true;
      purgeBtn.textContent = 'Purging…';
      if (previewResult) { previewResult.style.display = 'none'; }

      try {
        const r = await fetch('/admin/purge', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          purgeMsg.textContent = '✓ Deleted ' + (d.deleted ?? d.matched ?? '?') + ' incident(s)';
          purgeMsg.className = 'settings-inline-msg ok';
          setTimeout(() => {
            if (purgeMsg) { purgeMsg.textContent = ''; purgeMsg.className = 'settings-inline-msg'; }
          }, 10000);
        } else if (r.status === 403) {
          purgeMsg.textContent = '✗ Not authorised.';
          purgeMsg.className = 'settings-inline-msg err';
        } else {
          const b = await r.json().catch(() => ({}));
          purgeMsg.textContent = '✗ ' + (b.detail || 'Error ' + r.status);
          purgeMsg.className = 'settings-inline-msg err';
        }
      } catch (_) {
        purgeMsg.textContent = '✗ Network error — please retry.';
        purgeMsg.className = 'settings-inline-msg err';
      }
      purgeBtn.disabled = false;
      purgeBtn.textContent = 'Purge';
    });
  }
}
