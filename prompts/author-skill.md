# Relay — Author an AI Investigation Skill Prompt

You are helping the user add a new runtime AI investigation skill pack to `skills/`. These skill packs are mounted into a node's headless AI triage agent at incident time. They encode the first 5–20 minutes of mechanical triage — the gather phase — as vetted, read-only probes so the agent spends its reasoning budget on synthesis, not on remembering CLI flags.

Canonical references: [`skills/README.md`](../skills/README.md), [`skills/ecs-investigation/SKILL.md`](../skills/ecs-investigation/SKILL.md).

---

## Guardrails (non-negotiable)

- **Read-only, always.** `probe.sh` uses only `describe*` / `list*` / `get*` / `lookup-events` / `filter-log-events` AWS CLI calls. Never a mutating call. The agent's IAM allow-list enforces this too; the script is a vetted second line.
- **Degrade gracefully.** Each section of `probe.sh` is independently wrapped. An error (no permission, resource not found) prints a note and moves on — never aborts the full probe. Mirrors the "AI augments, never gates" guarantee.
- **Findings, not verdicts.** `SKILL.md`'s interpretation section frames output as hypotheses with evidence, never as a confirmed root cause. The human decides.
- **Time-boxed.** Default lookback is `RELAY_WINDOW_MINUTES` (default 60). Do not fan out to large time ranges or make unbounded API calls.
- **No PII.** Skills probe infrastructure state, not personal data.

---

## Shape of a skill pack

Each skill is a directory under `skills/` with exactly two files:

```
skills/<name>/
  SKILL.md     # frontmatter + when-to-use + inputs + probe invocation + interpretation
  probe.sh     # bash script wrapping read-only CLI calls
```

---

## Step 1 — Create the directory

```bash
mkdir skills/<name>
```

Use a descriptive lowercase name with hyphens (e.g. `s3-access`, `rds-slow-queries`).

---

## Step 2 — Write `SKILL.md`

The `SKILL.md` structure (follow this exactly — the agent reads it as a prompt):

```markdown
---
name: <name>
description: >
  One-sentence description of what this skill diagnoses and what it answers.
  Mention the service(s) and symptoms it addresses.
---

# <Human-readable title>

Brief paragraph: what question this skill answers and why it matters.

## When to use

- Bullet list of symptoms / incident types where this skill applies.
- Be specific: which AWS service, which alarm type, which error pattern.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`) |
| `RELAY_APP_NAME` | yes | App name; used to discover the resource when not supplied |
| `RELAY_<NAME>_IDENTIFIER` | no | Specific resource ID; if absent, probe discovers by app name |
| `RELAY_WINDOW_MINUTES` | no | Lookback window (default 60) |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which resource(s) it's investigating and how it found them.
2. **<Section 2>** — what it checks and what it reports.
3. **<Section 3>** — ...

## Required IAM permissions

The probe is read-only. The calling principal needs:

| Action | Required | Used for |
|--------|----------|---------|
| `<service>:Describe<Resource>` | Yes | ... |
| `<service>:List<Resources>` | No | Discovery by app name |

## How to interpret (raw output → hypotheses)

- **Signal A in the output** → Hypothesis X. Cross-check with skill Y.
- **Signal B** → Hypothesis Z. Evidence: ...
- **Everything looks normal** → Problem may be elsewhere; pivot to skill Y / Z.

Always present these as hypotheses with the evidence line that supports them,
never as a confirmed cause.
```

---

## Step 3 — Write `probe.sh`

Structure the script with:

1. A header comment documenting all input env vars.
2. A `resolve_resource` section that finds the resource from app name if the explicit ID is absent.
3. One bash function per probe section, each wrapped in its own subshell or error trap.
4. `RELAY_WINDOW_MINUTES` (default 60) used for any time-range parameters.

Skeleton:

```bash
#!/usr/bin/env bash
# <name> probe — read-only AWS CLI investigation
#
# Inputs (env vars):
#   RELAY_REGION          - AWS region (required)
#   RELAY_APP_NAME        - App name for resource discovery (required)
#   RELAY_<NAME>_ID       - Resource ID (optional; discovered from app name if absent)
#   RELAY_WINDOW_MINUTES  - Lookback window in minutes (default: 60)

set -euo pipefail

REGION="${RELAY_REGION:?RELAY_REGION is required}"
APP="${RELAY_APP_NAME:?RELAY_APP_NAME is required}"
RESOURCE_ID="${RELAY_<NAME>_ID:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"
START_TIME=$(date -u -d "${WINDOW} minutes ago" +%s)000 2>/dev/null \
  || START_TIME=$(date -u -v"-${WINDOW}M" +%s)000  # macOS fallback

echo "=== <Name> Investigation for app: $APP ==="
echo "Region: $REGION | Window: ${WINDOW}m | Start: $(date -d @$((START_TIME/1000)) 2>/dev/null || echo $START_TIME)"
echo

# --- Section 1: Resolve resource ---
echo "--- 1. Resolution ---"
if [[ -z "$RESOURCE_ID" ]]; then
  echo "No RELAY_<NAME>_ID supplied; discovering from app name '$APP'..."
  RESOURCE_ID=$(aws <service> list-<resources> \
    --region "$REGION" \
    --query "<Resources>[?contains(Tags[?Key=='relay:app-name'].Value|[0], \`$APP\`)].Identifier | [0]" \
    --output text 2>/dev/null || echo "")
  if [[ -z "$RESOURCE_ID" || "$RESOURCE_ID" == "None" ]]; then
    echo "WARNING: could not discover resource for app '$APP'; skipping remaining sections."
    exit 0
  fi
  echo "Discovered resource: $RESOURCE_ID"
else
  echo "Using supplied resource: $RESOURCE_ID"
fi
echo

# --- Section 2: <Check> ---
echo "--- 2. <Check> ---"
(
  aws <service> describe-<resource> \
    --region "$REGION" \
    --<resource>-id "$RESOURCE_ID" \
    --output json 2>&1
) || echo "WARNING: describe-<resource> failed (check IAM permissions); skipping."
echo

# --- Section 3: <Check> ---
echo "--- 3. <Check> ---"
(
  aws <service> list-<events> \
    --region "$REGION" \
    --resource-id "$RESOURCE_ID" \
    --start-time "$START_TIME" \
    --output json 2>&1
) || echo "WARNING: list-<events> failed; skipping."
echo

echo "=== End of <name> probe ==="
```

Key points:
- Wrap each section in `( ... ) || echo "WARNING: ..."` so one failure does not abort the rest.
- Never use `aws ... --no-paginate` on calls that could return large result sets without a `--max-items` limit.
- Use `--output json` or `--output text` consistently; avoid `--output yaml` (less script-friendly).
- Test that the script runs with missing optional inputs and only warns, never errors.

---

## Step 4 — Verify read-only

```bash
# Confirm no mutating calls in your probe
grep -E '(create|update|delete|put|start|stop|terminate|modify|attach|detach|enable|disable|tag|untag)' \
  skills/<name>/probe.sh
```

Any match that is not a `describe*`/`list*`/`get*`/`lookup*`/`filter*` must be removed.

---

## Step 5 — Test locally

```bash
RELAY_REGION=us-east-1 \
RELAY_APP_NAME=<your-app> \
./skills/<name>/probe.sh
```

Run with both the optional resource ID set and unset to confirm graceful degradation.

---

## Required IAM permissions

Add only the permissions your skill actually uses to the node's task role. See [`skills/README.md`](../skills/README.md) for the full permission set across all default skills. Grant only what the deployed skill set uses — none of these can mutate state.
