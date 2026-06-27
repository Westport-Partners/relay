# Domain Spec: Detection & Routing

**Owns:** turning a CloudWatch alarm state change into a classified, tagged
incident record — zero-config ingestion through severity assignment and
escalation-policy selection.

**Primary code:** `adapters/aws/cloudwatch_source.py` (parse + tag resolution +
deployment derivation), `core/classifier.py` (routing rule evaluation),
`config/routing.yaml` (policy seed), `config/tag_mapping.py` (template engine),
`adapters/aws/tag_resolver.py` (in-account resource tag fetch).
**status.md:** §1. **Related domains:** [incident-records](../incident-records/spec.md)
(the `Incident` written here), [escalation](../escalation/spec.md) (policy
referenced here is executed there), [integrations-config](../integrations-config/spec.md)
(ignore/routing rules stored in DynamoDB), [observability](../observability/spec.md)
(metrics flag synthetic signals).

## What it does now

- One **zero-config EventBridge rule** (`RelayCloudWatchAlarmRule`) captures every
  CloudWatch alarm state change and delivers it to SQS. No per-alarm registration
  is needed.
- The always-on container's `SQSConsumer` drives the in-process
  `DetectionPipeline`. `POST /ingest/alarm` is the same code path for local/test
  use.
- **Tag resolution** (`AlarmTagResolver`): EventBridge alarm events carry no tags;
  the container fetches them in-account (`cloudwatch:ListTagsForResource`,
  `ec2:DescribeTags` for EC2-sourced alarms). Resource tags (Lambda/SQS/ECS/EC2
  via metric dimensions) are merged resource-first with alarm tags. Best-effort,
  never raises; gated `RELAY_RESOLVE_ALARM_TAGS` (default on).
- **Deployment resolution** (`_derive_deployment_id`): precedence is
  `relay:deployment` → `COMPONENT_ID` → `relay:project` → alarm-name match.
- **Dynamic tag → metadata mapping** (`${tag:NAME}` grammar in `tag_mapping.py`):
  `deployment_defaults.tag_map` in `hierarchy.yaml` declares org-wide conventions
  once; per-deployment `metadata` can override. Missing tag → key skipped, never
  a half-resolved string.
- **Classifier** (`core/classifier.py`) evaluates priority-ordered rules from
  `routing.yaml` (name/namespace/tag/regex match) to assign severity and select an
  escalation policy. DB-backed routing rules (DynamoDB) are the runtime truth;
  `routing.yaml` is the seed and fail-open fallback.
- **Ignore rules** (`_matched_ignore_rule`): matched alarms are dropped before
  persist, page, ticket, or federation. Distinct from suppression — an ignored
  alarm never appears in metrics.
- **Synthetic canary failures** (`SignalSource.SYNTHETIC`) are first-class
  triggers, tagged and surfaced like production alarms.
- **Dedup / idempotent redelivery** — re-delivering the same correlation-id is a
  no-op.
- Adapter `required_metadata` + preflight gate (`config/preflight.py`): adapters
  declare the deployment-metadata keys they need; `relay-preflight` checks every
  catalog leaf and exits non-zero on any miss with actionable suggestions.

## Key entities

- **`Incident`** — written here; `SignalSource` (`CLOUDWATCH` / `SYNTHETIC` /
  `MANUAL`) and `Incident.tags` / `Incident.deployment_metadata` stamped at parse
  time.
- **Routing rule** — `{ priority, match_criteria, severity_override,
  escalation_policy_id, streams, enabled }`.
- **IgnoreRule** — drop predicate; runtime store is DynamoDB, seed from
  `routing.yaml`'s `ignore:` block.
- **`AlarmTagResolver`** — in-account tag fetcher; bound at container boot.
- **`DeploymentDefaults.tag_map`** — org-wide `${tag:NAME}` conventions in
  `hierarchy.yaml`.

## Invariants

- **AWS-free core:** `core/classifier.py` has no `boto3`; all AWS I/O lives in
  `adapters/aws/`.
- **Fail-open routing:** if DynamoDB is unavailable the classifier falls back to
  `routing.yaml`; paging is never broken.
- **Tag resolution is best-effort:** errors are logged, never raised to the
  ingest path.
- **Ignore drops before everything:** an ignored alarm never reaches persist, SNS,
  ticket, or federation.

## Out of scope (non-goals)

- Per-alarm EventBridge rule registration (one rule covers all alarms).
- Manual "start incident" UI button — `SignalSource.MANUAL` exists in the model;
  the create endpoint and button are roadmap (status.md §1).
- Non-AWS signal sources in v1 — AWS is the only monitored substrate.
