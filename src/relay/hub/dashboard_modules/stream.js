// SSE connection with auto-reconnect + the "connection lost" liveness banner.
// Feeds the shared `tiles` Map and triggers a fleet re-render on each event.
// Also debounces a refresh of the incident store on each fleet delta so open/
// history lists converge with the board without a manual tab re-click.
// Ported from the connection/SSE half of dashboard_parts/10-preamble-navmap.js.part (#33).

import { tiles } from './state.js';
import { renderAll } from './fleet.js';
import { refresh as refreshIncidentStore } from './incident-store.js';

// Trailing-edge 1 s debounce: incident-store refresh after a fleet delta.
// Multiple rapid deltas collapse into one fetch.
let _refreshTimer = null;
function _debouncedStoreRefresh() {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(refreshIncidentStore, 1_000);
}

let lastPingAt = Date.now();
const PING_TIMEOUT_MS = 45_000; // 3× ping interval (15s) before banner

const connStatus = document.getElementById('conn-status');
const connBanner = document.getElementById('conn-banner');

function setConnStatus(state) {
  connStatus.className = state;
  connStatus.textContent = { ok: 'Live', warn: 'Connecting…', lost: 'Lost' }[state] || state;
}

export function checkPingAlive() {
  if (Date.now() - lastPingAt > PING_TIMEOUT_MS) {
    connBanner.classList.add('visible');
    setConnStatus('lost');
  } else {
    connBanner.classList.remove('visible');
  }
}

export function connect() {
  setConnStatus('warn');
  const es = new EventSource('/stream');

  es.addEventListener('snapshot', e => {
    lastPingAt = Date.now();
    setConnStatus('ok');
    connBanner.classList.remove('visible');
    const data = JSON.parse(e.data);
    tiles.clear();
    data.forEach(t => tiles.set(t.account_id + '/' + t.app_name, t));
    renderAll();
  });

  es.addEventListener('delta', e => {
    lastPingAt = Date.now();
    const t = JSON.parse(e.data);
    tiles.set(t.account_id + '/' + t.app_name, t);
    renderAll();
    // A tile delta signals a possible incident state change — refresh the
    // incident store (trailing-edge debounced) so open/history lists converge
    // with the board without requiring a manual tab re-click.
    _debouncedStoreRefresh();
  });

  // Named keepalive 'ping' event (~every 15s) — refresh the liveness timer so
  // the "connection lost" banner only fires when pings genuinely stop. (SSE
  // ': ' comments fire NO JS event, so the server sends a real named event.)
  es.addEventListener('ping', () => {
    lastPingAt = Date.now();
    setConnStatus('ok');
    connBanner.classList.remove('visible');
  });

  es.onopen = () => {
    lastPingAt = Date.now();
    setConnStatus('ok');
    connBanner.classList.remove('visible');
  };

  es.onerror = () => {
    setConnStatus('warn');
    es.close();
    setTimeout(connect, 3_000);
  };
}
