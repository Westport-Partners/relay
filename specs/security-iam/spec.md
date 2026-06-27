# Domain Spec: Security / IAM

**Owns:** Hub authentication modes, write-access gating, and the IAM surface
the container presents — including support for pre-provisioned roles (BYOR) and
pre-provisioned VPCs (BYOV).

**Primary code:** `hub/auth.py` (`require_writer`, auth mode resolution),
`infra/stacks/compute_stack.py` (BYOR outputs, BYOV lookup, inline-policy
emission, `relay:ecs_{task,execution}_role_arn`, `relay:vpc_id`).
**status.md:** §11. **Related domains:**
[federation-topology](../federation-topology/spec.md) (compute stack where IAM
is wired), [hub-scaling](../hub-scaling/spec.md) (same compute stack),
[integrations-config](../integrations-config/spec.md) (settings writes gated by
`require_writer`), [ui](../ui/spec.md) (Settings screen blocked by auth).

## What it does now

- **Three auth modes** (selected at deploy time via CDK context / env var):
  - `none` — read-only public access; all **writes** return 403.
  - `alb` — OIDC via the ALB's built-in authentication; GitHub IdP supported via
    `relay-setup-oidc.sh`. Write allowlist checked against the OIDC username
    claim.
  - `dev` — development mode; all access permitted locally.
- **`require_writer` decorator** in `hub/auth.py` gates every mutating endpoint.
  In `alb` mode it validates the `X-Amzn-Oidc-Data` JWT and checks the username
  against the configured write allowlist.
- **BYOR (Bring Your Own Role):** accounts that cannot create IAM roles (e.g.,
  government agencies with strict IAM constraints) supply pre-provisioned role
  ARNs via `relay:ecs_task_role_arn` and `relay:ecs_execution_role_arn`. The
  stack imports the roles; `_emit_byor_outputs` prints the required inline-policy
  JSON so the account owner can attach it. Net IAM surface: one task role + one
  execution role (no Lambda exec, no Scheduler-invoke, no PassRole).
- **BYOV (Bring Your Own VPC):** `relay:vpc_id` triggers `from_lookup` so the
  compute stack places the ECS service into a pre-existing VPC instead of
  creating one. For accounts that forbid VPC creation.

## Key entities

- **Auth mode** — `none` | `alb` | `dev`; controls `require_writer` behavior.
- **`require_writer`** — decorator applied to all mutating Hub endpoints.
- **BYOR context keys** — `relay:ecs_task_role_arn`, `relay:ecs_execution_role_arn`.
- **BYOV context key** — `relay:vpc_id`.
- **`_emit_byor_outputs`** — generates inline-policy JSON for the account owner
  to attach to pre-provisioned roles.

## Invariants

- **Writes are always gated:** `require_writer` must be applied to every
  mutating endpoint; `auth_mode=none` must never allow writes.
- **BYOR is the only IAM path in constrained accounts** — new IAM role creation
  is gated on `byor_mode`; the stack never creates roles when BYOR context keys
  are present.
- **Net IAM surface stays minimal:** task role + execution role only; no extra
  execution roles, no PassRole grants.

## Out of scope (non-goals)

- **Production HTTPS + OIDC on the current Westport test deploy** — the auth
  modes are built; the live federated-hub test deploy runs plain HTTP with
  `auth_mode=none`. HTTPS requires a certificate (`relay:certificate_arn` is
  plumbed) and `auth_mode=alb` before any non-test use (status.md 🗺️).
