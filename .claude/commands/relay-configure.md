---
description: Configure Relay — routing rules, escalation policies, contacts, on-call schedule, GitLab config source, and optional OIDC auth.
---

You are helping the user configure a deployed Relay instance. The task covers editing `config/routing.yaml` and `config/escalation.yaml` (the startup seeds), adding contacts via the CLI or dashboard, building the on-call schedule, understanding the seed-vs-DynamoDB runtime model, optionally configuring GitLab as the config source, and setting up OIDC authentication.

Read and follow **`prompts/configure.md`** in this repo for the exact YAML schemas, environment variables, and step-by-step instructions.

**Relay-specific reminders:**
- **No PII in Git, ever.** Config YAML references opaque `contact_id` values only — never names, emails, or phone numbers. Those belong in DynamoDB in the user's own account.
- The `routing_rules:` and `ignore:` blocks in `routing.yaml` are startup seeds only. DynamoDB is the runtime source of truth; live UI edits are never clobbered by a config file change on restart ("DB wins").
- Escalation timers are DynamoDB deadlines, not in-memory timers — do not suggest replacing them with cron or in-memory scheduling.
- Integration credentials (GitLab token, ServiceNow, Teams webhook) belong on the Settings screen at runtime, not in config files or env vars committed to Git.
