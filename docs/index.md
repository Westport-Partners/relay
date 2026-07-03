<img src="assets/westport-logo.png" alt="Westport Partners" height="60" style="margin-bottom: 1rem;">

# Relay

**Open-source incident orchestration for AWS — the Incident Manager replacement you run yourself.**

AWS Systems Manager Incident Manager is end-of-life (closed to new customers 2025-11-07). Relay fills the gap: a lightweight, AWS-native, self-hosted orchestration layer that supplies exactly what AWS abandoned — on-call scheduling, escalation policies, dual-stream incident routing, and a live fleet dashboard — without a new SaaS dependency. You deploy it into your own AWS accounts and it never phones home.

Relay runs as **one always-on container.** A CloudWatch alarm flows in, and the whole life of the incident — classify → page the on-call → escalate → turn the dashboard tile red → file a ticket — happens in that one process, visible in one log stream.

<figure class="screenshot" markdown="span">
  ![The Relay fleet big-board: every tracked deployment on one live grid, color-coded by severity and grouped by org hierarchy.](assets/screenshots/operate/S-FLEET-ALL.png)
  <figcaption>The fleet big-board — every deployment across the org on one live grid, color-coded by severity. A quiet-but-healthy app stays green; a silent one turns red.</figcaption>
</figure>

---

## Why Relay?

- **No SaaS contract.** Deploy into your own AWS accounts. It never phones home. Apache-2.0 open source.
- **AWS-native gap-filler.** Delegates alarm detection, SMS/email transport, ticketing, and runbooks to the AWS services and enterprise tools that already handle them well.
- **Zero-config CloudWatch.** One EventBridge rule catches *every* alarm — existing and future, including Synthetics canaries — with no per-alarm wiring.
- **Installs in locked-down accounts.** A Bring-Your-Own-Role / Bring-Your-Own-VPC mode deploys without creating IAM roles or VPCs — for environments where teams can't.
- **Live fleet big-board.** Every app on one board; a healthy-but-quiet app stays green, a truly-silent one goes NO-SIGNAL red.
- **AI-assisted triage (optional).** A briefing pack is attached at alert time and an after-action review is drafted from the timeline — always async, always labeled, never gating the page.

---

## Quickstart

1. **Install** the CLI + clone with the one-liner, then run the preflight check — see [Install](install.md).
2. **Deploy** a team stack into your AWS account (data + compute), or stand up a federated hub — see [Deploy](deploy.md). In a role-constrained account, use [BYOR](byor.md).
3. **Configure** routing + escalation as code and add your contacts — see [Configure](configure.md).
4. **Operate** from the dashboard: watch the big-board, acknowledge and resolve incidents — see [Operate](operate.md).

Want to try it with no AWS account at all? The [Local development](local-dev.md) harness boots the whole thing offline against DynamoDB-Local in one command.

---

## Explore the docs

| Section | What's inside |
|---|---|
| [Architecture](architecture.md) | How an incident flows through the single container; the three deploy stacks; team vs. federated-hub topologies |
| [Install](install.md) | The one-liner installer, prerequisites, manual clone, preflight, updating |
| [Deploy](deploy.md) | Topologies, the CDK stacks, the deploy workflow, scoped deploys, pause/resume |
| [BYOR](byor.md) | Installing into locked-down / role-constrained accounts (bring your own role / VPC) |
| [Local development](local-dev.md) | Run Relay fully offline, fire test alarms, run the tests |
| [Configure](configure.md) | Config-as-code YAML, the environment-variable reference, severity tiers |
| [Operate](operate.md) | The dashboard, fleet big-board, incident lifecycle, the HTTP API |
| [Scheduling & escalation](scheduling.md) | On-call schedules, role-based availability, escalation policies |
| [Integrations & AI](integrations.md) | GitLab, ServiceNow, Microsoft Teams, SMS, and AI-assisted triage |
| [**Feature Status**](status.md) | **The code-verified source of truth for every feature's build state** |
| [AWS IM Coverage](coverage.md) | Feature-by-feature comparison vs. AWS Incident Manager |
| [Vision](vision.md) | Project direction and roadmap |

---

<p style="font-size: 0.85rem; color: #7a9fa7;">
  Relay is built and maintained by <a href="https://www.westportpartners.com/">Westport Partners</a>.
  Apache 2.0 licensed. See <a href="contributing.md">Contributing</a> and <a href="security.md">Security</a> for how to get involved or report issues.
</p>
