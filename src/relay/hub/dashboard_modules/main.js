// Dashboard entry module — loaded once via <script type="module">. Resolves all
// imports, then runs the top-level init that the concatenated build used to run
// implicitly by fragment order. Init order matches the module map (#33).

import { initAuth } from './auth.js';
import { connect, checkPingAlive } from './stream.js';
import { renderAll, wireTileActivation } from './fleet.js';
import { wireNav, handleHash } from './router.js';
import { closeDrawer } from './incident-drawer.js';
import { setActiveFilter, setActiveEnv } from './state.js';
import { readPersistedEnv, buildEnvFilter } from './env-filter.js';

// 0. Restore the persisted environment lens BEFORE the first render so the
// initial paint is already scoped. Buttons are built by buildEnvFilter() below
// (and again after the SSE snapshot), so no static button reflect is needed.
setActiveEnv(readPersistedEnv());

// 1. Auth (async, fire-and-forget — same as the old inline call).
initAuth();

// 2. SSE stream + liveness banner.
connect();
setInterval(checkPingAlive, 5_000);

// 3. Nav + hash router.
wireNav();
window.addEventListener('hashchange', handleHash);
handleHash();

// 4. Fleet grid tile activation (delegated, survives diff-render).
wireTileActivation();

// 5. Esc closes the drawer.
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

// 6. Fleet filter buttons (scoped to #filter-bar so the env control isn't caught).
document.querySelectorAll('#filter-bar .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    setActiveFilter(btn.dataset.filter);
    renderAll();
  });
});

// 7. Global environment lens — build buttons from live data. Called once here
// (renders just ALL before the SSE snapshot arrives) and again in stream.js
// after each snapshot/delta so new environments appear automatically. Button
// click handlers are wired inside buildEnvFilter() itself.
buildEnvFilter();
