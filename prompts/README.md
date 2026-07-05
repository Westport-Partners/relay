# Relay — Prompt Library

This directory contains **paste-ready task prompts** for an AI assistant helping a human install, deploy, configure, or operate [Relay](../README.md). Each file is a self-contained runbook that walks the exact scripts, environment variables, and verification steps for one task.

These prompts are **portable** — paste them into Claude Code, Cursor, Copilot Chat, ChatGPT, or any other AI assistant. Claude Code users can also invoke the matching slash-command wrappers in [`.claude/commands/`](../.claude/commands/) (e.g. `/relay-install`).

For architecture, guardrails, and the config/PII split, read [`AGENTS.md`](../AGENTS.md) first. The prompts here assume you have done so; they link back to `AGENTS.md` for constraints they rely on.

---

## Prompt index

| File | Use this when… |
|------|----------------|
| [`install.md`](install.md) | You are setting up the Relay toolchain on a new machine and need to run the installer, check preflight results, or choose between the one-liner, manual, PyPI wheel, and GHCR image paths. |
| [`deploy-team.md`](deploy-team.md) | You are deploying Relay for a single team (the default topology): CDK bootstrap → image build → synth → deploy data + compute, then verifying the dashboard. |
| [`deploy-federated-hub.md`](deploy-federated-hub.md) | You are deploying an org-wide federated Hub that multiple team deployments forward SEV1/SEV2 escalations up to. |
| [`deploy-byor.md`](deploy-byor.md) | You are deploying into a locked-down account that prohibits creating IAM roles or VPCs — you supply pre-provisioned role + VPC ARNs. |
| [`configure.md`](configure.md) | You need to edit routing/escalation config, add contacts, build the on-call schedule, wire a GitLab config source, or set up OIDC auth. |
| [`troubleshoot-deploy.md`](troubleshoot-deploy.md) | A deploy is failing or the deployed service is unhealthy and you need to diagnose it without making things worse. |
| [`operate-incident.md`](operate-incident.md) | A live incident is in progress and you need to work the dashboard, HTTP API, or integration settings. |
| [`author-adapter.md`](author-adapter.md) | You are adding a new external integration (ticketing, chat, etc.) following the auto-discovery adapter convention. |
| [`author-skill.md`](author-skill.md) | You are adding a new AI investigation skill pack (a read-only `probe.sh` + `SKILL.md`) to the `skills/` runtime pack. |

---

## Conventions

- Prompts are written in the **second person addressed to an AI assistant** ("You are helping the user…").
- Commands shown are the real `scripts/relay-*.sh` scripts and environment variables verified in the repo.
- Do not invent script names, flags, or env vars — if something is not in the prompt, link to the canonical `docs/` page.
- No PII in examples; contact references use opaque `contact_id` values only.
- Do not name specific customers or government agencies — say "government agencies" generically.
