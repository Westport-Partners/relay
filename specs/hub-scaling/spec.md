# Domain Spec: Hub Scaling

**Owns:** the compute availability and scaling model for the always-on container
— ensuring the hot path (detection, paging, timer sweeps) is never gated on a
cold start.

**Primary code:** `infra/stacks/compute_stack.py` (`auto_scale_task_count`,
`DeploymentCircuitBreaker`).
**status.md:** §10. **Related domains:**
[federation-topology](../federation-topology/spec.md) (the compute stack lives
inside the topology assembly), [security-iam](../security-iam/spec.md)
(BYOR/BYOV also in `compute_stack.py`).

## What it does now

- **Always-on, ≥2 tasks (HA):** the ECS Fargate service is configured with a
  minimum task count of 2, so the container is always running — detection,
  escalation timer sweeps, and the paging path are never blocked by a cold start.
- **CPU auto-scale to 8:** `auto_scale_task_count(min=2, max=8)` scales out
  under load (CPU-based target tracking) without operator intervention.
- **Deployment circuit breaker with rollback:** `DeploymentCircuitBreaker(rollback=True)`
  ensures a bad image rolls back automatically instead of wedging the CloudFormation
  stack.

## Key entities

- **`auto_scale_task_count(min=2, max=8)`** — ECS application auto-scaling policy
  in `compute_stack.py`.
- **`DeploymentCircuitBreaker(rollback=True)`** — ECS deployment configuration;
  automatic rollback on health-check failure.

## Invariants

- **Minimum 2 tasks at all times** — the service must never scale to zero; the
  always-on model is the architectural contract that underpins in-process
  detection and DynamoDB-deadline timer sweeps.
- **Rollback is always enabled** — the circuit breaker must not be disabled;
  a wedged deployment is strictly worse than a rollback to the previous image.

## Out of scope (non-goals)

- **On-demand scale-to-zero** — deliberately not a goal; the always-on model
  keeps the hot path warm. A cost-optimization that could be added later,
  independent of topology, but not targeted (status.md §10 ⛔).
