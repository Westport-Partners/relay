# Relay — External Integrations & AI Triage

This page covers every external integration Relay ships with: what it does, how to turn it on, and the configuration knobs available to operators.

---

## How integrations work

Relay emits a lifecycle event for each state change in an incident — `TRIGGERED`, `ACKNOWLEDGED`, `ESCALATED`, and `RESOLVED`. Each integration subscribes as a listener and decides what that event means to it (create a ticket, post a card, open a chat). Failure is isolated: a slow or broken integration never delays paging or blocks other listeners.

Every integration lives in its own folder and is auto-discovered at startup. Adding a new integration means dropping a folder with the required adapter interface — no changes to core are needed. Contributors should start from the adapter template in the repo (`adapters/_template/`).

---

## GitLab — Issue Tracking & DORA ✅ live

### What it does

- **Creates a GitLab issue** when an incident is triggered and **closes it** when the incident resolves.
- The target project is resolved per incident from the org hierarchy: the deployment's `gitlab_project` metadata field is used to construct the API call. There is no single hard-coded project — each deployment routes to its own GitLab project.
- Issues are created as `issue_type=incident` with a scoped environment label (`environment::<tier>`) so GitLab's native DORA metrics — **time-to-restore** and **change-failure-rate** — populate automatically.

### DORA tier mapping

Set `RELAY_GITLAB_ENV_TIER_MAP` to map Relay environment names to GitLab environment tiers:

```
RELAY_GITLAB_ENV_TIER_MAP=prod:production,staging:staging,dev:development
```

If a Relay environment is not in the map, the label is omitted for that incident.

### Token setup

Configure the token on the **Settings screen** in the Relay dashboard. The token is masked on read.

| Setting | Detail |
|---------|--------|
| Required scope | `api` |
| Required role | Reporter (on each project Relay will file issues against) |
| Where it's stored | Relay's own DynamoDB settings table (server-side encrypted), set from the Settings screen — no Secrets Manager secret to pre-create |

Use the **Test token** button after saving. The test verifies authentication via `GET /user`; it does not validate per-project write scope — confirm Reporter access on each target project separately.

> **Note:** GitLab can also serve as Relay's flat-file config store (GitOps mode). That is a separate concern; see `configure.md`.

---

## ServiceNow — ITSM Ticketing ✅ live

### What it does

- **Creates a ServiceNow incident record** via the Table API when an incident is triggered and **closes it** on resolve.

### Configuration

Configure ServiceNow on the **Settings screen** in the Relay dashboard — the same way as the
GitLab token. Enter the instance URL, service-account username, and password, then use the
**Test connection** button to verify. The credentials are stored in Relay's own DynamoDB
table (server-side encrypted) and read live at incident time; the password is masked on read.

| Setting | Detail |
|---------|--------|
| Instance URL | e.g. `https://acme.service-now.com` |
| Username | Service-account username |
| Required role | `itil` (or equivalent) — needs to create and update records in the `incident` table |
| Where it's stored | Relay's DynamoDB settings table (server-side encrypted), set from the Settings screen |

If ServiceNow is left unconfigured, the adapter is simply not loaded — it never blocks startup.

**Deploy-time fallback (optional).** For automated/no-UI setups you can instead supply the
credentials as environment variables; a value saved on the Settings screen overrides these.

| Variable | Purpose |
|----------|---------|
| `RELAY_SERVICENOW_INSTANCE_URL` | ServiceNow instance URL |
| `RELAY_SERVICENOW_USERNAME` | Service account username |
| `RELAY_SERVICENOW_SECRET` | Secrets Manager secret *name* holding the password (fetched by the task role) |

---

## Microsoft Teams — Incident Cards & War Rooms

### Standing-channel webhook ✅ live

Relay posts a formatted incident card to a standing Teams channel on every `TRIGGERED`, `ACKNOWLEDGED`, and `RESOLVED` event.

Both webhook styles are supported:

- **Classic incoming webhook** — connector URL from the channel settings
- **Power Automate workflow URL** — workflow-triggered webhook

Configure the webhook URL on the **Settings screen** (`PUT /settings/teams-webhook`). Use the **Test** button to fire a sample card. The webhook URL is read fresh on every event, so updates take effect immediately with no restart.

### Per-incident group chat 🗺️ roadmap

A future release will create a dedicated Teams group chat per incident via the Microsoft Graph API. The chat will auto-add on-call responders and seed context — functioning as a digital war room. This feature is designed but not yet built; it is not available in the current release.

---

## SMS & Email Paging

### Email via SNS ✅ live

Email paging through SNS topic subscriptions works out of the box. No additional grants are needed.

### Topic-subscription SMS ✅ live

SMS delivered via SNS topic subscriptions (users subscribe their phone numbers to the topic) works without additional grants.

### Direct-to-phone SMS 🟡 gated

Sending SMS directly to a phone number (bypassing topic subscription) requires two additional steps:

1. **IAM grant:** the `relay:enable_direct_sms` tag must be set on the task role. This grants `sns:Publish`, which is broad in scope — it is opt-in by design.
2. **SNS SMS sandbox exit:** new AWS accounts start in the SNS SMS sandbox, which restricts SMS to verified destination numbers. You must submit an AWS Support request to exit the sandbox before direct-to-phone SMS works in production.

Neither step is done automatically. Until both are in place, direct-to-phone SMS will silently skip and email/topic-subscription SMS will still fire.

---

## AI-Assisted Triage

### Principle

**AI augments; it never gates.** The page fires immediately on `TRIGGERED`. All AI output is asynchronous, clearly labeled as AI-generated, and attached after the fact. Paging is never delayed waiting for AI.

### Enabling

Set `RELAY_AI_ENABLED=true` in your environment or CDK config. Then select a provider:

| `RELAY_AI_PROVIDER` | Notes |
|---------------------|-------|
| `bedrock` | Default. Uses AWS Bedrock. No extra credentials needed inside AWS. |
| `bedrock-converse` | Bedrock via the Converse API. Preferred for multi-turn use. |
| `openai` | OpenAI-compatible endpoint — covers OpenAI, Azure OpenAI, Gemini, local models, OpenRouter. Set `RELAY_AI_BASE_URL` and `RELAY_AI_API_KEY_SECRET` (Secrets Manager secret name). |
| `claude-code` | Shells to a headless `claude` CLI with a read-only allow-list. Requires the CLI installed in the container. |

Set the model with `RELAY_AI_MODEL_ID`. For `openai`-compatible providers also set:

| Variable | Purpose |
|----------|---------|
| `RELAY_AI_BASE_URL` | Provider base URL |
| `RELAY_AI_API_KEY_SECRET` | Secrets Manager secret name containing the API key |

A direct Anthropic API provider is not needed — Bedrock and the `openai`-compatible adapter cover all Anthropic models.

### What's built today ✅ live

**t=0 briefing pack** — On every `TRIGGERED` event, Relay asynchronously generates an AI briefing and attaches it to the incident. The briefing is available at:

```
GET /incidents/{id}/brief
```

The page fires first. The briefing appears within seconds, labeled `AI-generated`. It never blocks paging.

**After-action review (AAR)** — After an incident resolves, an AI-drafted AAR is generated from the incident timeline. Available at:

```
GET /incidents/{id}/aar
```

When `RELAY_AI_ENABLED=false`, a deterministic fallback AAR is generated from the raw timeline instead.

### What's coming 🗺️ roadmap

A deeper **multi-step AI investigator agent** (Tier 2/3 incidents) that performs live account investigation via the Claude Code CLI and a read-only skill pack (log queries, CloudWatch metrics, resource tags). The interface and briefing slice are in place; the live agent loop is future work.

---

## Adding Your Own Integration

Relay's adapter auto-discovery means you can add an integration without touching core code:

1. Copy `adapters/_template/` to a new folder (e.g. `adapters/pagerduty/`).
2. Implement the adapter interface — at minimum, handle the lifecycle events you care about.
3. Register in the adapter manifest (`adapters/MANIFEST`).
4. Deploy. The new adapter is discovered and loaded automatically.

One failing adapter never affects others. See the template folder for the full interface contract and a stub implementation.
