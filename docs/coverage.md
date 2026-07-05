# AWS Incident Manager → Relay coverage matrix

> **Status lives in [`status.md`](status.md).** This document is the
> *capability comparison vs. AWS Incident Manager* (does Relay match the
> original, and where does it go beyond). For the authoritative, code-verified
> **build state** of each feature (done / partial / in progress / researching /
> roadmap), see **[`status.md`](status.md)** — if the two disagree, status.md
> wins.

**Purpose.** Relay exists to replace AWS Systems Manager Incident Manager
(end-of-life, closed to new customers). This document tracks, feature by
feature, how completely Relay covers what AWS Incident Manager did — and where
Relay deliberately goes **beyond** the original. It is a primary acceptance
artifact for the project: "have we built a proper replacement?"

Source of truth for the AWS side is the documentation mirror in
[`aws_incident-manager/`](aws_incident-manager/README.md) (captured 2026-06-21).
Relay's side reflects the code in this repo as of the date below.

**Last updated:** 2026-06-21 (post gap-closing sprint: metrics, ITSM sinks,
schedule overrides, SMS IAM, AI briefing + AAR)

## Legend

| Mark | Meaning |
|------|---------|
| ✅ **Full** | Relay covers this as well as or better than AWS Incident Manager |
| ⭐ **Enhanced** | Relay covers it **and improves on** the AWS original (see note) |
| 🟡 **Partial** | Core works; caveats or missing sub-features noted |
| 🔵 **Planned** | Designed and documented, not yet implemented |
| ❌ **Gap** | Not present; a known hole vs. AWS Incident Manager |
| ✨ **Net-new** | Relay capability AWS Incident Manager never had |

## Scorecard

| Category | Status |
|----------|--------|
| Contacts | 🟡 Partial (SMS delivery gated in live deploy) |
| On-call scheduling | ⭐ Enhanced |
| Escalation | ⭐ Enhanced |
| Engagement / notification | 🟡 Partial (email/Teams live; SMS gated; no voice) |
| Incident records | ✅ Full |
| Routing / detection | ⭐ Enhanced |
| Runbooks / automation | ❌ Gap (AI-assisted remediation planned instead) |
| ChatOps | 🟡 Partial (Teams webhook live; per-incident chat planned) |
| Post-incident analysis | ✅ Full (AI-drafted AAR + deterministic fallback) |
| Metrics / monitoring | ✅ Full KPIs (MTTR/TTA/counts) + ✨ fleet big-board |
| Cross-account / region | ⭐ Enhanced (federation without org approval) |
| Security / IAM | ⭐ Enhanced (BYOR/BYOV for locked-down accounts) |
| Tagging | 🟡 Partial |
| Integrations (ITSM/chat/IaC) | ⭐ Enhanced (ServiceNow + GitLab ticket APIs live; GitOps/CDK) |
| Post-incident **AI investigation** | ✨ Net-new (briefing pack + AAR built) |

---

## 1. Contacts

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Contacts with name/alias + engagement plan | ✅ | `Contact` model + `DynamoContactStore` CRUD; full contacts UI (list, add/edit/delete, sortable). PII in DynamoDB only, never in Git. |
| Channels: email, SMS, **voice** | 🟡 | Email + SMS modeled and wired via SNS. **Voice: ❌ deliberate non-goal** (research scoped out voice). **SMS delivery is gated in the live deploy** (task role lacks `sns:Publish` + SNS sandbox) — code path exists, not yet enabled. |
| Channel activation codes / opt-out (START/STOP) | ❌ | Relay relies on SNS subscription management rather than IM's per-channel activation handshake. |
| Per-channel staged engagement (wait N min) | ✅ | Expressed through escalation steps + timeouts (see §3) rather than per-contact channel staging. |

## 2. On-call scheduling ⭐

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| On-call schedules with rotations | ⭐ | Relay uses a **role-aware generated schedule** (primary/secondary/manager per shift) built from each person's availability, rather than hand-ordered rotation lists. |
| Shift recurrence (daily/weekly/monthly), ≥30-min shifts | 🟡 | Relay uses **three fixed 8-hour shifts** (night/day/evening). Simpler and gap-friendly; arbitrary shift lengths are not supported (deliberate v1 simplification). |
| Time-zone aware, DST handling | ✅ | Team wall-clock via `RELAY_TZ`; "who's on call now" resolves in local time. |
| Coverage preview / who's-on-call-now | ⭐ | Live grid **plus explicit gap highlighting** — uncovered slots are surfaced in red per role, not silently empty. AWS shows coverage but Relay treats a gap as a first-class operational warning. |
| Balanced fill across people | ✨ | **One-click auto-schedule** balances load across eligible people with a no-double-booking rule (primary≠secondary in a slot). AWS requires manual rotation construction. |
| Up to 8 rotations / 30 contacts; overrides; copy schedule | ✅ | No hard caps in Relay. **Ad-hoc overrides (cover-me) are shipped**; multi-week fairness is not yet built (planned). |

**Enhancement summary:** Relay turns scheduling from "define ordered rotations"
into "people declare availability + roles, software generates a balanced,
gap-flagged schedule." Escalation pages a *role*, resolved to the current person
at page time (see §3).

## 3. Escalation ⭐

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Multi-stage escalation, per-stage duration | ✅ | `EscalationPolicy` → ordered `EscalationStep`s with `timeout_minutes`; pure state machine in `core/escalation.py`. |
| Mix contacts + on-call schedules per stage | ⭐ | Steps page **on-call roles** (resolved via the schedule at page time) **or** explicit contact_ids as an escape hatch. Policy files never name people, so they stay stable as who's-on-call changes. |
| Acknowledge stops escalation | ✅ | `acknowledge()` cancels the deadline and halts progression. |
| Timer mechanism | ✅ | Durable DynamoDB deadlines, fired by the container's ~30s sweep loop — survive a restart/redeploy mid-incident. |

**Enhancement summary:** role-based paging (`role → schedule → person`) is
cleaner than AWS's fixed-contact or pre-named-rotation stages. The resolver is
wired into the default paging path, resolving the on-call role to a person against
the shared schedule table at page time.

## 4. Engagement / notification

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Engage via SMS / email / voice; ack via phone/SMS/email | 🟡 | Email live; SMS code-complete but gated in live deploy; **voice is a non-goal**. Inbound ack via SMS reply is **planned** (UI/console ack works today). |
| Add responders mid-incident | ✅ | Manual page / contact test from the Hub UI. |
| Engagement status tracking (engaged→acknowledged) | ✅ | Incident state machine + timeline track ack with actor + timestamp. |

## 5. Incident records ✅

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Incident record: title, status, timeline, properties | ✅ | `Incident` model + `DynamoIncidentStore`; append-only `TimelineEvent` audit trail. |
| Impact levels 1–5 | ✅ | Relay uses **SEV1–SEV4** severity (4 tiers) as the impact proxy. Equivalent concept, fewer levels by design. |
| Status open/resolved (+ Relay adds states) | ⭐ | Relay state machine is richer: TRIGGERED → ACKNOWLEDGED → (ESCALATED) → RESOLVED → CLOSED. |
| Notes / comments, duration, tags, dedup | ✅ | Timeline events serve as notes; duration derivable; tags carried; correlation-id dedup + idempotent ingest. |
| Incident detail tabs (overview/diagnosis/timeline/…) | ✅ | Hub incident detail view with timeline, properties, actions. |

## 6. Routing / detection ⭐

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| CloudWatch alarm → incident | ⭐ | **Zero-config**: one EventBridge rule captures *every* alarm in the account. AWS requires a per-alarm "Start incident" action. |
| EventBridge event-based creation | ✅ | Same ingest path; classifier maps events to severity + policy. |
| Manual incident creation | 🟡 | `MANUAL` signal source supported; console "start incident" button is planned UI. |
| Deduplication | ✅ | Correlation-id keyed; idempotent redelivery handling at the Hub. |
| Routing rules (severity/policy selection) | ⭐ | GitOps `routing.yaml`: priority-ordered rules, name/namespace/tag/regex match, severity override. AWS routing is implicit per response plan. |
| Synthetic canary signals | ⭐ | Canary failures are **first-class triggers**, tagged and surfaced on the board. |
| Involved-resources capture | 🟡 | Alarm metadata captured; structured related-resource extraction is lighter than AWS. |
| Alarm-level ignore rules (drop before page/ticket/metrics) | ✨ | **Net-new** — no AWS Incident Manager equivalent. UI-managed ignore rules drop matching alarms entirely at the Node: no incident row, no page, no ticket, no federation, and automatically excluded from all metric rollups. Persistent in DynamoDB; `routing.yaml` `ignore:` block is a startup seed. The Rules screen shows per-rule trigger counts + deviation banner + YAML download. |
| UI-managed routing rules (severity / policy / streams per alarm) | ⭐ | `routing.yaml` `rules:` block now also DB-backed and fully editable in the Rules screen — same seed → DynamoDB model as ignore rules. Priority, match criteria, severity override, escalation policy, streams, and per-rule match count all editable live, no redeploy. Classifier fails open to `routing.yaml` config on any DynamoDB error. See [status.md](status.md) for build state. |

## 7. Runbooks / automation ❌

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| SSM Automation runbooks, auto-start, manual steps, cross-account exec | ❌ | **Not implemented.** Relay's intended answer is different: an **AI investigation agent** (§15) that proposes remediation, plus links out to existing automation — rather than embedding an SSM runbook engine. Tracked as a deliberate strategy difference, not just a hole. |

## 8. ChatOps 🟡

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Chat channels: Slack / Teams / Chime via AWS Chatbot | 🟡 | **Microsoft Teams webhook** is live (incident cards to a standing channel, classic + Power Automate). Slack/Chime not targeted. |
| Per-incident collaboration space | 🔵 | **Per-incident Teams group chat** (auto-create, add on-call members, seed context) is designed (`docs/TEAMS.md`) via Graph API — Phase 2, not built. |
| Chat commands (run CLI from chat) | ❌ | Not implemented. |

**Net-new note:** AWS pushes notifications to a *standing* channel; Relay's
plan is a *per-incident* chat with the right responders auto-added — closer to
PagerDuty-style war rooms.

## 9. Post-incident analysis ✅

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| PIA: overview, metrics, timeline, guided questions, action items, templates, PDF | ✅ | **AI-drafted AAR** from the captured timeline (`core/analysis.py`, `GET /incidents/{id}/aar`) with a deterministic timeline-based fallback. PDF export not yet built. |

## 10. Metrics / monitoring ✅

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| CloudWatch metrics: #created, #resolved, time-to-first-ack, time-to-resolve | ✅ | **MTTR, time-to-ack, and incident counts** via `core/metrics.py` + `GET /metrics` + the Metrics dashboard view. |
| Dashboards | ✨ | Relay ships a **live fleet big-board** across all ~200 apps with **liveness/NO-SIGNAL** detection (a lost app goes red, not invisible) — a capability AWS Incident Manager has no equivalent for. |
| CloudTrail audit logging | 🟡 | Relay's per-incident timeline is an append-only audit trail; control-plane audit relies on standard AWS CloudTrail for the underlying services. |

## 11. Cross-account / cross-region ⭐

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Multi-region replication set, failover | 🟡 | Relay is per-account/per-region; resilience comes from decentralized deploys rather than a managed replication set. |
| Cross-account via AWS RAM (central management account) | ⭐ | Relay's two topologies — **team** (one container + table in the team's account) and **federated-hub** (team deployments forward selected incidents up to an always-on aggregator) — give cross-account aggregation **without org-level RAM grants**, which matters for locked-down orgs. |
| Cross-account runbook / findings / SNS limitations | n/a | Mostly avoided by the decentralized model. |

## 12. Security / IAM ⭐

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Identity/resource IAM policies, service-linked roles | ✅ | Standard IAM; Hub auth modes: `none` (read-only default) / `alb` (OIDC) / `dev`. Write actions gated by `require_writer`. |
| Role creation assumed | ⭐ | **BYOR (bring-your-own-role)** + **BYOV (bring-your-own-VPC)**: import pre-provisioned roles/VPC and emit inline-policy JSON, for accounts that forbid creating roles/VPCs. AWS Incident Manager assumes you can create roles. |

## 13. Tagging 🟡

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| Tag resources; default tags on incidents; tag-based access | 🟡 | **Different mechanism, by design.** AWS IM tags are manual labels for a shared central service, used to categorize resources and scope IAM access by tag. Relay instead treats resource tags as an **automatic classification signal**: `AlarmTagResolver` fetches them from the alarming resource (alarm events carry none), and they drive routing/ignore matches (`tag_filters`) and incident metadata enrichment (`${tag:...}` templates). The two AWS sub-features Relay omits only make sense in a centralized model — a **tag-resource API** (nothing to hand-label; config is GitOps, incidents are your own DynamoDB rows) and **tag-based RBAC** (isolation comes from AWS account boundaries, since each team's incidents live in that team's own account). |

## 14. Integrations 🟡

| AWS Incident Manager | Relay | Notes |
|---|---|---|
| PagerDuty, Jira, ServiceNow (via Service Management Connector) | ✅ | **ServiceNow** + **GitLab** sinks make **real ticket/issue API calls** (`integrations/servicenow/sink.py` → `/api/now/table`, `integrations/gitlab/sink.py` → `/api/v4/.../issues`) and record a `*.ticket_created` timeline event. PagerDuty/Jira not targeted. |
| CloudWatch / EventBridge / SNS / Secrets Manager | ✅ | Fully wired (detection, transport, paging, secrets). |
| IaC: CDK / Terraform / CloudFormation | ⭐ | Relay ships as **AWS CDK** with portable deploy scripts and **GitOps config-as-code** (routing/escalation in Git, hot-reloaded). AWS IM is console-first. |

## 15. ✨ Net-new: AI incident investigation

AWS Incident Manager has **no AI capability**. Relay's headline differentiator
(designed, `docs/AI.md`; not yet implemented) is an **AI investigator that runs
at the team's node account** with read-only access to that account's real
CloudWatch/logs/deploys, assembles a t=0 briefing pack, proposes root-cause
hypotheses, and **pushes findings up** to the central Hub (which can never reach
back into node accounts). Augments, never gates: the page fires immediately and
AI is asynchronous and always labeled.

## 16. ✨ Other net-new capabilities

These have no AWS Incident Manager equivalent:

- **Decentralized clone-and-deploy** — each team owns its deploy; no central approval or org onboarding.
- **Single artifact, two topologies** — the same container runs as a team deployment or an org-wide federated hub by config.
- **Fleet big-board + liveness** — all ~200 apps on one board; lost apps go NO-SIGNAL red.
- **App org hierarchy + service paths** — incidents carry product-line → product → component → deployment ancestry.
- **GitOps config-as-code** — escalation/routing reviewed via MR, hot-reloaded.
- **Federation without org grants** — local Hubs forward up; no `PrincipalOrgID` requirement.
- **Open source** — adoptable without procurement.

---

## Gaps closed (2026-06-21 sprint)

1. ✅ **Metrics/KPIs** — MTTR, time-to-ack, counts via `core/metrics.py` +
   `GET /metrics` + a Metrics dashboard view.
2. ✅ **ITSM sinks** — ServiceNow + GitLab ticket creation implemented; the Hub
   records a `*.ticket_created` timeline event linking back.
3. ✅ **SMS delivery** — clarified (topic path already delivers); opt-in
   `relay:enable_direct_sms` IAM grant for direct-to-phone + `docs/SMS.md`
   documenting the sandbox-exit request. *(Sandbox exit is a manual AWS Support
   request — see SMS.md.)*
4. ✅ **Post-incident analysis** — AI-drafted AAR (`core/analysis.py`) with a
   deterministic timeline-based fallback; `GET /incidents/{id}/aar` + UI.
5. ✅ **AI investigator (Tier-1)** — `AIAssistant` interface + `BedrockAssistant`;
   t=0 briefing pack auto-attached on TRIGGERED; `GET /incidents/{id}/brief` + UI.
6. ✅ **Schedule overrides** — ad-hoc cover-me layer over the generated schedule,
   respected by paging resolution and shown on the grid.

## Remaining work

See **[`status.md`](status.md)** for the full, code-verified roadmap. The
headline open items:

- **Per-incident Teams chat** (🔵) — Graph app registration + group-chat creation (Phase 2).
- **AI investigator, deeper tiers** (Tier 2/3) — live account investigation via
  Claude Code + skills. The interface + briefing slice are built (`claude_code`
  adapter included); the multi-step agent loop is future work.
- **Override authoring UI** — create overrides by clicking a grid cell (backend + API done).
- **Manual incident creation UI** — `SignalSource.MANUAL` modeled; no create endpoint/button.

> Deliberate non-goals (not gaps): **voice** engagement, and a built-in **SSM
> runbook engine** (Relay favors AI-assisted remediation + links to existing
> automation).
