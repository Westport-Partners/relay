# Domain Spec: Integrations & Config

**Owns:** the incident lifecycle event seam, the adapter registry, and the
runtime config model — everything that lets external integrations subscribe to
incident events without modifying the Hub's core logic.

**Primary code:** `core/lifecycle.py` (`IncidentLifecycleEvent`, `IncidentListener`,
`dispatch`), `adapters/registry.py` (`AdapterManifest`, `AdapterContext`,
`discover_manifests`, `build_listeners`), `adapters/_support.py`
(`record_sink_event`, `AIBriefListener`), `adapters/integrations/` (per-adapter
packages), `config/` (loaders, schema, routing seed), `core/settings.py`
(`SettingsKey`).
**status.md:** §12, §15. **Related domains:**
[detection-routing](../detection-routing/spec.md) (routing/ignore rules stored
in DynamoDB, seeded from config), [chatops](../chatops/spec.md) (Teams adapter
subscribes here), [node-hub-federation](../node-hub-federation/spec.md) (Hub
emits lifecycle events on ingest), [incident-records](../incident-records/spec.md)
(`external_tickets` written by adapter sinks), [ui](../ui/spec.md) (Settings
screen for per-integration credentials).

## What it does now

- **Lifecycle event seam** (`core/lifecycle.py`): in-process pub/sub for
  `TRIGGERED / ACKNOWLEDGED / ESCALATED / RESOLVED`. `dispatch()` fans events
  to all registered listeners with per-listener failure isolation — a listener
  that raises is logged and skipped; others continue. Cross-account forwarding
  still uses EventBridge; this seam decouples local dispatch only.
- **Hub emission:** the Hub emits all four lifecycle events:
  - `TRIGGERED` on ingest (`_handle_incident`).
  - `ACKNOWLEDGED` on ack.
  - `ESCALATED` when the Node flips the incident state (timeout path).
  - `RESOLVED` on resolve → triggers ticket close in GitLab/ServiceNow.
- **Standard adapter packaging + auto-discovery:** each integration lives in
  `adapters/integrations/<name>/` and exposes a `MANIFEST` (`AdapterManifest`).
  `discover_manifests()` scans `integrations/` only (never `aws/` or `ai/`);
  the Hub assembles listeners from discovered manifests with no Hub edit required.
  A `_template/` skeleton + `README.md` donor contract supports contributed
  adapters.
- **Active integrations:**
  - **GitLab sink** — creates and closes incident-type issues via the GitLab API.
    Issues carry `environment::<tier>` labels for GitLab DORA (time-to-restore,
    change-failure-rate). Per-incident project resolved from `metadata["gitlab_project"]`.
    Token set on the Settings screen (DynamoDB); overrides Secrets Manager fallback.
    Test button validates auth scope and project access end-to-end.
  - **ServiceNow sink** — creates and closes records via `GET /api/now/table/incident`.
    Credentials (instance URL + username + password) set on the Settings screen,
    stored in DynamoDB. Test button validates against the live API.
  - **Teams notifier** — see [chatops](../chatops/spec.md).
  - **AI brief listener** (`_support.AIBriefListener`) — attaches the t=0
    briefing pack on `TRIGGERED` (see [ai](../ai/spec.md)).
- **Sink event recording** (`record_sink_event` in `adapters/_support.py`):
  each adapter appends a `*.ticket_created` `TimelineEvent` on the incident.
- **GitOps config-as-code:** `routing.yaml` and `escalation.yaml` seed
  DynamoDB on first boot; thereafter DynamoDB is the runtime source of truth.
  Hot-reload (`config/loader.py` `refresh()`) works but requires an external
  trigger (no autonomous watch yet).
- **Local-mock harness:** `docker-compose.yml` + `DynamoDB-Local` + bootstrap
  scripts; fully offline. `RELAY_AWS_ENDPOINT_URL` redirects all AWS calls.
- **Self-populating demo harness** (`tools/testenv/`): `RELAY_DEMO=true docker
  compose up` fills the board with ~39 tiles, 25 contacts, routing/ignore rules,
  and a live incident drip — generic-agency data, no real names.

## Key entities

- **`IncidentLifecycleEvent`** — event types: `TRIGGERED / ACKNOWLEDGED /
  ESCALATED / RESOLVED`.
- **`IncidentListener`** — protocol; each adapter implements `on_event(event)`.
- **`AdapterManifest`** — `{ id, listener_factory, required_metadata,
  suggested_tag_map }`.
- **`SettingsKey`** — centralized enum of all adapter settings keys stored in
  DynamoDB.
- **`record_sink_event`** — shared helper; appends `*.ticket_created` timeline
  event.

## Invariants

- **Per-listener failure isolation:** `dispatch()` must never let one listener's
  exception prevent others from running.
- **Auto-discovery scans `integrations/` only** — `aws/` and `ai/` are never
  scanned; adapters outside `integrations/` are invisible to the registry.
- **Settings-store token overrides Secrets Manager** — runtime-set credentials
  take precedence over env/Secrets Manager fallback, live per request.
- **GitOps seed → DynamoDB truth** — `routing.yaml`/`escalation.yaml` seed only
  on first boot; DynamoDB is the runtime source of truth thereafter.
- **Core models carry no per-integration columns** — ticket ids in
  `Incident.external_tickets`, project paths in `OrgNode.metadata`; adding a
  new adapter never edits `core/model.py`.

## Out of scope (non-goals)

- **Hot-reload without an external trigger** — autonomous config file watch is
  not built; `refresh()` requires an external webhook (status.md §12 🟡).
- **Built-in SSM Automation runbook engine** — Relay's answer is AI-assisted
  remediation and links to existing automation, not an embedded runner
  (status.md ⛔).
