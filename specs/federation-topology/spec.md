# Domain Spec: Federation Topology

**Owns:** the two deployment topologies and the IaC that assembles them —
independent Data / Compute / Federation stacks selected by a single config flag.

**Primary code:** `infra/app.py` (stack composition), `infra/stacks/data_stack.py`,
`infra/stacks/compute_stack.py`, `infra/stacks/federation_stack.py`,
`scripts/relay-deploy.sh`.
**status.md:** §9. **Related domains:**
[hub-scaling](../hub-scaling/spec.md) (compute stack internals),
[security-iam](../security-iam/spec.md) (BYOR / BYOV in compute stack),
[node-hub-federation](../node-hub-federation/spec.md) (the runtime protocol
that flows over the topology wired here),
[integrations-config](../integrations-config/spec.md) (portable deploy scripts).

## What it does now

- **Exactly two topologies:**
  - **`team`** — one always-on container + one DynamoDB table. Node and Hub
    roles run in the same process. Full incident pipeline (detection, escalation,
    paging) runs locally; no cross-account forwarding required.
  - **`federated-hub`** — separate Federation stack; team Nodes forward SEV1/2
    events up via `events:PutEvents` to the Hub's EventBridge bus. The Hub
    aggregates the fleet big-board from team heartbeats.
  - `relay:role` in CDK context selects which topology is assembled.
- **Independent stacks:** Data (DynamoDB table, provisioned once), Compute
  (ECS service + ALB, redeployed per image), Federation (EventBridge bus + org
  policy, federated-hub only). A compute redeploy never touches the data plane.
- **Deploy script** (`scripts/relay-deploy.sh`): `--exclusively` and
  `RELAY_STACK_SELECTOR=data|compute|federation` let operators target a single
  stack. Fail-fast on missing image; circuit-breaker rollback on bad deploy.
- **Federation bus** (`RelayHubBus`) uses a `CfnEventBusPolicy` scoped to the
  AWS organization so only member accounts can publish.

## Key entities

- **`relay:role`** CDK context key — `team` | `federated-hub`.
- **Data stack** — DynamoDB table; deployed once per environment.
- **Compute stack** — ECS Fargate service, ALB, auto-scaling, IAM policies.
- **Federation stack** — EventBridge bus + org bus policy (federated-hub only).
- **`RELAY_STACK_SELECTOR`** — env var / CLI flag scoping a deploy to one stack.

## Invariants

- **Exactly two topologies** — `team` and `federated-hub`; no standalone/central
  third mode exists.
- **Compute redeploy is data-safe** — the Data stack is independent; a compute
  update never modifies the DynamoDB table definition.
- **Deploy logic lives in scripts, not the pipeline** — `relay-deploy.sh` is
  portable; CI/CD pipelines call the script.

## Out of scope (non-goals)

- **Distributed split (separate detection + aggregator processes) and
  scale-to-zero** — the internal seams (`DetectionPipeline`, `Stream.CENTRAL`,
  `TimerPort`) are kept so a future split would be a transport swap, not a
  rewrite; the split itself is under research, not built (status.md §10 ⛔/🔬).
