// Dashboard entry module — loaded once via <script type="module">. Resolves all
// imports, then runs the top-level init that the concatenated build used to run
// implicitly by fragment order. Init order matches the module map (#33).

import { initAuth } from './auth.js';
import { connect, checkPingAlive } from './stream.js';
import { renderAll, wireTileActivation } from './fleet.js';
import { wireNav, handleHash } from './router.js';
import { closeDrawer } from './incident-drawer.js';
import { setActiveFilter, setActiveEnv } from './state.js';
import { applyEnvToAll, readPersistedEnv, persistEnv } from './env-filter.js';

// 0. Restore the persisted environment lens BEFORE the first render so the
// initial paint is already scoped. Reflect it on the matching control button.
const _restoredEnv = readPersistedEnv();
setActiveEnv(_restoredEnv);
document.querySelectorAll('#env-filter .filter-btn').forEach(b => {
  b.classList.toggle('active', b.dataset.env === _restoredEnv);
});

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

// 7. Global environment lens — sticky across nav (it lives in #topstrip and is
// never hidden) and persisted across reload. Re-renders the active view only.
document.querySelectorAll('#env-filter .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#env-filter .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const env = btn.dataset.env;
    setActiveEnv(env);
    persistEnv(env);
    applyEnvToAll();
  });
});
