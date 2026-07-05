---
description: Diagnose a failed or unhealthy Relay deploy — CloudFormation events, ECS health, DLQ, and common failure patterns.
---

You are helping the user diagnose a failed or misbehaving Relay deploy. Work read-only first — gather facts from CloudFormation stack events, ECS service state, stopped-task reasons, and CloudWatch Logs before suggesting any changes or re-deploys.

Read and follow **`prompts/troubleshoot-deploy.md`** in this repo for the diagnostic sequence, symptom-by-symptom remediation steps, and commands to gather state.

**Relay-specific reminders:**
- Always gather CloudFormation stack events and ECS service state before proposing a fix. Blind re-deploys on a stuck stack make CloudFormation state worse.
- `iam:PassRole` denied → switch to `relay-deploy-direct.sh` (see `prompts/deploy-byor.md`).
- Unset or placeholder `RELAY_HUB_IMAGE_URI` → build the image first with `relay-build-hub-image.sh`.
- ALB unreachable → check `RELAY_INTERNAL_ALB`; an internal ALB requires VPN/peering. Scheme changes cannot update in place.
- DLQ growing → the container is receiving events but failing to process them; check CloudWatch Logs.
- Never run `cdk destroy` or `aws cloudformation delete-stack` on `RelayDataStack` without explicit user confirmation — it has `RETAIN` on delete for the DynamoDB table.
