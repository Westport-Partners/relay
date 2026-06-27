# Relay Domain Map

The top-level layout of the application. Each **domain** is a coherent slice of
behavior with one owning spec. This is the front door: start here to find where
something lives, then read that domain's `spec.md` for the durable "what it does
now & why," and `docs/status.md` for the code-verified build state.

> A reader-facing mirror of this table is published on the docs site at
> [`docs/domains.md`](../docs/domains.md). This file (with the full relative
> cross-links between specs) is the working source; keep the two in sync when
> domains are added or renamed.

## How the three layers relate (read this once)

| Layer | Question it answers | Churn discipline |
|---|---|---|
| **GitHub issues** | "what changed, when, and every dead end along the way" | append forever — the messy history lives here, on purpose |
| **`specs/<domain>/spec.md`** | "what does this domain do *now*, and why" | **rewritten clean each change — never appended.** No dated logs, no "previously we…" |
| **code + `docs/`** | current truth | kept in sync in the same PR (DoD) |
| **`docs/status.md`** | code-verified state (✅/🟡/🗺️…) per feature | one row per feature, evidence path re-verified |

The rule that kills bloat: **iteration noise goes in issues; the spec is always
rewritten to describe only the current design.** If you find yourself appending
"update:" or "v2:" to a spec, stop — rewrite the section clean and let the issue
hold the history.

## Domains

| # | Domain | Code (primary) | Spec | status.md |
|---|---|---|---|---|
| 1 | [Detection & routing](detection-routing/spec.md) | `adapters/aws/cloudwatch_source.py`, `core/classifier.py` | `detection-routing/` | §1 |
| 2 | [Contacts](contacts/spec.md) | `adapters/aws/dynamo_stores.py` (`DynamoContactStore`) | `contacts/` | §2 |
| 3 | [Scheduling](scheduling/spec.md) | `core/scheduling.py`, `core/role_resolver.py` | `scheduling/` | §3 |
| 4 | [Escalation](escalation/spec.md) | `core/escalation.py`, `node/handler.py` | `escalation/` | §3 |
| 5 | [Engagement / notification](engagement/spec.md) | `core/dispatcher.py`, `adapters/aws/sns_notifier.py` | `engagement/` | §4 |
| 6 | [Incident records](incident-records/spec.md) | `core/model.py` (`Incident`, `TimelineEvent`) | `incident-records/` | §5 |
| 7 | [ChatOps](chatops/spec.md) | `adapters/integrations/teams/` | `chatops/` | §6 |
| 8 | [Post-incident analysis](post-incident/spec.md) | `core/analysis.py` | `post-incident/` | §7 |
| 9 | [Observability / metrics](observability/spec.md) | `core/metrics.py`, `hub/health.py` | `observability/` | §8 |
| 10 | [Federation topology](federation-topology/spec.md) | `infra/app.py`, `infra/stacks/*` | `federation-topology/` | §9 |
| 11 | [Hub scaling](hub-scaling/spec.md) | `infra/stacks/compute_stack.py` | `hub-scaling/` | §10 |
| 12 | [Security / IAM](security-iam/spec.md) | `hub/auth.py`, `infra/stacks/compute_stack.py` | `security-iam/` | §11 |
| 13 | [Integrations & config](integrations-config/spec.md) | `core/lifecycle.py`, `adapters/integrations/`, `config/` | `integrations-config/` | §12, §15 |
| 14 | [Node ↔ Hub federation](node-hub-federation/spec.md) | `hub/fleet_store.py`, `node/handler.py` | `node-hub-federation/` | §13 |
| 15 | [AI capability](ai/spec.md) | `adapters/ai/` | `ai/` | §14 |
| 16 | [UI / dashboard](ui/spec.md) | `hub/dashboard_parts/` (assembled by `hub/app.py`) | `ui/` | (cross-cutting) |

**UI is cross-cutting.** The dashboard (`hub/dashboard_parts/`, assembled at serve time) renders many domains, so
it has its own domain *and* a binding [design language](ui/design-language.md)
that all UI work must conform to. When a feature has a UI surface, its domain spec
describes the data contract; the `ui/` spec describes how it looks and behaves.

## Conventions every spec follows

- **AWS-free core** — no `boto3` under `src/relay/core/`. AWS specifics live behind adapters.
- **No agency names** in any spec/code/doc — say "government agencies."
- **Source of truth precedence** when specs and code disagree: **code wins**, then update the spec.
- See [`../.specify/memory/constitution.md`](../.specify/memory/constitution.md) for the full rule set (it points at `CLAUDE.md` + `CONTRIBUTING.md`).
