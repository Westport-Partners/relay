# Domain Map

The top-level layout of Relay. Each **domain** is a coherent slice of behavior
with one owning specification. Start here to find where something lives, then
read that domain's spec for the durable "what it does now & why," and the
[Feature Status](status.md) ledger for the code-verified build state.

!!! info "Where the detailed specs live"
    The full per-domain specifications are in the
    [`specs/` tree on GitHub](https://github.com/Westport-Partners/relay/tree/main/specs).
    This page is the published index; the specs are the working source of truth
    and are kept in sync with the code in the same PR as any behavior change.

## How the layers relate

| Layer | Question it answers | Churn discipline |
|---|---|---|
| **GitHub issues** | "what changed, when, and every dead end" | append forever — the messy history lives here, on purpose |
| **`specs/<domain>/spec.md`** | "what does this domain do *now*, and why" | **rewritten clean each change — never appended** |
| **code + docs** | current truth | kept in sync in the same PR |
| **[Feature Status](status.md)** | code-verified state per feature | one row per feature, evidence path re-verified |

The rule that controls bloat: **iteration noise goes in issues; the spec is
always rewritten to describe only the current design.**

## Domains

| # | Domain | Code (primary) | Spec | Status |
|---|---|---|---|---|
| 1 | Detection & routing | `adapters/aws/cloudwatch_source.py`, `core/classifier.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/detection-routing/spec.md) | [§1](status.md) |
| 2 | Contacts | `adapters/aws/dynamo_stores.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/contacts/spec.md) | [§2](status.md) |
| 3 | Scheduling | `core/scheduling.py`, `core/role_resolver.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/scheduling/spec.md) | [§3](status.md) |
| 4 | Escalation | `core/escalation.py`, `node/handler.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/escalation/spec.md) | [§3](status.md) |
| 5 | Engagement / notification | `core/dispatcher.py`, `adapters/aws/sns_notifier.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/engagement/spec.md) | [§4](status.md) |
| 6 | Incident records | `core/model.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/incident-records/spec.md) | [§5](status.md) |
| 7 | ChatOps | `adapters/integrations/teams/` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/chatops/spec.md) | [§6](status.md) |
| 8 | Post-incident analysis | `core/analysis.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/post-incident/spec.md) | [§7](status.md) |
| 9 | Observability / metrics | `core/metrics.py`, `hub/health.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/observability/spec.md) | [§8](status.md) |
| 10 | Federation topology | `infra/app.py`, `infra/stacks/` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/federation-topology/spec.md) | [§9](status.md) |
| 11 | Hub scaling | `infra/stacks/compute_stack.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/hub-scaling/spec.md) | [§10](status.md) |
| 12 | Security / IAM | `hub/auth.py`, `infra/stacks/compute_stack.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/security-iam/spec.md) | [§11](status.md) |
| 13 | Integrations & config | `core/lifecycle.py`, `adapters/integrations/`, `config/` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/integrations-config/spec.md) | [§12, §15](status.md) |
| 14 | Node ↔ Hub federation | `hub/fleet_store.py`, `node/handler.py` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/node-hub-federation/spec.md) | [§13](status.md) |
| 15 | AI capability | `adapters/ai/` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/ai/spec.md) | [§14](status.md) |
| 16 | UI / dashboard | `hub/dashboard.html` | [spec](https://github.com/Westport-Partners/relay/blob/main/specs/ui/spec.md) · [design language](https://github.com/Westport-Partners/relay/blob/main/specs/ui/design-language.md) | cross-cutting |

**UI is cross-cutting.** The single `hub/dashboard.html` renders many domains, so
it has its own domain *and* a binding
[design language](https://github.com/Westport-Partners/relay/blob/main/specs/ui/design-language.md)
that all UI work must conform to.
