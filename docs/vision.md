# Relay — Vision

**Status:** Living document

---

## What Relay is

AWS Systems Manager Incident Manager is end-of-life. Teams running on AWS — especially in
regulated and government environments — need a replacement for on-call, escalation, paging, and
incident coordination that they can **own and run themselves**.

Relay is that replacement: a **self-hosted, AWS-native, open-source** incident orchestration
tool. It supplies only the orchestration layer AWS dropped (on-call scheduling, escalation,
dual-stream routing, fleet status) and delegates everything else to the AWS services and
enterprise systems that already do it well. You deploy it into your own accounts; it never
phones home, and there's nothing to buy.

It's maintained by [Westport Partners](https://www.westportpartners.com/) and released under
Apache-2.0.

## Positioning

- **What it is:** the on-call / escalation / paging / fleet-status layer AWS removed —
  self-hosted, deployed into your own accounts.
- **Who it's for:** AWS teams (especially regulated / government) that lost Incident Manager and
  want something they own, not another SaaS contract.
- **Why it's credible:** built from real-world operation of ~200 applications across AWS, and
  designed around the constraints those environments actually impose — account isolation,
  locked-down IAM, network segmentation.
- **One-liner:** *Relay — open-source incident orchestration for AWS. The Incident Manager
  replacement you run yourself.*

## Design principles

- **You own it.** Self-hosted; no vendor, no data leaving your boundary.
- **Installs in locked-down accounts.** A first-class adoption requirement, not an afterthought
  (see below).
- **Zero-config CloudWatch.** One EventBridge rule captures every alarm, no per-alarm wiring.
- **Dual-stream by design.** One alarm pages the owning team *and* notifies central monitoring
  in parallel.
- **Config as code, no PII in Git.** Escalation/routing live in Git; contacts live in
  your own account.
- **AWS-first, modular.** Concrete AWS implementations sit behind clean interfaces.

## The #1 adoption gate: locked-down IAM

The make-or-break requirement for regulated/government adoption. In strict environments,
application teams often **cannot create IAM roles**, **cannot attach managed policies**, and get
a fixed set of pre-provisioned roles per account that they may modify **only via inline policies
and trust relationships**. Some environments also forbid creating VPCs.

Relay supports a **Bring-Your-Own-Role / Bring-Your-Own-VPC** mode for exactly this: supply
existing role and VPC identifiers, and Relay imports them instead of creating any — emitting the
exact inline-policy and trust JSON for an administrator to apply. See [byor.md](byor.md). A tool
that can't be installed in the target environment delivers no value, so this is treated as a
top-priority capability.

## Branding

Relay uses the Westport Partners brand (mountain-range mark, teal palette
`#005b6d`/`#007489`/`#218993`/`#7a9fa7`) tastefully — a visible but unobtrusive attribution in
the README and the dashboard footer. Status colors in the dashboard stay colorblind-safe and
distinct from the brand teal so health always reads clearly. See [TRADEMARK.md](https://github.com/Westport-Partners/relay/blob/main/TRADEMARK.md)
for mark usage.

## License

Apache-2.0 — permissive, contributor- and adopter-friendly, with an explicit patent grant and
no copyleft obligations that would deter adoption. The code license does not grant rights to the
Westport Partners marks; see [TRADEMARK.md](https://github.com/Westport-Partners/relay/blob/main/TRADEMARK.md).

## Contributing

Relay is open to contributions — see [CONTRIBUTING.md](https://github.com/Westport-Partners/relay/blob/main/CONTRIBUTING.md). Feedback from real
deployments is especially valuable for hardening the locked-down install paths.
