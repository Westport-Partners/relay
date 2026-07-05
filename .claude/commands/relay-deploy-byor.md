---
description: Deploy Relay into a locked-down account using pre-provisioned IAM roles and/or an existing VPC (BYOR/BYOV mode).
---

You are helping the user deploy Relay into an account that prohibits creating IAM roles (`iam:CreateRole` denied) and/or VPCs (`ec2:CreateVpc` denied). The task covers supplying pre-provisioned role ARNs and a VPC ID as CDK context keys, synthesizing to generate the inline-policy JSON, having an administrator apply the policies to the roles, and deploying with `relay-deploy-direct.sh` when `iam:PassRole` is also denied.

Read and follow **`prompts/deploy-byor.md`** in this repo for the exact steps, context keys, and policy application instructions.

**Relay-specific reminders:**
- The IAM surface is exactly two roles: one ECS task role and one ECS execution role. No Lambda, no EventBridge Scheduler role.
- Use `relay-deploy-direct.sh` (not `relay-deploy.sh`) when `iam:PassRole` is denied — it synthesizes templates and submits via `aws cloudformation deploy`, bypassing the CDK bootstrap execution role.
- CDK bootstrap is not required for the `relay-deploy-direct.sh` path.
- Pass the same `-c relay:ecs_*_role_arn` and `-c relay:vpc_id` context keys on every subsequent compute deploy.
- Deploy the data stack first (`RELAY_STACK_SELECTOR=data`) — it creates zero IAM roles and zero VPC resources.
