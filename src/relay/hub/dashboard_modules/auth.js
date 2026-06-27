// Auth + hub-scope chip + write-gating. Reads /auth once on load and seeds the
// shared CAN_WRITE / TEAM_TZ state. AUTH_SUBJECT is display-only and stays local.
// Ported from the auth half of dashboard_parts/10-preamble-navmap.js.part (#33).

import { esc } from './helpers.js';
import { CAN_WRITE, setAuth } from './state.js';

let AUTH_SUBJECT = null;

export async function initAuth() {
  try {
    const r = await fetch('/auth');
    if (!r.ok) return;
    const data = await r.json();
    setAuth({ canWrite: data.can_write === true, teamTz: data.timezone });
    AUTH_SUBJECT = data.subject || null;
    renderHubScope(data.hub_scope);
    const indicator = document.getElementById('auth-indicator');
    if (AUTH_SUBJECT) {
      indicator.style.display = 'flex';
      indicator.innerHTML = '<span class="dot">&#9679;</span> ' + esc(AUTH_SUBJECT);
    }
  } catch (_) { /* auth unavailable — leave CAN_WRITE=false */ }
}

// Reflect the deployment scope as a human-facing hub-type chip in the rail.
// Per the two-topology model the three raw scopes collapse to a binary:
// 'central' → Central Hub (org-wide aggregator); everything else → Team Hub.
export function renderHubScope(scope) {
  const el = document.getElementById('hub-scope-badge');
  if (!el) return;
  const isCentral = scope === 'central';
  el.classList.remove('team', 'central');
  el.classList.add(isCentral ? 'central' : 'team');
  el.querySelector('.hub-scope-label').textContent = isCentral ? 'Central Hub' : 'Team Hub';
  el.title = isCentral
    ? 'Central Hub — org-wide aggregator across all teams'
    : 'Team Hub — this team’s incidents and on-call';
  el.hidden = false;
}

export function gateWrite(btnEl, title) {
  // CAN_WRITE is a live binding — reflects the value set by initAuth() once /auth
  // resolves, regardless of when this module was evaluated.
  if (!CAN_WRITE) {
    btnEl.disabled = true;
    btnEl.title = title || 'Read-only: authentication not configured';
  }
}
