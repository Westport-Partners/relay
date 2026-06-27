# Relay — Feature Status Tracker

**This is the single source of truth for what is built, in progress, being
researched, and on the roadmap.** If you want to know "is feature X done?",
look here first. Every row carries **code evidence** (`file:line`) so the claim
can be re-verified against the repo, not taken on faith.

- **Last verified against code:** see git history for the most recent reconciliation.
- **How to update:** when you change a feature's status, edit the row here in the
  same commit, update the evidence path, and bump "Last verified." Keep the
  rollup lists at the top in sync.
- **Related docs (rationale, not status):** [`coverage.md`](coverage.md) is the
  feature-by-feature comparison vs. AWS Incident Manager. That explains *why*;
  **this file owns *what state it's in*.** Where they disagree with this file,
  this file wins (it is code-verified).

## Status legend

| Mark | Meaning |
|------|---------|
| ✅ **Done** | Implemented and code-verified. Real wiring, not a stub. |
| 🟡 **Partial** | Core path works; specific sub-features or caveats are missing (named in Notes). |
| 🔄 **In progress** | Actively being built right now. |
| 🔬 **Researching** | Under investigation / design; decision pending. |
| 🗺️ **Roadmap** | Agreed direction, not started. |
| ⛔ **Non-goal** | Deliberately out of scope (not a gap). |

---

> Relay runs as one always-on container; detection is in-process, escalation
> timers are DynamoDB deadlines swept by the container; IaC is independent
> Data / Compute / Federation stacks.

## Quick rollup (the only lists you need for "what's left")

### 🔄 In progress
- _(nothing actively mid-build)_

### 🔬 Researching / decision pending
- **Distributed split (separate detection + aggregator processes) + scale-to-zero** — an optional future topology. The internal seams (`DetectionPipeline`, `Stream.CENTRAL`, `TimerPort`/`SchedulerTimerPort`) are kept so splitting the single container into distributed processes would be a transport swap, not a rewrite.
- **AI investigator, deeper tiers (Tier 2/3)** — the live multi-step agent loop (Claude Code + skills doing real account investigation). Interface, briefing slice, and `claude_code` adapter exist; the agent loop is not built. See [`integrations.md`](integrations.md).

### 🗺️ Roadmap (agreed, not started)
- **Per-incident Teams group chat** (Graph API: auto-create chat, add on-call, seed context). Webhook-to-standing-channel is done; the per-incident war room is not. See [`integrations.md`](integrations.md).
- **Manual schedule override authoring UI** (click a grid cell). Backend + `PUT /schedule/override` API are done; the click-to-assign UI is not.
- **Manual incident creation UI** (console "start incident" button). `SignalSource.MANUAL` exists in the model but there is no create endpoint/button.
- **Inbound acknowledgement via SMS reply** (ack by replying to the page). UI/console ack works today; inbound SMS ack is a `TODO` in `node/handler.py`.
- **PDF export of AAR / post-incident report.** AAR generates as markdown; no PDF.
- **Multi-week scheduling fairness.** Auto-schedule balances within a week only; no carryover across weeks.
- **Direct-to-phone SMS, generally available.** Code path exists but is gated (IAM opt-in flag + AWS SNS sandbox exit required) — see [Engagement](#4-engagement--notification).
- **Production hub hardening: HTTPS + auth on the deployed ALB.** The auth modes themselves are built ([§11](#11-security--iam)), but the live federated-hub test deploy runs on plain HTTP with `auth_mode=none` (public read-only) and no TLS certificate. Before any non-test use the ALB needs a certificate + HTTPS listener (`relay:certificate_arn` is already plumbed) and `auth_mode=alb` (OIDC); until then Settings **writes** return 403. A friendly domain (vs. the raw ELB DNS name) belongs here too.

### ⛔ Non-goals (not gaps)
- **Voice engagement** (call-out / phone ack). Scoped out; email + SMS only.
- **Built-in SSM Automation runbook engine.** Relay's answer is AI-assisted remediation + links to existing automation, not an embedded runbook runner.
- **Anthropic-direct AI provider.** Out of scope; Bedrock (default) + OpenAI-compatible umbrella cover the need.

---

## Full ledger

> Evidence paths are illustrative anchors; line numbers drift as code changes —
> treat the **file + symbol** as the durable pointer.

### 1. Detection & routing

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Zero-config CloudWatch alarm capture (one EventBridge rule, all alarms) | ✅ | `infra/stacks/compute_stack.py` (`RelayCloudWatchAlarmRule`, `aws.cloudwatch` / "CloudWatch Alarm State Change" → SQS) | No per-alarm config, unlike AWS IM. The rule delivers to SQS; the always-on container parses + detects in-process. |
| EventBridge event ingest + parse | ✅ | `adapters/aws/cloudwatch_source.py`; `hub/app.py` (`SQSConsumer` → `HubProcessor.handle_event` → `DetectionPipeline.handle_alarm`) | SQS consumer runs the in-process pipeline; `POST /ingest/alarm` is the same path for local/test. |
| Alarm/resource tag resolution (close the `tags={}` gap) | ✅ | `adapters/aws/tag_resolver.py` (`AlarmTagResolver`, `_ec2_resource`); `adapters/aws/cloudwatch_source.py` (`tag_resolver` param, `bind_config`); `node/handler.py` (injects resolver + binds live config before parse); `infra/stacks/compute_stack.py` (`RelayAlarmTagResolution` inline policy, non-BYOR + BYOR-emitted, gated `relay:resolve_alarm_tags`; `_ALARM_TAG_ACTIONS` includes `ec2:DescribeTags`) | EventBridge alarm events carry no tags; the container fetches them in-account — resource tags (Lambda/SQS/ECS/EC2 via metric dimensions) merged resource-first with alarm tags (`cloudwatch:ListTagsForResource`). EC2-sourced alarms resolve instance tags via `ec2:DescribeTags`. Populates `Incident.tags` with `COMPONENT_ID`/`GIT_SHA`/`GITLAB_PIPELINE_URL`/`relay:*`. Best-effort, never raises; gated `RELAY_RESOLVE_ALARM_TAGS` (default on). |
| Deployment resolution from tags (`COMPONENT_ID` join key) | ✅ | `adapters/aws/cloudwatch_source.py` (`_derive_deployment_id`) | Precedence: `relay:deployment` → **`COMPONENT_ID`** (node id or `metadata["component_id"]`) → `relay:project` → alarm-name match. Prod source binds the live `org_tree`/`environments` config. |
| Dynamic tag → deployment metadata mapping (`${tag:NAME}` grammar) | ✅ | `config/tag_mapping.py` (`resolve_template`/`resolve_deployment_metadata`); `core/model.py` (`Incident.deployment_metadata`); `config/schema.py` (`DeploymentDefaults.tag_map` on `HierarchyConfig`); `node/handler.py` (`_handle_alarm` resolves + stamps); `adapters/integrations/gitlab/listener.py` (incident-first) | Catalog `metadata` values are literals or `${tag:NAME}` templates resolved against the incident's resource tags; a global `deployment_defaults.tag_map` in `hierarchy.yaml` declares org-wide conventions (COMPONENT_ID/GIT_SHA/pipeline) once, per-deployment metadata overrides. The container resolves and stamps `Incident.deployment_metadata`; adapters read it incident-first, org-tree as fallback. Missing tag → key skipped (never a half-resolved string). |
| Adapter `required_metadata` + preflight gate + placeholder generator | ✅ | `adapters/registry.py` (`AdapterManifest.required_metadata`/`suggested_tag_map`); `adapters/integrations/gitlab/adapter.py` (`required_metadata=("gitlab_project",)`); `config/preflight.py` (`evaluate_metadata`/`generate_placeholder`/`main`); `pyproject.toml` (`relay-preflight` entry point) | Adapters declare the deployment-metadata keys they need; `relay-preflight` checks every catalog leaf against each enabled adapter's `required_metadata` and classifies each key `literal`/`tag_map`/`template`/`missing`, exiting non-zero on any miss with an actionable `${tag:…}` suggestion. `--generate-placeholders ID=TAG` emits paste-ready catalog stubs. Pure + null-safe (`evaluate_metadata` never raises). |
| Tag-aware incident drawer (resource tags + resolved metadata) | ✅ | `hub/dashboard_modules/incident-drawer.js` (`renderIncident` "Resolved metadata" + "Resource tags" sections; shared `metaValueHtml` from `helpers.js`); `hub/app.py` (`GET /incidents/{id}` returns full incident) | The incident drawer renders `deployment_metadata` (as a kv grid; `pipeline_url`→link, `git_sha`→short+title) then `incident.tags` (as tag chips). The same `metaValueHtml` helper renders `pipeline_url`/`git_sha` consistently in the tile drawer's Metadata section. |
| Classifier → severity + escalation policy (routing.yaml-driven) | ✅ | `core/classifier.py`; `config/routing.yaml` | Priority-ordered rules; name/namespace/tag/regex match. |
| Synthetic canary failures as first-class triggers | ✅ | `adapters/aws/cloudwatch_source.py` (canary / `CloudWatchSynthetics` → `SignalSource.SYNTHETIC`) | Tagged and surfaced. |
| Correlation-id dedup / idempotent redelivery | ✅ | `hub/app.py` (ingest path, "Idempotent ingest") | |
| Manual incident creation | 🟡 | `core/model.py` (`SignalSource.MANUAL`) | Model supports it; **no create endpoint or "start incident" UI button** (roadmap). |
| UI-managed ignore rules (drop matching alarms entirely) | ✅ | `config/schema.py` (`IgnoreRule`, `IgnoreConfig`); `adapters/aws/dynamo_stores.py` (`DynamoIgnoreRuleStore`); `node/handler.py` (`_matched_ignore_rule`); `hub/app.py` (`/rules` routes, `_seed_ignore_rules`, `POST /incidents/{id}/ignore`); `hub/dashboard_modules/rules.js` + `rule-forms.js` (Rules view, Ignore action in incident drawer) | Distinct from `suppression:` (which rate-limits but eventually pages) — an ignored alarm is dropped at the Node before persist, page, ticket, or federation, and is automatically excluded from all metric rollups. `routing.yaml`'s optional `ignore:` block seeds the DynamoDB store on first boot; thereafter DynamoDB is the runtime source of truth and the UI edits it directly ("DB wins"). The Rules screen shows trigger counts, reason/note, created_by/at, and a deviation banner + YAML download when live rules differ from the routing.yaml baseline. |
| UI-managed routing rules (DB-backed; severity / policy / streams per alarm) | ✅ | `adapters/aws/dynamo_stores.py` (`DynamoRoutingRuleStore`); `node/handler.py` (`_effective_routing_config`, `_refresh_routing_rules`); `hub/app.py` (`/routing-rules` routes, `_seed_routing_rules`, `POST /incidents/{id}/route`); `hub/dashboard_modules/rules.js` + `rule-forms.js` (Rules screen routing section, "Routing…" incident drawer action) | Same seed → DynamoDB-truth model as ignore rules. `routing.yaml`'s `rules:` block seeds the DynamoDB store on first boot; thereafter DynamoDB is the runtime source of truth. The classifier reads DB rules via a 30 s in-memory cache (`RELAY_ROUTING_RULES_TTL_SECONDS`, default 30) and **fails open** to the `routing.yaml` config on any DynamoDB error or empty store — paging is never broken. The Rules screen (Routing section, default tab) shows priority, match criteria, severity override, escalation policy, streams, per-rule match count, and an enabled toggle, with create/edit/delete. The "Routing…" incident-drawer action pre-fills a rule from the open alarm; it does **not** auto-resolve the incident (unlike Ignore) and only affects future alarms. Verified live 2026-06-24. |

### 2. Contacts

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Contact model + CRUD + UI | ✅ | `adapters/aws/dynamo_stores.py` (`DynamoContactStore`); `hub/app.py` (`/contacts`) | PII in DynamoDB only, never in Git. |
| Contacts directory UX (filter, role badges, role eligibility at create) | ✅ | `hub/dashboard_modules/contacts.js`; `hub/app.py` (`PUT /availability` empty-roles semantics) | Filter bar (text + role + available-only), per-contact role badges, optional eligible-roles at create (two-call POST `/contacts` + PUT `/availability`), availability expander close button, "On-call" column renamed "Available". Eligible roles persist on the `Availability` record; an explicit empty selection means "no roles". Last verified against code: 2026-06-27. |
| Email + SMS channels modeled | ✅ | `core/model.py`; `adapters/aws/sns_notifier.py` | |
| Channel activation handshake (START/STOP opt-in) | ⛔/🟡 | — | Relies on SNS subscription management instead of IM's per-channel activation. |

### 3. On-call scheduling & escalation

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Role-aware schedule (primary/secondary/manager per slot) | ✅ | `core/scheduling.py` (`Role` enum, role-aware `ScheduledSlot`) | |
| One-click auto-schedule, balanced, no double-booking | ✅ | `core/scheduling.py` (`auto_schedule`, `EXCLUSIVE_ROLES`) | Primary ≠ secondary in a slot. |
| `assignment_at(when, role)` / per-role "on call now" | ✅ | `core/scheduling.py` (`assignment_at`, `assignments_at`) | |
| Timezone-aware "who's on call now" | ✅ | `core/role_resolver.py` (team TZ resolution) | |
| Gap highlighting (uncovered slots flagged per role) | ✅ | `hub/app.py` + Schedule view (`coverage_by_role`) | |
| Multi-stage escalation (timeouts, ack stops it) | ✅ | `core/escalation.py`; `core/model.py` (`EscalationStep`); `node/handler.py` (`_handle_alarm`, `_handle_timeout`, `_record_escalation_event`) | Pure state machine; EventBridge Scheduler one-shot timers. Emits `incident.triggered` + `escalation.page_sent`/`step_advanced`/`exhausted` on the incident timeline. Last verified against code: 2026-06-27. |
| Driving policy captured on the incident | ✅ | `core/model.py` (`Incident.escalation_policy_id`); `node/handler.py:1040` (stamped from `Classification.escalation_policy_id` at classification) | Lets the process-flow view reconstruct the expected ladder even after the policy is edited; legacy rows fall back to the `incident.triggered` event's `policy_id`. Last verified against code: 2026-06-27. |
| Escalation references **roles**, contact_ids as escape hatch | ✅ | `core/escalation.py`; `config/escalation.yaml` | Validator requires ≥1 of roles/contacts. |
| Role→person resolution **wired into the paging path** | ✅ | `node/handler.py:185-190` (`ScheduleRoleResolver(DynamoScheduleStore(...))` default) | Wired in the container's default construction. |
| Ad-hoc schedule overrides (cover-me) stored + respected | ✅ | `dynamo_stores.py` (override CRUD); `scheduling.py` (`apply_overrides`); `hub/app.py` (`PUT/DELETE /schedule/override`) | |
| Manual override **authoring UI** (click a cell) | 🗺️ | API done (`PUT /schedule/override`); UI not built | |
| Multi-week fairness | 🗺️ | `core/scheduling.py` (`auto_schedule` is single-week greedy) | No cross-week carryover. |
| Round-robin rotation lists (hand-ordered) | ⛔ | `core/scheduling.py` | Not a goal — Relay generates a role-aware schedule from per-person availability instead. |

### 4. Engagement / notification

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Email delivery via SNS | ✅ | `adapters/aws/sns_notifier.py` (`send`) | |
| SMS delivery (topic path) | 🟡 | `adapters/aws/sns_notifier.py` (`publish_direct`) | Code exists; **direct-to-phone is gated**: needs `relay:enable_direct_sms` IAM grant + AWS SNS sandbox exit. See [`integrations.md`](integrations.md). |
| Dual-stream dispatch (Team SNS + Central/upstream EventBridge) | ✅ | `core/dispatcher.py` (`DualStreamDispatcher`, `Stream.TEAM`/`Stream.CENTRAL`) | The product notification model — **do not rename `Stream.CENTRAL`.** |
| Add responders mid-incident (manual page / contact test) | ✅ | `hub/app.py` | |
| Engagement status tracking (engaged → acknowledged) | ✅ | `core/model.py` (state machine + timeline) | |
| Inbound ack via SMS reply | 🗺️ | `node/handler.py` (`TODO: wire inbound ack source`) | UI/console ack works today. |
| Voice engagement | ⛔ | — | Deliberate non-goal. |

### 5. Incident records

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Incident model (status, timeline, properties, tags, severity) | ✅ | `core/model.py` (`Incident`, append-only `TimelineEvent`) | SEV1–SEV4 as the impact proxy. |
| Paging + escalation events on the incident timeline | ✅ | `node/handler.py` (`_record_escalation_event`, `_handle_alarm`, `_handle_timeout`) | Four events: `incident.triggered`, `escalation.page_sent` (contacts resolved at page time), `escalation.step_advanced`, `escalation.exhausted`. Idempotent: duplicate/stale timeouts append nothing. Last verified against code: 2026-06-27. |
| State machine TRIGGERED→ACKNOWLEDGED→(ESCALATED)→RESOLVED→CLOSED | ✅ | `core/model.py` (`IncidentState`) | Richer than IM's open/resolved. |
| Incident detail view (timeline, properties, actions) | ✅ | `hub/app.py`; dashboard | |
| Process-flow timeline view (expected ladder vs. actual events) | ✅ | `core/flow.py` (`build_flow`); `hub/app.py:1919` (`GET /incidents/{id}/flow`); `hub/dashboard_modules/incident-drawer.js` (`buildFlowHtml`); `core/model.py` (`Incident.escalation_policy_id`); `node/handler.py:1040` (stamps it at classification) | Drawer renders the expected escalation ladder as a spine (reached rungs filled w/ page timestamp, unreached ghosted, red now-line); `source` = `config`\|`derived`\|`none`. Federated Hubs with no `escalation.yaml` derive the ladder from `escalation.page_sent` events (labeled). Falls back to the flat timeline list when no flow data. Pure AWS-free merge in `core/flow.py`. Last verified against code: 2026-06-27. |
| Synthetic ("test"/"fake") incidents | ✅ | `core/model.py` (`Incident.synthetic`); `adapters/aws/cloudwatch_source.py` (`relay_synthetic` marker); `hub/app.py` (`POST /synthetic/incident`); dashboard Maintenance view | Operator-triggered fake incident runs the full pipeline (paging, tiles, adapters, federation) to verify a fresh deploy. Flagged `TEST` everywhere; counted in metrics on purpose (that's the verification), then cleared via the purge tool. |
| Temporal purge of incidents + metrics | ✅ | `adapters/aws/dynamo_stores.py` (`DynamoIncidentStore.purge_incidents`); `hub/app.py` (`POST /admin/purge`); dashboard Maintenance view | Before/after timestamp bound or synthetic-only; dry-run preview; cascades to companion `ESC#` rows. Writer-gated; refuses an unbounded non-dry-run purge. |

### 6. ChatOps

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Microsoft Teams webhook (incident cards to a standing channel) | ✅ | `adapters/integrations/teams/notifier.py`; `adapters/integrations/teams/listener.py` (`TeamsListener`) | Classic + Power Automate. Driven via the lifecycle seam ([§15](#15-incident-lifecycle-event-seam)); webhook URL UI-set + read fresh per event. |
| Per-incident Teams group chat (Graph API, auto-add on-call) | 🗺️ | — | Designed, not built. See [`integrations.md`](integrations.md). |
| Chat commands (run CLI from chat) | ⛔ | — | Not targeted. |

### 7. Post-incident analysis

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| AI-drafted AAR from timeline + deterministic fallback | ✅ | `core/analysis.py` (`generate_aar`, `_fallback_aar`); `hub/app.py` (`GET /incidents/{id}/aar`) | Falls back to timeline-based markdown when AI off. |
| PDF export of AAR | 🗺️ | — | Markdown only today. |

### 8. Metrics / monitoring

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| KPIs: MTTR, time-to-ack, incident counts | ✅ | `core/metrics.py` (`compute_metrics`); `hub/app.py` (`GET /metrics`) + Metrics view | Synthetic incidents are included (so a smoke test shows up end-to-end); `compute_metrics` also reports `synthetic_total` and the Metrics view flags when figures include test data. |
| Fleet big-board across all apps | ✅ | `hub/fleet_store.py`; dashboard | ✨ Net-new vs AWS IM. |
| Tile detail drawer (click a tile → deployment detail) | ✅ | `hub/dashboard_modules/tile-drawer.js` (`openTile`/`renderTile`); `hub/app.py` (`GET /fleet/{account}/{app}` fills `on_call`); `hub/health.py` (`FleetTile.org_path`/`metadata`/`on_call`) | One data-driven drawer for BOTH topologies — sections (on-call, hierarchy, metadata, AWS tags, open incidents) render only when data is present. Team Hub fills on-call live; federated Hub shows the owning team's pushed `on_call` snapshot. |
| Deployment metadata + AWS tag enrichment (heartbeat `metadata`) | ✅ | `node/enrichment.py` (`TagEnricher`); `node/handler.py` (`_emit_heartbeat`); `infra/stacks/compute_stack.py` (`RelayTagEnrichment` inline policy, gated `relay:enrich_tags`) | The container folds catalog facts (owner/gitlab) + optional Resource Groups Tagging API tags into heartbeat `metadata`. Off by default; best-effort, never breaks the heartbeat. Surfaced in the tile drawer and available to AI skills. |
| Per-app on-call on a federated board | ✅ | `node/handler.py` (`_resolve_oncall_snapshot`, pushed on heartbeat); `hub/app.py` (live fill via `_resolve_now_on_call`) | The team container resolves its own on-call and ships a read-only snapshot up the heartbeat — paging authority stays Node→Hub→escalation. |
| Liveness / NO-SIGNAL detection (lost app goes red) | ✅ | `hub/health.py` (`Liveness` LIVE/STALE/LOST); fed by the container heartbeat (see [§13](#13-node--hub-federation)) | The container heartbeats every minute, so tiles stay LIVE between incidents and a real silence → STALE → LOST. |

### 9. Cross-account / federation topology

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Two topologies: `team` (one container + table) and `federated-hub` | ✅ | `infra/app.py`; `infra/stacks/{data,compute,federation}_stack.py` | Independent Data / Compute / Federation stacks selected by `relay:role`. |
| Independent deploy targets (data once, compute per image) | ✅ | `infra/app.py`; `scripts/relay-deploy.sh` (`--exclusively`, `RELAY_STACK_SELECTOR=data\|compute\|federation`) | A compute redeploy never touches the data plane; fail-fast on missing image; circuit-breaker rollback. |
| Federation bus + org policy (federated-hub only) | ✅ | `infra/stacks/federation_stack.py` (`RelayHubBus`, `CfnEventBusPolicy`) | Self-contained bus resource policy scoped to the org. Team containers forward SEV1/2 up via `events:PutEvents`. |

### 10. Hub scaling

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Always-on container (≥2 tasks, HA), CPU auto-scale to 8 | ✅ | `infra/stacks/compute_stack.py` (`auto_scale_task_count(min=2,max=8)`) | The container is always up, so detection + paging are never gated on a cold start. |
| ECS deployment circuit breaker WITH rollback | ✅ | `infra/stacks/compute_stack.py` (`DeploymentCircuitBreaker(rollback=True)`) | A bad image rolls back instead of wedging CFN. |
| On-demand scale-to-zero | ⛔ | — | Not a goal — the always-on container keeps the hot path warm. A cost-optimization that could be added later, independent of the topology. |

### 11. Security / IAM

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Hub auth modes: none (read-only) / alb (OIDC) / dev; writes gated | ✅ | `hub/auth.py` (`require_writer`) | |
| BYOR — import pre-provisioned roles, emit inline-policy JSON | ✅ | `compute_stack.py` (`relay:ecs_{task,execution}_role_arn`; `_emit_byor_outputs`; new IAM gated on `byor_mode`) | Net IAM surface is one task role + one exec role (no Lambda exec, no Scheduler-invoke, no PassRole). |
| BYOV — import a pre-provisioned VPC | ✅ | `compute_stack.py` (`relay:vpc_id` → `from_lookup`) | For accounts that forbid creating VPCs. |

### 12. Integrations & config

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Incident lifecycle event seam (adapters subscribe to TRIGGERED/RESOLVED/…) | ✅ | `core/lifecycle.py` (`IncidentLifecycleEvent`, `IncidentListener`, `dispatch`); `adapters/registry.py`; `adapters/_support.py`; `hub/app.py` (`HubProcessor._build_listeners`, `dispatch_event`) | Per-adapter `if`-blocks replaced by a uniform listener protocol; per-listener failure isolation preserved. See [§15](#15-incident-lifecycle-event-seam). |
| ServiceNow sink (create ticket via API) | ✅ | `adapters/integrations/servicenow/sink.py` (`create_incident`); `adapters/integrations/servicenow/listener.py` (`ServiceNowListener`) | Real API call; create **and close** via the lifecycle seam. |
| GitLab sink (create issue via API) | ✅ | `adapters/integrations/gitlab/sink.py` (`create_incident`); `adapters/integrations/gitlab/listener.py` (`GitLabListener`) | Real API call. Per-incident project resolution + create/close via the seam. |
| GitLab DORA (incident-type issues tied to environment) | ✅ | `adapters/integrations/gitlab/sink.py` (`issue_type=incident`, `_labels` env tier); `hub/app.py` (`_parse_gitlab_env_tier_map`, `RELAY_GITLAB_ENV_TIER_MAP`) | Issues are `issue_type=incident` with `environment::<tier>` scoped labels so GitLab DORA (time-to-restore, change-failure-rate) populates. Accurate close-on-resolve drives time-to-restore. |
| Close external ticket on resolve | ✅ | `hub/app.py` (`resolve_incident` → `dispatch_event(RESOLVED)`); `adapters/integrations/{gitlab,servicenow}/listener.py` (`close_incident`) | Resolving in the Hub closes the GitLab issue / ServiceNow record. |
| Per-incident GitLab project from catalog/org tree | ✅ | `hub/app.py` (`HubProcessor._resolve_deployment_attr` via `HubState.get_org_tree`); `core/model.py` (`Incident.external_tickets["gitlab_project"]`/`["gitlab_iid"]`) | Leaf node's `metadata["gitlab_project"]` (path) resolved by `deployment_id`; URL-encoded into the API call. |
| GitLab token UI setting (settings store overrides Secrets Manager) | ✅ | `hub/app.py` (`PUT /settings/gitlab-token`, `/settings/gitlab-token/test`, token provider); `hub/dashboard_modules/settings.js` (Settings card) | Runtime-set token, masked on read; overrides the Secrets Manager fallback live. The Test button verifies the full create-issue capability — auth (`GET /user`), `api` scope (`GET /personal_access_tokens/self`), and Reporter+ access on an optional target project (`GET /projects/:id`). Info bubble documents required `api` scope + Reporter role. |
| ServiceNow credentials UI setting (settings store overrides env fallback) | ✅ | `hub/app.py` (`PUT /settings/servicenow-credentials`, `/settings/servicenow-credentials/test`, credential providers); `adapters/integrations/servicenow/sink.py` (provider precedence in `_instance_url`/`_username`/`_password`, `test_connection`); `hub/dashboard_modules/settings.js` (Settings card) | At parity with the GitLab token: instance URL + username + password set on the Settings screen, stored in DynamoDB, masked on read, resolved live per request (overrides the `RELAY_SERVICENOW_*` env/Secrets-Manager fallback). The Test button validates against `GET /api/now/table/incident`. |
| Sink records `*.ticket_created` timeline event | ✅ | `adapters/_support.py` (`record_sink_event`) | Links back to the incident; moved out of `HubProcessor` into the listeners. |
| GitOps config-as-code (routing/escalation in Git) | 🟡 | `config/loader.py` (`refresh()`); `config/local_loader.py` | Reload works; **hot-reload needs an external trigger** (webhook), no autonomous watch. |
| IaC: AWS CDK + portable deploy scripts | ✅ | `infra/`; `scripts/relay-*.sh` | Data/Compute/Federation stacks; deploy logic in scripts, not the pipeline. |
| Local-mock harness (offline, no AWS) | ✅ | `docker-compose.yml` (DynamoDB-Local + bootstrap + container); `scripts/relay-local-bootstrap.py`; `scripts/relay-fire.sh`; `fixtures/alarms/*.json`; `adapters/aws/endpoint.py` (`RELAY_AWS_ENDPOINT_URL`) | `docker compose up` → fire `relay-fire.sh` → watch a tile go red, fully offline. |
| Self-populating demo / test-env harness | ✅ | `tools/testenv/world.py` (seeded Faker org generator); `tools/testenv/harness.py` (HTTP populate + incident drip); `scripts/relay-entrypoint.sh` (`RELAY_DEMO=true`); `docker-compose.yml` | `RELAY_DEMO=true docker compose up` self-fills the board: ~39 tiles across 4 product lines, 25 contacts + schedule, routing/ignore rules, incident drip. Generic-agency data, no real names. |
| App org hierarchy (product line > product > component > deployment) | ✅ | `config/schema.py` (`HierarchyConfig`); `core/model.py` (`OrgTree`) | Built **dynamically from node registrations** at the Hub (see [§13](#13-node--hub-federation)); `catalog.yaml` is now only an optional Node-side seed. |
| Environment isolation (Dev/Test/Prod) | 🟡 | `config/schema.py` (`EnvironmentsConfig`); `cloudwatch_source.py` | Incidents tagged per env; **single table per team** (no per-env partition). |

### 13. Node ↔ Hub federation

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Hub heartbeat receive + fleet store | ✅ | `hub/fleet_store.py` (`record_heartbeat`, `apply_incident`); `hub/app.py` (`_handle_heartbeat`, detail-type `relay.heartbeat`) | Receiver side is fully built. |
| HTTP heartbeat ingest (no SQS/Node needed) | ✅ | `hub/app.py` (`POST /ingest/heartbeat`) | Feeds a `relay.heartbeat` detail straight to `_handle_heartbeat`; gated like `/ingest/alarm` (local runtimes or `RELAY_ALLOW_INGEST=true`). Lets a collapsed single-container runtime — and the demo harness — keep tiles LIVE without EventBridge/SQS. |
| App self-registration on **first incident** | ✅ | `hub/fleet_store.py` (`apply_incident` creates the `FLEET#` entry from incident metadata) | Account/app/env/deployment/service-path captured from the incident. |
| **Dynamic catalog from registrations** | ✅ | `node/handler.py` (`_emit_heartbeat`, `relay_event=="heartbeat"`); container self-identity env in `compute_stack.py` (`RELAY_NODE_APP_NAME`/`_DEPLOYMENT_ID`/`_ENVIRONMENT`/`_SERVICE_PATH`/`_ORG_PATH`) | The always-on container self-registers its own tile on boot and carries the node org identity. Apps register on deploy; tiles stay LIVE. |
| **Richer registration payload → Hub builds hierarchy, stores no catalog** | ✅ | `core/model.py` (`OrgTree.org_path`, `OrgTree.from_registrations`); `hub/fleet_store.py` (`build_org_tree`); `hub/app.py` (`HubState.get_org_tree`, `/fleet/rollup`) | Heartbeat carries `org_path` (full org ancestry); Hub rebuilds the org tree from registrations and serves `/fleet/rollup` from it. **Federated Hub stores no static catalog** — org data always comes from the team side. |

### 14. AI capability

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Pluggable AI provider factory | ✅ | `adapters/ai/factory.py` (`make_assistant`, `RELAY_AI_PROVIDER`) | |
| Bedrock adapter (`invoke_model`) | ✅ | `adapters/ai/bedrock_assistant.py` | Default provider. |
| Bedrock Converse adapter | ✅ | `adapters/ai/bedrock_converse.py` | Any Bedrock model, one schema. |
| OpenAI-compatible adapter (base_url + key from Secrets Manager) | ✅ | `adapters/ai/openai_compat.py` | Unlocks OpenAI/Azure/Gemini/local/OpenRouter. |
| Claude Code adapter (shells to headless `claude` CLI) | ✅ | `adapters/ai/claude_code_assistant.py` | Read-only allow-list; graceful degradation. |
| `AICompletion` result type (text/model/tokens/provider) | ✅ | `adapters/base.py` | Enables cost/usage telemetry. |
| t=0 AI briefing pack auto-attached on TRIGGERED | ✅ | `hub/app.py` (`_attach_ai_brief`); `GET /incidents/{id}/brief` | Async, labeled, never gates paging. |
| AI investigator deeper tiers (Tier 2/3 agent loop) | 🔬 | [`integrations.md`](integrations.md) | Interface + briefing built; multi-step live investigation is future work. |
| Anthropic-direct provider | ⛔ | — | Out of scope. |

### 15. Incident lifecycle event seam

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| `IncidentLifecycleEvent` (TRIGGERED/ACKNOWLEDGED/ESCALATED/RESOLVED) + `IncidentListener` protocol | ✅ | `core/lifecycle.py` | In-process pub/sub; cross-account routing still EventBridge (this only decouples local dispatch). |
| `dispatch()` fans events out with per-listener failure isolation | ✅ | `core/lifecycle.py` (`dispatch`) | A listener that raises is logged and skipped; others still run. |
| Adapters wrapped as listeners (GitLab/ServiceNow/Teams/AI brief) | ✅ | `adapters/integrations/<name>/listener.py` (+ `_support.AIBriefListener`) | Each decides what an event means to it; assembled via the registry. |
| Adapter boundary cleanup (Option A) | ✅ | `core/settings.py` (`SettingsKey`); `adapters/_support.py`; `adapters/integrations/<name>/sink.py` (`from_env`, `GitLabSink.test_token`); `adapters/integrations/teams/notifier.py` (`build_test_card`/`send_test`) | Each adapter owns its env/config loading + HTTP knowledge; the Hub injects only a secret-fetcher; settings keys centralized; settings test endpoints delegate to the adapters. Data-model generalization is Option B. |
| Adapter data-model generalization (Option B) | ✅ | `core/model.py` (`Incident.external_tickets` + `get_ticket`/`set_ticket`; `OrgNode.metadata` holds routing keys; `_org_node_to_payload`/`_payload_to_org_node`); `adapters/aws/cloudwatch_source.py` (`relay:project` tag); `hub/app.py` (`_resolve_deployment_attr` reads metadata) | Core models carry no per-integration columns: external-ticket ids live in `Incident.external_tickets`, GitLab project in `OrgNode.metadata["gitlab_project"]`, the CloudWatch tag is the generic `relay:project`. Adding a 4th integration now never edits a core model. |
| Standard adapter packaging + auto-discovery | ✅ | `adapters/registry.py` (`AdapterManifest`, `AdapterContext`, `discover_manifests`, `build_listeners`); per-adapter packages `adapters/integrations/{gitlab,servicenow,teams}/` (`adapter.py` MANIFEST + `sink.py`/`listener.py`/`README.md`); `adapters/integrations/README.md` (donor contract) + `adapters/integrations/_template/` skeleton | One folder per adapter; the Hub discovers any package exposing a `MANIFEST` (no Hub edit to add one). Discovery scans ONLY `integrations/`, so `aws/` (substrate) + `ai/` (providers) are never scanned — no skip-list. Enables donated adapters that plug into the standard interfaces. |
| Hub emits TRIGGERED on ingest, RESOLVED on resolve | ✅ | `hub/app.py` (`_handle_incident` via `_STATE_TO_LIFECYCLE_EVENT`, `resolve_incident`) | State-to-event mapping fans out uniformly; no per-adapter `if`-blocks. |
| Hub emits ACKNOWLEDGED on ack, ESCALATED on escalation | ✅ | `hub/app.py` (`acknowledge_incident` dispatch; `_STATE_TO_LIFECYCLE_EVENT[ESCALATED]`); `node/handler.py` (`_handle_timeout` sets `state=ESCALATED` before re-emit) | The container flips the incident to ESCALATED on a paging timeout so the Hub sees a real transition (dedup suppresses repeat ESCALATED). All four lifecycle events are available to listeners. |
