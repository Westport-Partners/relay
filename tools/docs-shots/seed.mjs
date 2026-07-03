// Deterministic capture seed for the Relay demo container.
//
// The demo world (RELAY_DEMO=true) already provides the fleet, contacts, and
// routing rules. This script tops it up with the moving parts a good set of
// documentation screenshots needs, using ONLY the same HTTP API the docs
// describe — no direct DB access, so it stays honest to what a user could do:
//
//   * fire a few CloudWatch-shaped alarms (open incidents, red tiles)
//   * acknowledge one and resolve one (so the drawer shows every lifecycle
//     state and Metrics/History have data)
//   * auto-generate the current week's on-call schedule (gaps to highlight)
//   * create one routing rule so the Rules screen shows a deviation banner
//
// Idempotent enough for repeated runs: it never deletes, and duplicate rules
// are harmless for a screenshot. Safe because it only talks to localhost.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const BASE = process.env.RELAY_BASE_URL || 'http://localhost:8080';
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

async function api(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json;
  try { json = text ? JSON.parse(text) : {}; } catch { json = { raw: text }; }
  return { status: res.status, json };
}

function isoWeekMonday() {
  // Monday of the current UTC week, as YYYY-MM-DD (matches GET /schedule?week=).
  const now = new Date();
  const day = now.getUTCDay(); // 0=Sun..6=Sat
  const diff = (day === 0 ? -6 : 1) - day; // shift back to Monday
  const mon = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + diff));
  return mon.toISOString().slice(0, 10);
}

async function main() {
  const log = (...a) => console.log('[seed]', ...a);

  // 1) Confirm write access — every action below needs it.
  const auth = await api('GET', '/auth');
  if (!auth.json.can_write) {
    console.error('[seed] FATAL: auth.can_write is false. Start the container with '
      + 'RELAY_AUTH_MODE=dev (the demo compose already does).');
    process.exit(1);
  }
  log(`auth ok — subject=${auth.json.subject} mode=${auth.json.mode}`);

  // 2) Fire alarms by replaying the real CloudWatch-event fixtures (the exact
  //    shape scripts/relay-fire.sh posts). The RELAY_DEMO drip also supplies a
  //    stream of org-named incidents; between the two the Incidents list and
  //    red tiles are well populated.
  const fixtures = ['lambda-error.json', 'canary-failure.json'];
  const fired = [];
  for (const name of fixtures) {
    let payload;
    try {
      payload = JSON.parse(readFileSync(resolve(REPO_ROOT, 'fixtures', 'alarms', name), 'utf8'));
    } catch (e) {
      log(`WARN: cannot read fixture ${name}: ${e.message}`);
      continue;
    }
    const r = await api('POST', '/ingest/alarm', payload);
    if (r.status === 200 && r.json.correlation_id) {
      fired.push(r.json.correlation_id);
      log(`fired ${name} -> ${r.json.correlation_id.slice(0, 8)} (${r.json.severity})`);
    } else {
      log(`WARN: fire ${name} returned ${r.status} ${JSON.stringify(r.json).slice(0, 120)}`);
    }
  }

  // 3) Give one incident an ACKNOWLEDGED state and resolve another, so the
  //    incident drawer, History tab, and Metrics all have real material.
  const open = (await api('GET', '/incidents')).json;
  if (Array.isArray(open) && open.length) {
    const ack = open[0];
    await api('POST', `/incidents/${ack.correlation_id}/acknowledge`, {});
    log(`acknowledged ${ack.correlation_id.slice(0, 8)}`);
    if (open.length > 1) {
      const done = open[open.length - 1];
      await api('POST', `/incidents/${done.correlation_id}/resolve`, {});
      log(`resolved ${done.correlation_id.slice(0, 8)} (feeds History + MTTR)`);
    }
  }

  // 4) Auto-generate this week's schedule (docs: Schedule > Auto-schedule).
  const week = isoWeekMonday();
  const sched = await api('POST', `/schedule/auto?week=${week}`);
  const gaps = sched.json?.gaps;
  log(`auto-scheduled week ${week} — gaps=${typeof gaps === 'number' ? gaps : (gaps?.length ?? '?')}`);

  // 5) Force a routing-rules deviation so the Rules screen banner is visible.
  //    (A rule created at runtime that isn't in routing.yaml == deviation.)
  const rule = await api('POST', '/routing-rules', {
    priority: 5,
    alarm_name_prefix: 'api-5xx',
    severity_override: 'SEV1',
    escalation_policy_id: (await firstEscalationPolicy()),
    streams: ['TEAM', 'CENTRAL'],
    enabled: true,
  });
  log(`created routing rule -> status ${rule.status}${rule.json?.rule_id ? ' id=' + rule.json.rule_id : ''}`);

  // 6) Report the resulting state so the capture step knows what's available.
  const finalOpen = (await api('GET', '/incidents')).json;
  const hist = (await api('GET', '/incidents/history')).json;
  const dev = (await api('GET', '/routing-rules/deviation')).json;
  log(`state: open=${arrLen(finalOpen)} history=${arrLen(hist)} deviation=${JSON.stringify(dev).slice(0, 80)}`);
  log('done.');
}

async function firstEscalationPolicy() {
  const r = await api('GET', '/escalation-policies');
  const list = r.json?.policies || [];
  return list[0]?.policy_id || 'default';
}

function arrLen(x) { return Array.isArray(x) ? x.length : (x?.rules?.length ?? '?'); }

main().catch((e) => { console.error('[seed] error', e); process.exit(1); });
