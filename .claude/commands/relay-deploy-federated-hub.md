---
description: Deploy an org-wide federated Relay Hub — three stacks, org-scoped bus policy, then wire team deployments to forward escalations up.
---

You are helping the user deploy a Relay federated Hub: the org-wide aggregator that serves a NOC big-board and owns the `relay-hub` EventBridge bus that team accounts forward SEV1/SEV2 escalations up to. The task covers choosing the hub account, deploying all three stacks, verifying the bus policy is correctly org-scoped, and wiring team deployments with `RELAY_HUB_SCOPE=local-federated`.

Read and follow **`prompts/deploy-federated-hub.md`** in this repo for the exact steps, security review checklist, and environment variables.

**Relay-specific reminders:**
- Never deploy the hub into the AWS Organizations management (root) account — this is a hard security rule.
- Always supply `RELAY_ORG_ID`; without it the bus policy falls back to same-account-only ingress (no team account can forward up).
- After deploy, verify `aws events describe-event-bus --name relay-hub --query Policy` shows `aws:PrincipalOrgID` scoped to your org — not open to the world.
- No integration credentials are required before deploying; configure them at runtime on the Settings screen.
- Trust flows one way: team accounts push to the hub bus. The hub never reaches into a team account.
