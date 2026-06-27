# Domain Spec: Node ↔ Hub Federation

**Owns:** the runtime protocol by which team Nodes register themselves, stream
heartbeats, and forward incident events to a federated Hub — enabling the Hub
to build its fleet big-board and org hierarchy from live registrations rather
than a static catalog.

**Primary code:** `hub/fleet_store.py` (`record_heartbeat`, `apply_incident`,
`build_org_tree`), `node/handler.py` (`_emit_heartbeat`,
`_resolve_oncall_snapshot`), `hub/app.py` (`POST /ingest/heartbeat`,
`_handle_heartbeat`, `/fleet/rollup`), `core/model.py` (`OrgTree`,
`OrgTree.from_registrations`, `OrgTree.org_path`),
`infra/stacks/compute_stack.py` (self-identity env vars).
**status.md:** §13. **Related domains:**
[federation-topology](../federation-topology/spec.md) (the EventBridge bus and
IAM wiring that carries heartbeats), [observability](../observability/spec.md)
(fleet big-board and liveness are fed by heartbeats),
[incident-records](../incident-records/spec.md) (incidents forwarded to Hub),
[scheduling](../scheduling/spec.md) (on-call snapshot pushed with heartbeat).

## What it does now

- **Periodic heartbeat** (`_emit_heartbeat`): the always-on Node container
  heartbeats every minute, carrying its `org_path` (full org ancestry), on-call
  snapshot, deployment metadata, and optional enriched AWS tags. Self-identity is
  injected at deploy time via CDK-set env vars (`RELAY_NODE_APP_NAME`,
  `_DEPLOYMENT_ID`, `_ENVIRONMENT`, `_SERVICE_PATH`, `_ORG_PATH`).
- **HTTP heartbeat ingest** (`POST /ingest/heartbeat`): the same
  `_handle_heartbeat` path accepts heartbeats over HTTP (no SQS / EventBridge
  needed). Used by the collapsed single-container runtime and the demo harness.
  Gated like `/ingest/alarm`.
- **App self-registration on first incident** (`apply_incident`): the Hub
  creates the `FLEET#` entry from incident metadata when it receives the first
  incident for an unknown deployment.
- **Dynamic catalog from registrations:** tiles stay `LIVE` between incidents
  because the container heartbeats continuously. A silent app goes `STALE` then
  `LOST` when the heartbeat stops (liveness classification in `hub/health.py`).
- **Hub builds org tree, stores no catalog:** `build_org_tree` reconstructs
  the full product-line → product → component → deployment hierarchy from
  heartbeat `org_path` values via `OrgTree.from_registrations`. The federated
  Hub never holds a static `catalog.yaml`; all org data comes from the team side
  on each heartbeat.
- **On-call snapshot pushed with heartbeat:** `_resolve_oncall_snapshot` resolves
  the current on-call for the Node's own schedule and embeds it in the heartbeat
  payload. The federated Hub shows this snapshot in the tile drawer; paging
  authority remains Node → Hub → escalation.
- **Fleet rollup endpoint** (`/fleet/rollup`): serves the Hub's assembled org
  tree and tile states to the big-board UI.

## Key entities

- **`FleetStore`** (`hub/fleet_store.py`) — receives heartbeats and incident
  forwards; builds the in-memory tile grid and org tree.
- **`OrgTree`** / **`org_path`** — full ancestry string carried in heartbeats;
  used to rebuild hierarchy at the Hub.
- **`Liveness`** — enum: `LIVE / STALE / LOST`; derived from last-heartbeat age.
- **Heartbeat payload** — `{ org_path, on_call, metadata, deployment_id,
  environment, … }`.

## Invariants

- **Hub stores no static catalog** — the org hierarchy is always built from live
  registrations; a static `catalog.yaml` is only an optional Node-side seed.
- **Paging authority is Node-side** — the Hub reads the on-call snapshot as
  read-only display data; it never modifies the on-call resolution for paging.
- **Self-registration on first incident** — a Node that heartbeats before any
  incident gets a tile via heartbeat; a Node that only forwards incidents still
  gets a `FLEET#` entry on first incident.

## Out of scope (non-goals)

- **Hub-owned org catalog** — the federated Hub deliberately stores no static
  catalog; all hierarchy data flows from Node heartbeats (by design, not a gap).
