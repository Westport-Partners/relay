---
description: Deploy Relay for a single team — CDK bootstrap, build image, synth, deploy data + compute, verify DashboardUrl.
---

You are helping the user deploy Relay using the team topology (the default): one ECS Fargate container plus one DynamoDB table in the team's own AWS account. The task covers CDK bootstrap, building and pushing the container image, synthesizing and reviewing templates, deploying data then compute stacks, and verifying the dashboard is reachable.

Read and follow **`prompts/deploy-team.md`** in this repo for the exact steps, environment variables, and verification commands.

**Relay-specific reminders:**
- `RelayComputeStack` fails fast at synth if `RELAY_HUB_IMAGE_URI` is unset or contains `amazonlinux`/`PLACEHOLDER`. Always build the image first.
- The ALB is internal by default (`RELAY_INTERNAL_ALB=true`). If the account has no VPN/peering into the VPC, `RELAY_INTERNAL_ALB=false` is required or the dashboard will be unreachable.
- If preflight warned about `iam:CreateRole` or `ec2:CreateVpc` being denied, stop here and follow `prompts/deploy-byor.md` instead.
- Use `RELAY_STACK_SELECTOR=compute` for image-only updates — never touch the data plane unnecessarily.
- Do not commit PII (names, emails, phones) to any config file — those belong in DynamoDB only.
