# Domain Spec: Incident Records

**Owns:** the canonical incident model — the data structure that every other
domain reads from and writes to, and the persistence layer that makes it durable.

**Primary code:** `core/model.py` (`Incident`, `TimelineEvent`, `IncidentState`,
`SignalSource`, `OrgTree`), `adapters/aws/dynamo_stores.py`
(`DynamoIncidentStore`), `hub/app.py` (incident CRUD endpoints).
**status.md:** §5. **Related domains:** [detection-routing](../detection-routing/spec.md)
(creates incidents), [escalation](../escalation/spec.md) (advances state),
[engagement](../engagement/spec.md) (writes ack/engaged state),
[observability](../observability/spec.md) (reads timeline + metrics),
[integrations-config](../integrations-config/spec.md) (external ticket ids stored here),
[ui](../ui/spec.md) (incident drawer + table).

## What it does now

- **`Incident`** is the central record: `{ id, state, severity, signal_source,
  tags, deployment_metadata, external_tickets, timeline, synthetic, … }`.
  `SEV1–SEV4` serves as the impact proxy.
- **State machine:** `TRIGGERED → ACKNOWLEDGED → (ESCALATED) → RESOLVED → CLOSED`.
  Richer than AWS Incident Manager's binary open/resolved.
- **Append-only `TimelineEvent` list:** `{ event_type, at, detail }`. Today's
  recorded types: `acknowledged`, `resolved`, `ignored`, `ai.brief`,
  `*.ticket_created`. Events are written by the domain that causes the transition;
  the list is never edited or reordered.
- **`Incident.external_tickets`** — generic dict; adapters store their ticket ids
  here (`gitlab_iid`, `servicenow_sys_id`, etc.) so core models carry no
  per-integration columns.
- **Synthetic incidents** (`Incident.synthetic = True`): operator-triggered fake
  incidents run the full pipeline (paging, tiles, adapters, federation) to verify
  a fresh deploy. Flagged `TEST` everywhere; included in metrics on purpose (that
  is the verification); cleared via the purge tool.
- **Temporal purge** (`DynamoIncidentStore.purge_incidents`): before/after
  timestamp bound or synthetic-only; dry-run preview; cascades to companion
  `ESC#` rows. Writer-gated; refuses an unbounded non-dry-run purge.
- **Incident detail view** rendered by the Hub: timeline, properties, actions
  (ack / resolve / route / ignore / add responder).

## Key entities

- **`Incident`** — the central aggregate; owns state, timeline, tags,
  deployment_metadata, external_tickets, synthetic flag.
- **`TimelineEvent`** — `{ event_type, at, detail }`; append-only.
- **`IncidentState`** — enum: `TRIGGERED / ACKNOWLEDGED / ESCALATED / RESOLVED / CLOSED`.
- **`SignalSource`** — enum: `CLOUDWATCH / SYNTHETIC / MANUAL`.
- **`DynamoIncidentStore`** — sole persistence layer.

## Invariants

- **AWS-free core:** `core/model.py` contains no `boto3`; all DynamoDB I/O is
  behind `DynamoIncidentStore`.
- **Timeline is append-only and immutable** — past events are never edited or
  reordered.
- **`external_tickets` is the only place for integration ids** — no
  per-integration columns on `Incident`.
- **Synthetic incidents count in metrics** deliberately; they are flagged, not
  hidden.

## Out of scope (non-goals)

- Manual "start incident" UI button — `SignalSource.MANUAL` exists in the model;
  the create endpoint and button are roadmap (status.md §1 🗺️).
