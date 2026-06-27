// Settings view — Teams webhook, GitLab token, ServiceNow credentials,
// and the read-only config/build info card.
// Ported from dashboard_parts/29-view-settings.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE } from './state.js';

export async function loadSettings() {
  const view = document.getElementById('view-settings');
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';
  let cfg = {}, sys = null;
  try {
    const [rs, rc] = await Promise.all([fetch('/settings'), fetch('/config')]);
    if (!rs.ok) throw new Error('fetch failed');
    cfg = await rs.json();
    if (rc.ok) sys = await rc.json();
  } catch (_) {
    view.innerHTML = '<div style="color:var(--red);padding:20px;">Failed to load settings.</div>';
    return;
  }
  renderSettings(cfg, sys);
}

// Build the read-only configuration + build-info card from GET /config.
export function renderConfigCard(sys) {
  if (!sys) return '';
  const b = sys.build || {}, r = sys.runtime || {}, f = sys.features || {};
  const yes = '<span style="color:var(--green);">&#10003;</span>';
  const no = '<span style="color:var(--text-dim);">&mdash;</span>';
  const flag = v => v ? yes : no;
  const row = (k, v) => `<div class="cfg-k">${esc(k)}</div><div class="cfg-v">${v}</div>`;
  return `
    <div class="settings-card">
      <div class="settings-card-title">Configuration &amp; build</div>
      <div class="cfg-grid">
        ${row('Version', esc(b.version || '?'))}
        ${row('Build (git SHA)', `<code>${esc(b.git_sha || 'unknown')}</code>`)}
        ${row('Built at', esc(b.built_at || 'unknown'))}
        ${row('Role', esc(r.role || ''))}
        ${row('Hub scope', esc(r.hub_scope || ''))}
        ${row('Scaling', esc(r.scaling || ''))}
        ${row('Region', esc(r.region || ''))}
        ${row('Timezone', esc(r.timezone || ''))}
        ${row('Auth mode', esc(r.auth_mode || ''))}
        ${row('Config source', esc(r.config_source || ''))}
        ${row('Log level', esc(r.log_level || ''))}
      </div>
      <div class="settings-card-title" style="margin-top:16px;">Integrations</div>
      <div class="cfg-grid">
        ${row('AI', f.ai_enabled ? yes + ' <code>' + esc(f.ai_provider || 'bedrock') + '</code> <code>' + esc(f.ai_model || '') + '</code>' : no)}
        ${row('Teams webhook', flag(f.teams_webhook_configured))}
        ${row('ServiceNow', flag(f.servicenow_configured))}
        ${row('GitLab', flag(f.gitlab_configured))}
        ${row('Federation forwarding', flag(f.forwarding))}
      </div>
    </div>`;
}

export function renderSettings(cfg, sys) {
  const view = document.getElementById('view-settings');
  const isConfigured = cfg.teams_webhook_configured === true;
  const maskedUrl = cfg.teams_webhook_masked || '';

  const statusHtml = isConfigured
    ? `<div class="settings-status-line configured">&#10003; Connected &mdash; ${esc(maskedUrl)}</div>`
    : `<div class="settings-status-line unconfigured">Not configured</div>`;

  const readOnlyAttr = !CAN_WRITE
    ? ' disabled title="Read-only: authentication not configured"'
    : '';

  const clearBtnHtml = isConfigured
    ? `<button class="btn-sm btn-settings-clear"${readOnlyAttr}>Clear</button>`
    : '';

  const testBtnHtml = isConfigured
    ? `<button class="btn-sm btn-settings-test"${readOnlyAttr}>Send test message</button>`
    : '';

  const integrationsLocked = !!(sys && sys.features && sys.features.integrations_locked);
  const lockedNoticeHtml = integrationsLocked
    ? `<div class="info-banner" style="color:var(--text-dim);">&#9203; Configuration locked &mdash; this integration is pending validation by the maintainer and cannot be saved yet.</div>`
    : '';

  // GitLab token card state (mirrors the Teams pattern).
  const glConfigured = cfg.gitlab_token_configured === true;
  const glMasked = cfg.gitlab_token_masked || '';
  const glStatusHtml = glConfigured
    ? `<div class="settings-status-line configured">&#10003; Token set &mdash; <code>${esc(glMasked)}</code></div>`
    : `<div class="settings-status-line unconfigured">Not configured</div>`;
  const glClearBtnHtml = glConfigured
    ? `<button class="btn-sm btn-gitlab-clear"${readOnlyAttr}>Clear</button>`
    : '';
  const glTestBtnHtml = glConfigured
    ? `<button class="btn-sm btn-gitlab-test"${readOnlyAttr}>Test token</button>`
    : '';
  const glSaveDisabledAttr = integrationsLocked ? ' disabled title="Configuration locked — pending validation"' : readOnlyAttr;

  // ServiceNow card state (mirrors the GitLab pattern; three fields).
  const snConfigured = cfg.servicenow_configured === true;
  const snInstance = cfg.servicenow_instance_url || '';
  const snUsername = cfg.servicenow_username || '';
  const snPwMasked = cfg.servicenow_password_masked || '';
  const snStatusHtml = snConfigured
    ? `<div class="settings-status-line configured">&#10003; Connected &mdash; <code>${esc(snInstance)}</code> as <code>${esc(snUsername)}</code> (pw <code>${esc(snPwMasked)}</code>)</div>`
    : `<div class="settings-status-line unconfigured">Not configured</div>`;
  const snClearBtnHtml = snConfigured
    ? `<button class="btn-sm btn-servicenow-clear"${readOnlyAttr}>Clear</button>`
    : '';
  const snTestBtnHtml = snConfigured
    ? `<button class="btn-sm btn-servicenow-test"${readOnlyAttr}>Test connection</button>`
    : '';
  const snSaveDisabledAttr = integrationsLocked ? ' disabled title="Configuration locked — pending validation"' : readOnlyAttr;

  view.innerHTML = `
    <div class="view-toolbar"><h2>Settings</h2></div>
    <div class="settings-card">
      <div class="settings-card-title">Microsoft Teams (Incoming Webhook)</div>
      <div class="info-banner">
        Posts incident notifications to a Teams channel via an Incoming Webhook.
        This does not create a per-incident chat or add people &mdash; that&rsquo;s a future capability.
        Get a webhook URL from your Teams channel:
        &bull;&bull;&bull; &rarr; Connectors / Workflows &rarr; Incoming Webhook.
      </div>
      ${statusHtml}
      <div class="settings-row">
        <input type="text" id="settings-webhook-url"
          placeholder="https://...webhook.office.com/..."
          value=""${readOnlyAttr}>
        <button class="btn-primary btn-settings-save"${readOnlyAttr}>Save</button>
        ${clearBtnHtml}
      </div>
      <div id="settings-save-msg" class="settings-inline-msg"></div>
      ${testBtnHtml
        ? `<div style="display:flex;align-items:center;gap:10px;">
             ${testBtnHtml}
             <span id="settings-test-msg" class="settings-inline-msg"></span>
           </div>`
        : ''}
    </div>
    <div class="settings-card">
      <div class="settings-card-title">GitLab (Incident issues &amp; DORA)</div>
      <div class="info-banner">
        Optional. When set, Relay opens a GitLab incident issue for each incident and
        closes it on resolve, feeding GitLab&rsquo;s DORA metrics.
        Use a <strong>project</strong> or <strong>group access token</strong> (or a PAT)
        with the <code>api</code> scope and at least the <strong>Reporter</strong> role
        on the target projects &mdash; <code>api</code> is required to create, label,
        and close incident-type issues. A read-only <code>read_api</code> token will
        not work. The token is stored only in this account&rsquo;s DynamoDB &mdash; never in Git.
      </div>
      ${lockedNoticeHtml}
      ${glStatusHtml}
      <div class="settings-row">
        <input type="text" id="settings-gitlab-token"
          placeholder="glpat-… or project/group access token"
          value=""${readOnlyAttr}>
        <button class="btn-primary btn-gitlab-save"${glSaveDisabledAttr}>Save</button>
        ${glClearBtnHtml}
      </div>
      <div id="gitlab-save-msg" class="settings-inline-msg"></div>
      ${glTestBtnHtml
        ? `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
             <input type="text" id="settings-gitlab-test-project"
               placeholder="project to verify, e.g. group/app (optional)"
               style="flex:1;min-width:220px;"${readOnlyAttr}>
             ${glTestBtnHtml}
             <span id="gitlab-test-msg" class="settings-inline-msg"></span>
           </div>`
        : ''}
    </div>
    <div class="settings-card">
      <div class="settings-card-title">ServiceNow (ITSM ticketing)</div>
      <div class="info-banner">
        Optional. When set, Relay creates a ServiceNow incident record for each
        incident and closes it on resolve. Use a service account with the
        <strong>itil</strong> role (or equivalent) so it can create and update
        records in the <code>incident</code> table. Credentials are stored only
        in this account&rsquo;s DynamoDB &mdash; never in Git.
      </div>
      ${lockedNoticeHtml}
      ${snStatusHtml}
      <div class="settings-row" style="flex-wrap:wrap;gap:8px;">
        <input type="text" id="settings-servicenow-instance"
          placeholder="https://yourinstance.service-now.com"
          value="${esc(snInstance)}" style="flex:1;min-width:240px;"${readOnlyAttr}>
        <input type="text" id="settings-servicenow-username"
          placeholder="api username"
          value="${esc(snUsername)}" style="flex:1;min-width:160px;"${readOnlyAttr}>
        <input type="password" id="settings-servicenow-password"
          placeholder="api password"
          value="" style="flex:1;min-width:160px;"${readOnlyAttr}>
        <button class="btn-primary btn-servicenow-save"${snSaveDisabledAttr}>Save</button>
        ${snClearBtnHtml}
      </div>
      <div id="servicenow-save-msg" class="settings-inline-msg"></div>
      ${snTestBtnHtml
        ? `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
             ${snTestBtnHtml}
             <span id="servicenow-test-msg" class="settings-inline-msg"></span>
           </div>`
        : ''}
    </div>
    ${renderConfigCard(sys)}`;

  // Save
  const saveBtn = view.querySelector('.btn-settings-save');
  if (saveBtn && CAN_WRITE) {
    saveBtn.addEventListener('click', async () => {
      const urlInput = document.getElementById('settings-webhook-url');
      const msgEl = document.getElementById('settings-save-msg');
      const webhookUrl = (urlInput ? urlInput.value : '').trim();
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        const r = await fetch('/settings/teams-webhook', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ webhook_url: webhookUrl }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 422) {
          msgEl.textContent = 'Must be an https URL.';
          msgEl.className = 'settings-inline-msg err';
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
      }
    });
  }

  // Clear
  const clearBtn = view.querySelector('.btn-settings-clear');
  if (clearBtn && CAN_WRITE) {
    clearBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('settings-save-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      clearBtn.disabled = true;
      clearBtn.textContent = 'Clearing…';
      try {
        const r = await fetch('/settings/teams-webhook', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ webhook_url: '' }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          clearBtn.disabled = false;
          clearBtn.textContent = 'Clear';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          clearBtn.disabled = false;
          clearBtn.textContent = 'Clear';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        clearBtn.disabled = false;
        clearBtn.textContent = 'Clear';
      }
    });
  }

  // Test
  const testBtn = view.querySelector('.btn-settings-test');
  if (testBtn && CAN_WRITE) {
    testBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('settings-test-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      testBtn.disabled = true;
      testBtn.textContent = 'Sending…';
      try {
        const r = await fetch('/settings/teams-webhook/test', { method: 'POST' });
        if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (r.status === 404) {
          msgEl.textContent = '&#10007; No webhook configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (r.status === 502) {
          msgEl.textContent = '&#10007; failed';
          msgEl.className = 'settings-inline-msg err';
        } else {
          const body = await r.json().catch(() => ({}));
          if (body.ok) {
            msgEl.textContent = '&#10003; sent';
            msgEl.className = 'settings-inline-msg ok';
          } else {
            msgEl.textContent = '&#10007; failed';
            msgEl.className = 'settings-inline-msg err';
          }
        }
      } catch (_) {
        msgEl.textContent = '&#10007; network error';
        msgEl.className = 'settings-inline-msg err';
      }
      testBtn.disabled = false;
      testBtn.textContent = 'Send test message';
      // Auto-clear after 8s
      setTimeout(() => {
        if (msgEl) { msgEl.textContent = ''; msgEl.className = 'settings-inline-msg'; }
      }, 8000);
    });
  }

  // --- GitLab token: Save ---
  const glSaveBtn = view.querySelector('.btn-gitlab-save');
  if (glSaveBtn && CAN_WRITE) {
    glSaveBtn.addEventListener('click', async () => {
      const tokenInput = document.getElementById('settings-gitlab-token');
      const msgEl = document.getElementById('gitlab-save-msg');
      const token = (tokenInput ? tokenInput.value : '').trim();
      if (integrationsLocked) {
        msgEl.textContent = '⏳ Configuration locked — this integration is pending validation by the maintainer and cannot be saved yet.';
        msgEl.className = 'settings-inline-msg err';
        return;
      }
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      glSaveBtn.disabled = true;
      glSaveBtn.textContent = 'Saving…';
      try {
        const r = await fetch('/settings/gitlab-token', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          glSaveBtn.disabled = false;
          glSaveBtn.textContent = 'Save';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          glSaveBtn.disabled = false;
          glSaveBtn.textContent = 'Save';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        glSaveBtn.disabled = false;
        glSaveBtn.textContent = 'Save';
      }
    });
  }

  // --- GitLab token: Clear ---
  const glClearBtn = view.querySelector('.btn-gitlab-clear');
  if (glClearBtn && CAN_WRITE) {
    glClearBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('gitlab-save-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      glClearBtn.disabled = true;
      glClearBtn.textContent = 'Clearing…';
      try {
        const r = await fetch('/settings/gitlab-token', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: '' }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          glClearBtn.disabled = false;
          glClearBtn.textContent = 'Clear';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          glClearBtn.disabled = false;
          glClearBtn.textContent = 'Clear';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        glClearBtn.disabled = false;
        glClearBtn.textContent = 'Clear';
      }
    });
  }

  // --- GitLab token: Test ---
  const glTestBtn = view.querySelector('.btn-gitlab-test');
  if (glTestBtn && CAN_WRITE) {
    glTestBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('gitlab-test-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      glTestBtn.disabled = true;
      glTestBtn.textContent = 'Testing…';
      try {
        const projInput = document.getElementById('settings-gitlab-test-project');
        const proj = (projInput ? projInput.value : '').trim();
        const url = '/settings/gitlab-token/test'
          + (proj ? '?project=' + encodeURIComponent(proj) : '');
        const r = await fetch(url, { method: 'POST' });
        if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (r.status === 404) {
          msgEl.textContent = '✗ No token configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = '✗ ' + (body.detail || 'token check failed');
          msgEl.className = 'settings-inline-msg err';
        } else {
          const body = await r.json().catch(() => ({}));
          if (body.ok) {
            let txt = '✓ authenticated' + (body.username ? ' as ' + body.username : '');
            if (body.project) {
              const lvl = { 10: 'guest', 20: 'reporter', 30: 'developer',
                            40: 'maintainer', 50: 'owner' }[body.access_level]
                          || body.access_level;
              txt += ' — can file issues in ' + body.project
                   + (lvl ? ' (' + lvl + ')' : '');
            } else {
              txt += ' — auth + api scope OK';
            }
            msgEl.textContent = txt;
            msgEl.className = 'settings-inline-msg ok';
          } else {
            msgEl.textContent = '✗ token check failed';
            msgEl.className = 'settings-inline-msg err';
          }
        }
      } catch (_) {
        msgEl.textContent = '✗ network error';
        msgEl.className = 'settings-inline-msg err';
      }
      glTestBtn.disabled = false;
      glTestBtn.textContent = 'Test token';
      setTimeout(() => {
        if (msgEl) { msgEl.textContent = ''; msgEl.className = 'settings-inline-msg'; }
      }, 8000);
    });
  }

  // --- ServiceNow: Save ---
  const snSaveBtn = view.querySelector('.btn-servicenow-save');
  if (snSaveBtn && CAN_WRITE) {
    snSaveBtn.addEventListener('click', async () => {
      const instEl = document.getElementById('settings-servicenow-instance');
      const userEl = document.getElementById('settings-servicenow-username');
      const pwEl = document.getElementById('settings-servicenow-password');
      const msgEl = document.getElementById('servicenow-save-msg');
      const instance_url = (instEl ? instEl.value : '').trim();
      const username = (userEl ? userEl.value : '').trim();
      const password = (pwEl ? pwEl.value : '').trim();
      if (integrationsLocked) {
        msgEl.textContent = '⏳ Configuration locked — this integration is pending validation by the maintainer and cannot be saved yet.';
        msgEl.className = 'settings-inline-msg err';
        return;
      }
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      snSaveBtn.disabled = true;
      snSaveBtn.textContent = 'Saving…';
      try {
        const r = await fetch('/settings/servicenow-credentials', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ instance_url, username, password }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          snSaveBtn.disabled = false;
          snSaveBtn.textContent = 'Save';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          snSaveBtn.disabled = false;
          snSaveBtn.textContent = 'Save';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        snSaveBtn.disabled = false;
        snSaveBtn.textContent = 'Save';
      }
    });
  }

  // --- ServiceNow: Clear ---
  const snClearBtn = view.querySelector('.btn-servicenow-clear');
  if (snClearBtn && CAN_WRITE) {
    snClearBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('servicenow-save-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      snClearBtn.disabled = true;
      snClearBtn.textContent = 'Clearing…';
      try {
        const r = await fetch('/settings/servicenow-credentials', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ instance_url: '', username: '', password: '' }),
        });
        if (r.ok) {
          loadSettings();
        } else if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
          snClearBtn.disabled = false;
          snClearBtn.textContent = 'Clear';
        } else {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = body.detail || ('Error ' + r.status);
          msgEl.className = 'settings-inline-msg err';
          snClearBtn.disabled = false;
          snClearBtn.textContent = 'Clear';
        }
      } catch (_) {
        msgEl.textContent = 'Network error — please retry.';
        msgEl.className = 'settings-inline-msg err';
        snClearBtn.disabled = false;
        snClearBtn.textContent = 'Clear';
      }
    });
  }

  // --- ServiceNow: Test ---
  const snTestBtn = view.querySelector('.btn-servicenow-test');
  if (snTestBtn && CAN_WRITE) {
    snTestBtn.addEventListener('click', async () => {
      const msgEl = document.getElementById('servicenow-test-msg');
      msgEl.textContent = '';
      msgEl.className = 'settings-inline-msg';
      snTestBtn.disabled = true;
      snTestBtn.textContent = 'Testing…';
      try {
        const r = await fetch('/settings/servicenow-credentials/test', { method: 'POST' });
        if (r.status === 403) {
          msgEl.textContent = 'Read-only: authentication not configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (r.status === 404) {
          msgEl.textContent = '✗ No credentials configured.';
          msgEl.className = 'settings-inline-msg err';
        } else if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          msgEl.textContent = '✗ ' + (body.detail || 'connection check failed');
          msgEl.className = 'settings-inline-msg err';
        } else {
          const body = await r.json().catch(() => ({}));
          if (body.ok) {
            msgEl.textContent = '✓ connected'
              + (body.username ? ' as ' + body.username : '')
              + (body.instance_url ? ' — ' + body.instance_url : '');
            msgEl.className = 'settings-inline-msg ok';
          } else {
            msgEl.textContent = '✗ connection check failed';
            msgEl.className = 'settings-inline-msg err';
          }
        }
      } catch (_) {
        msgEl.textContent = '✗ network error';
        msgEl.className = 'settings-inline-msg err';
      }
      snTestBtn.disabled = false;
      snTestBtn.textContent = 'Test connection';
      setTimeout(() => {
        if (msgEl) { msgEl.textContent = ''; msgEl.className = 'settings-inline-msg'; }
      }, 8000);
    });
  }
}
