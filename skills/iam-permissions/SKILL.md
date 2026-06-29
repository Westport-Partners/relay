---
name: iam-permissions
description: >
  Read-only; finds the root cause of AccessDenied errors — recent denied API
  calls from CloudTrail, the principal's attached/inline policies, and an
  iam simulate-principal-policy evaluation of the specific action against the
  resource. Use when logs/errors show AccessDenied, UnauthorizedOperation, or
  "not authorized to perform".
---

# IAM permissions investigation

When a service suddenly starts throwing `AccessDenied` or
`UnauthorizedOperation`, the root cause is almost always one of three things:
a missing grant (action never existed in any policy), an explicit Deny
(SCP, permission boundary, or resource policy), or a grant that disappeared
after a deploy that swapped the task role or an inline policy statement.
This skill pulls all three threads without you hand-crafting the lookup chain.

## When to use

- Logs or errors contain `AccessDenied`, `UnauthorizedOperation`, or
  "is not authorized to perform".
- An ECS task, Lambda function, or other workload suddenly can't reach a
  downstream service (S3, Secrets Manager, SSM, KMS, SQS, …).
- A recent deploy is suspected of dropping a policy permission (correlate
  with `recent-changes`).

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_PRINCIPAL_ARN` | no | The role or user ARN being denied (e.g. the ECS task role ARN). If absent the probe discovers denied principals from CloudTrail in the window. |
| `RELAY_DENIED_ACTION` | no | The specific IAM action to simulate, e.g. `s3:GetObject`. If absent the probe attempts to derive it from the most recent CloudTrail denial. |
| `RELAY_RESOURCE_ARN` | no | The resource ARN for the simulation (default `*`). |
| `RELAY_WINDOW_MINUTES` | no | Lookback window for CloudTrail events (default 60). |

## Run

```bash
RELAY_REGION=... RELAY_PRINCIPAL_ARN=... [RELAY_DENIED_ACTION=...] ./probe.sh
```

The probe prints these sections (each isolated — one failure never aborts the rest):

1. **Resolution** — inputs in effect and what will be discovered vs. provided.
2. **Recent denied calls (CloudTrail)** — all events in the window whose
   `errorCode` contains `AccessDenied` or `Unauthorized`; shows time,
   `EventName`, principal ARN, `errorCode`, and `errorMessage`. This is the
   core section: it names the exact action + principal + resource being denied.
3. **Principal policies** — for a role: `get-role`, attached managed policies,
   inline policy names and full documents. For a user: equivalent user calls.
   Shows what the principal *can* do so you can spot the gap.
4. **Policy simulation** — `iam simulate-principal-policy` for the given (or
   discovered) action against the given (or default `*`) resource. Prints
   `EvalDecision` and the matched/missing statements. Definitively answers
   whether the denial is implicit (missing grant) or explicit (Deny statement).

## Required IAM permissions

The probe is read-only. The calling principal (the investigation agent's role in the
team account) needs the actions below. A missing **Required** permission makes the
probe silently skip that section — output looks like "no results" rather than "denied".

| Action | Required | Used for |
|--------|----------|----------|
| `cloudtrail:LookupEvents` | **Yes — core** | Find recent access-denied events |
| `iam:SimulatePrincipalPolicy` | **Yes** | Allow/deny verdict for an action on a resource |
| `iam:GetRole` | No | Role metadata + permissions boundary |
| `iam:ListAttachedRolePolicies` | No | Managed policies attached to the role |
| `iam:ListRolePolicies` | No | Inline policy names for the role |
| `iam:GetRolePolicy` | No | Inline policy documents for the role |
| `iam:GetUser` | No | User metadata + permissions boundary |
| `iam:ListAttachedUserPolicies` | No | Managed policies attached to the user |
| `iam:ListUserPolicies` | No | Inline policy names for the user |
| `iam:GetUserPolicy` | No | Inline policy documents for the user |

## How to interpret (raw output → hypotheses)

- **Denied CloudTrail call whose action appears in no attached or inline
  policy** → missing grant. Hypothesis: add an inline statement to the
  existing role that allows the specific action on the resource. Do **not**
  suggest creating a new role — this account family uses pre-provisioned roles
  with inline-only policies (see IAM constraints note below).
- **`EvalDecision: explicitDeny`** → an explicit `Deny` somewhere in the
  policy stack (SCP, permission boundary, resource-based policy, or inline
  deny) is blocking even a matching Allow. Flag that the SCP / boundary layer
  needs checking by someone with org-level access; the fix is unlikely to be
  just adding an Allow.
- **`EvalDecision: implicitDeny`** → no policy grants the action. Hypothesis:
  add an inline statement to the existing role.
- **Denied call immediately after a deploy** → the deploy may have replaced
  the task role ARN or removed an inline policy statement. Cross-reference
  with the `recent-changes` skill to confirm the task-def or role change time
  matches the error onset.
- **Multiple principals denied the same action** → the resource policy
  (S3 bucket policy, KMS key policy, Secrets Manager resource policy) may be
  the blocking layer rather than the identity policy. Inspect the resource
  policy in the console or via `get-bucket-policy` / `describe-key` /
  `get-resource-policy`.

Always present these as hypotheses with the evidence line that supports them,
never as a confirmed cause.

## IAM constraints note

This account family cannot create new IAM roles and uses inline-only policies
on a fixed set of pre-provisioned roles. Remediation hypotheses must therefore
be framed as "add an inline statement to the existing role" — never "create a
new role" as the only fix. Flag this constraint explicitly in your findings so
the responder doesn't waste time attempting a forbidden operation.
