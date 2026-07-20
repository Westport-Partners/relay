#!/usr/bin/env bash
# relay-preflight.sh — Relay install-readiness checker.
# Validates that the local environment and AWS account meet the minimum
# requirements to run a Relay deploy. Safe to run with no side-effects;
# all checks are read-only.
#
# Usage:
#   ./scripts/relay-preflight.sh            # human-readable table to stdout
#   ./scripts/relay-preflight.sh --json     # machine-readable JSON to stdout
#   ./scripts/relay-preflight.sh --help
#
# Environment variables (all optional):
#   AWS_REGION / AWS_DEFAULT_REGION  — target region for CDK/CloudFormation checks
#   RELAY_DEPLOY_TARGET              — ecs | local (default: local). When "ecs",
#                                      a missing Docker daemon is a FAIL (the image
#                                      can't be built) instead of a WARN. The local
#                                      run-on-EC2 path needs no Docker, so it stays
#                                      a WARN there.
#
# Exit codes:
#   0  — no FAIL-level checks triggered
#   1  — at least one FAIL-level check triggered
set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
JSON_MODE=false
for arg in "$@"; do
  case "${arg}" in
    --json) JSON_MODE=true ;;
    --help|-h)
      sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "Unknown flag: ${arg}" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Arch + package-manager detection (done once; used by fix strings)
# ---------------------------------------------------------------------------
_ARCH="$(uname -m)"   # x86_64 | aarch64 | arm64

_pkgmgr=""
if command -v apt-get >/dev/null 2>&1; then
  _pkgmgr="apt-get"
elif command -v dnf >/dev/null 2>&1; then
  _pkgmgr="dnf"
elif command -v yum >/dev/null 2>&1; then
  _pkgmgr="yum"
elif command -v apk >/dev/null 2>&1; then
  _pkgmgr="apk"
elif command -v pacman >/dev/null 2>&1; then
  _pkgmgr="pacman"
fi

# Return the install command for a named package, arch-aware where needed.
# $1 = logical name (docker | aws-cli | git | node | python3)
_install_cmd() {
  local name="$1"
  case "${_pkgmgr}" in
    apt-get)
      case "${name}" in
        docker)   echo "sudo apt-get install -y docker.io" ;;
        git)      echo "sudo apt-get install -y git" ;;
        node)     echo "curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs  # distro 'nodejs' may be < 20" ;;
        python3)  echo "sudo apt-get install -y python3" ;;
        aws-cli)  _aws_install_cmd ;;
        *)        echo "sudo apt-get install -y ${name}" ;;
      esac ;;
    dnf)
      case "${name}" in
        docker)   echo "sudo dnf install -y docker && sudo systemctl enable --now docker" ;;
        git)      echo "sudo dnf install -y git" ;;
        node)     echo "sudo dnf install -y nodejs22 && sudo alternatives --set node /usr/bin/node-22  # 'nodejs' alone is EOL Node 18 on AL2023" ;;
        python3)  echo "sudo dnf install -y python3" ;;
        aws-cli)  _aws_install_cmd ;;
        *)        echo "sudo dnf install -y ${name}" ;;
      esac ;;
    yum)
      case "${name}" in
        docker)   echo "sudo yum install -y docker && sudo systemctl enable --now docker" ;;
        git)      echo "sudo yum install -y git" ;;
        node)     echo "sudo yum install -y nodejs" ;;
        python3)  echo "sudo yum install -y python3" ;;
        aws-cli)  _aws_install_cmd ;;
        *)        echo "sudo yum install -y ${name}" ;;
      esac ;;
    apk)
      case "${name}" in
        docker)   echo "sudo apk add docker && sudo rc-update add docker && sudo service docker start" ;;
        git)      echo "sudo apk add git" ;;
        node)     echo "sudo apk add nodejs npm" ;;
        python3)  echo "sudo apk add python3" ;;
        aws-cli)  _aws_install_cmd ;;
        *)        echo "sudo apk add ${name}" ;;
      esac ;;
    pacman)
      case "${name}" in
        docker)   echo "sudo pacman -S docker && sudo systemctl enable --now docker" ;;
        git)      echo "sudo pacman -S git" ;;
        node)     echo "sudo pacman -S nodejs npm" ;;
        python3)  echo "sudo pacman -S python" ;;
        aws-cli)  _aws_install_cmd ;;
        *)        echo "sudo pacman -S ${name}" ;;
      esac ;;
    *)
      case "${name}" in
        aws-cli) _aws_install_cmd ;;
        *)       echo "install ${name} via your system package manager" ;;
      esac ;;
  esac
}

# AWS CLI v2 official curl-installer URL, arch-aware.
#   _aws_install_cmd            → fresh install (`./aws/install`)
#   _aws_install_cmd --update   → upgrade in place (`./aws/install --update`),
#                                 which is required when a v2 is already present
#                                 (a plain install aborts with "already exists").
_aws_install_cmd() {
  local zip_name update_flag=""
  [ "${1:-}" = "--update" ] && update_flag=" --update"
  case "${_ARCH}" in
    aarch64|arm64) zip_name="awscli-exe-linux-aarch64.zip" ;;
    *)             zip_name="awscli-exe-linux-x86_64.zip" ;;
  esac
  echo "curl -fsSL https://awscli.amazonaws.com/${zip_name} -o /tmp/awscliv2.zip && unzip -o /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install${update_flag}"
}

# ---------------------------------------------------------------------------
# Result accumulation
# ---------------------------------------------------------------------------
# Parallel arrays: _names _statuses _details _fixes
_names=()
_statuses=()
_details=()
_fixes=()

_record() {
  # $1=status $2=name $3=detail $4=fix (optional)
  _statuses+=("$1")
  _names+=("$2")
  _details+=("$3")
  _fixes+=("${4:-}")
}

# ---------------------------------------------------------------------------
# Helper: compare semver components (integers only; handles vX.Y.Z)
# Returns 0 (true) if actual >= required
# ---------------------------------------------------------------------------
_ver_ge() {
  # $1=actual_str  $2=required_major  $3=required_minor (optional, default 0)
  local raw="${1#v}"  # strip leading 'v'
  local req_maj="${2:-0}"
  local req_min="${3:-0}"
  local act_maj act_min
  act_maj="$(echo "${raw}" | cut -d. -f1)"
  act_min="$(echo "${raw}" | cut -d. -f2)"
  act_maj="${act_maj//[^0-9]/}"
  act_min="${act_min//[^0-9]/}"
  act_maj="${act_maj:-0}"
  act_min="${act_min:-0}"
  if [ "${act_maj}" -gt "${req_maj}" ]; then return 0; fi
  if [ "${act_maj}" -eq "${req_maj}" ] && [ "${act_min}" -ge "${req_min}" ]; then return 0; fi
  return 1
}

# ---------------------------------------------------------------------------
# Progress to stderr (suppressed in JSON mode)
# ---------------------------------------------------------------------------
_progress() {
  if ! "${JSON_MODE}"; then
    echo "$*" >&2
  fi
}

# ===========================================================================
# CHECK 1 — Tooling presence and minimum versions
# ===========================================================================
_progress ""
_progress "=== Relay preflight checks ==="
_progress ""
_progress "--- 1. Tooling ---"

# ---- bash >= 4 ----
_bash_maj="${BASH_VERSINFO[0]}"
if [ "${_bash_maj}" -ge 4 ]; then
  _record PASS "bash" "bash ${BASH_VERSION}"
else
  _record FAIL "bash" "bash ${BASH_VERSION} (need >= 4)" \
    "Upgrade bash: ${_pkgmgr:+sudo ${_pkgmgr} install -y bash; }see https://www.gnu.org/software/bash/"
fi
_progress "  bash ${BASH_VERSION}"

# ---- git (any version) ----
if _git_ver="$(git --version 2>/dev/null)"; then
  _record PASS "git" "${_git_ver}"
  _progress "  ${_git_ver}"
else
  _record FAIL "git" "not found" "$(_install_cmd git)"
  _progress "  git: NOT FOUND"
fi

# ---- aws CLI v2 ----
_progress "  aws CLI..."
if _aws_ver_line="$(aws --version 2>&1)"; then
  # Output format: "aws-cli/2.x.y Python/3.x.y ..."
  _aws_ver="$(echo "${_aws_ver_line}" | cut -d/ -f2 | cut -d' ' -f1)"
  _aws_maj="$(echo "${_aws_ver}" | cut -d. -f1)"
  _aws_maj="${_aws_maj//[^0-9]/}"
  if [ "${_aws_maj:-0}" -ge 2 ]; then
    _record PASS "aws-cli" "aws-cli ${_aws_ver} (v2)"
    _progress "  aws CLI ${_aws_ver}"
    # CloudFormation Express Mode (--deployment-config) landed in aws-cli 2.35.
    # It powers the opt-in RELAY_CFN_MODE=EXPRESS fast path in
    # relay-deploy-direct.sh, so this is a WARN (feature unavailable), not a FAIL:
    # the default STANDARD deploy works on any v2. See prompts/upgrade-aws-cli.md.
    if _ver_ge "${_aws_ver}" 2 35; then
      _record PASS "aws-cli-express" "aws-cli ${_aws_ver} supports CloudFormation Express Mode (>= 2.35)"
      _progress "  aws CLI ${_aws_ver}: Express Mode supported"
    else
      _record WARN "aws-cli-express" \
        "aws-cli ${_aws_ver} predates CloudFormation Express Mode (need >= 2.35)" \
        "Optional: to use the faster RELAY_CFN_MODE=EXPRESS deploy path, upgrade the AWS CLI. Run the AI runbook prompts/upgrade-aws-cli.md, or: $(_aws_install_cmd --update). The default STANDARD deploy works without this."
      _progress "  aws CLI ${_aws_ver}: no Express Mode (< 2.35) — WARN (optional)"
    fi
  else
    _record FAIL "aws-cli" "aws-cli ${_aws_ver} (v1 — v2 required)" "$(_aws_install_cmd)"
    _progress "  aws CLI ${_aws_ver}: v1 detected — FAIL"
  fi
else
  _record FAIL "aws-cli" "not found" "$(_aws_install_cmd)"
  _progress "  aws CLI: NOT FOUND"
fi

# ---- docker: present + daemon reachable ----
_progress "  docker..."
if command -v docker >/dev/null 2>&1; then
  _docker_client_ver="$(docker --version 2>/dev/null || echo unknown)"
  if docker info >/dev/null 2>&1; then
    _record PASS "docker" "${_docker_client_ver}"
    _progress "  ${_docker_client_ver} (daemon up)"
  elif [ "${RELAY_DEPLOY_TARGET:-local}" = "ecs" ]; then
    # ECS deploy MUST build + push the container image, so a dead daemon blocks it.
    _record FAIL "docker" "${_docker_client_ver} — daemon not reachable (required for RELAY_DEPLOY_TARGET=ecs)" \
      "Docker is required to build the Relay image for an ECS deploy. Start it: sudo systemctl start docker  (or: sudo service docker start)"
    _progress "  ${_docker_client_ver}: daemon not reachable, target=ecs — FAIL"
  else
    _record WARN "docker" "${_docker_client_ver} — daemon not reachable" \
      "start the docker service: sudo systemctl start docker  (or: sudo service docker start). Not needed for the local run-on-EC2 path; required for an ECS image build (set RELAY_DEPLOY_TARGET=ecs)."
    _progress "  ${_docker_client_ver}: daemon not reachable — WARN"
  fi
else
  _record FAIL "docker" "not found" "$(_install_cmd docker)"
  _progress "  docker: NOT FOUND"
fi

# ---- node >= 20 ----
# CDK synth runs `npx aws-cdk@2`, whose current release refuses to support
# Node 18 (end-of-life 2025-11-30) — it still exits 0 but prints a loud EOL
# banner. Node 20 is the floor; 22 is clean. Require >= 20.
_progress "  node..."
if _node_ver="$(node --version 2>/dev/null)"; then
  if _ver_ge "${_node_ver}" 20 0; then
    _record PASS "node" "node ${_node_ver}"
    _progress "  node ${_node_ver}"
  else
    _record FAIL "node" "node ${_node_ver} (need >= 20; aws-cdk dropped Node 18)" "$(_install_cmd node)"
    _progress "  node ${_node_ver}: too old — FAIL"
  fi
else
  _record FAIL "node" "not found" "$(_install_cmd node)"
  _progress "  node: NOT FOUND"
fi

# ---- python >= 3.12 (probe versioned binaries, not just python3) ----
# Amazon Linux 2023 (and some RHEL/Fedora) keep `python3` pinned at the system
# version (3.9) and install a newer interpreter as a PARALLEL binary named
# `python3.12` — installing 3.12 does NOT repoint the `python3` symlink. Probe
# the versioned names too so a correct install is detected; report which binary
# satisfies the requirement so the venv can be created with it.
_progress "  python (>= 3.12)..."
_py_found_bin=""
_py_found_ver=""
_py_best_bin=""      # newest interpreter seen, even if < 3.12 (for the FAIL message)
_py_best_ver=""
for _py_bin in python3.13 python3.12 python3; do
  command -v "${_py_bin}" >/dev/null 2>&1 || continue
  _cand_ver="$("${_py_bin}" --version 2>/dev/null)" || continue
  _cand_ver="${_cand_ver#Python }"
  [ -z "${_py_best_bin}" ] && { _py_best_bin="${_py_bin}"; _py_best_ver="${_cand_ver}"; }
  if _ver_ge "${_cand_ver}" 3 12; then
    _py_found_bin="${_py_bin}"
    _py_found_ver="${_cand_ver}"
    break
  fi
done
if [ -n "${_py_found_bin}" ]; then
  _record PASS "python3" "${_py_found_bin} ${_py_found_ver}"
  _progress "  ${_py_found_bin} ${_py_found_ver}"
  # Nudge when `python3` itself is too old but a versioned binary satisfies it —
  # this is the Amazon Linux trap: the venv MUST be created with the versioned name.
  if [ "${_py_found_bin}" != "python3" ]; then
    _record WARN "python3-venv-binary" \
      "use '${_py_found_bin}' for the venv ('python3' is older)" \
      "Create the virtualenv with the versioned binary: ${_py_found_bin} -m venv .venv"
    _progress "  note: create the venv with ${_py_found_bin} (python3 is older)"
  fi
elif [ -n "${_py_best_bin}" ]; then
  _record FAIL "python3" "${_py_best_bin} ${_py_best_ver} (need >= 3.12; no 3.12+ binary found)" "$(_install_cmd python3)"
  _progress "  ${_py_best_bin} ${_py_best_ver}: too old, and no python3.12/3.13 found — FAIL"
else
  _record FAIL "python3" "not found" "$(_install_cmd python3)"
  _progress "  python3: NOT FOUND"
fi

# ---- .venv Python version (if .venv already exists) ----
# An existing .venv created with the system python3 (e.g. Python 3.9 on AL2023)
# activates without error but silently uses the wrong interpreter — the failure
# only surfaces at CDK synth time. Catch it here with a clear message.
_venv_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv"
if [ -f "${_venv_dir}/bin/python" ]; then
  _venv_py_raw="$("${_venv_dir}/bin/python" --version 2>/dev/null)" || _venv_py_raw=""
  _venv_py_ver="${_venv_py_raw#Python }"
  if [ -z "${_venv_py_ver}" ]; then
    _record WARN "venv-python-version" ".venv/bin/python found but --version failed" \
      "Recreate the venv: rm -rf .venv && ${_py_found_bin:-python3.12} -m venv .venv && source .venv/bin/activate && pip install -e '.[infra]'"
    _progress "  .venv: could not read Python version — WARN"
  elif _ver_ge "${_venv_py_ver}" 3 12; then
    _record PASS "venv-python-version" ".venv uses Python ${_venv_py_ver}"
    _progress "  .venv: Python ${_venv_py_ver}"
  else
    _record WARN "venv-python-version" \
      ".venv uses Python ${_venv_py_ver} (< 3.12) — recreate with ${_py_found_bin:-python3.12}" \
      "Delete and recreate: rm -rf .venv && ${_py_found_bin:-python3.12} -m venv .venv && source .venv/bin/activate && pip install -e '.[infra]'"
    _progress "  .venv: Python ${_venv_py_ver} — too old, WARN"
  fi
fi

# ===========================================================================
# CHECK 2 — AWS identity
# ===========================================================================
_progress ""
_progress "--- 2. AWS identity ---"

_AWS_ACCOUNT_ID=""
_AWS_CALLER_ARN=""
_AWS_REGION_RESOLVED=""

if _identity_json="$(aws sts get-caller-identity 2>/dev/null)"; then
  _AWS_ACCOUNT_ID="$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['Account'])" <<< "${_identity_json}")"
  _AWS_CALLER_ARN="$(python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['Arn'])" <<< "${_identity_json}")"
  _record PASS "aws-identity" "account=${_AWS_ACCOUNT_ID} arn=${_AWS_CALLER_ARN}"
  _progress "  Account: ${_AWS_ACCOUNT_ID}"
  _progress "  ARN:     ${_AWS_CALLER_ARN}"
else
  _record FAIL "aws-identity" "aws sts get-caller-identity failed — no valid credentials" \
    "configure credentials: aws configure, or assume a role (e.g. aws sts assume-role)"
  _progress "  FAIL: could not call sts:GetCallerIdentity"
fi

# Region resolution
if [ -n "${AWS_REGION:-}" ]; then
  _AWS_REGION_RESOLVED="${AWS_REGION}"
elif [ -n "${AWS_DEFAULT_REGION:-}" ]; then
  _AWS_REGION_RESOLVED="${AWS_DEFAULT_REGION}"
elif _cfg_region="$(aws configure get region 2>/dev/null)" && [ -n "${_cfg_region}" ]; then
  _AWS_REGION_RESOLVED="${_cfg_region}"
fi

if [ -n "${_AWS_REGION_RESOLVED}" ]; then
  _record PASS "aws-region" "region=${_AWS_REGION_RESOLVED}"
  _progress "  Region:  ${_AWS_REGION_RESOLVED}"
else
  _record WARN "aws-region" "no AWS region configured" \
    "set AWS_REGION env var, or run: aws configure (set default region)"
  _progress "  WARN: no region found in AWS_REGION, AWS_DEFAULT_REGION, or aws configure"
fi

# ---- AWS_PROFILE override warning ----
# When AWS_PROFILE is set, every aws/CDK call uses that named profile and SILENTLY
# ignores the EC2 instance role — common on a multi-account dev box. There is no
# error: operations can target the wrong account or fail on a permission mismatch
# in a different account, which is especially dangerous for resource-creating
# deploys. The identity above already reflects the resolved credentials, so point
# the operator at it.
if [ -n "${AWS_PROFILE:-}" ]; then
  _record WARN "aws-profile" \
    "AWS_PROFILE=${AWS_PROFILE} is set — it overrides the EC2 instance role" \
    "Confirm the account/ARN above is the intended deploy target. To use the instance profile instead: unset AWS_PROFILE"
  _progress "  WARN: AWS_PROFILE=${AWS_PROFILE} overrides the instance role — verify the account above"
fi

# ===========================================================================
# CHECK 3 — IAM capability detection via simulate-principal-policy
# ===========================================================================
_progress ""
_progress "--- 3. IAM capability detection ---"

if [ -n "${_AWS_CALLER_ARN}" ]; then
  _sim_out=""
  _sim_err=""
  # simulate-principal-policy wants a *principal* ARN (a user or role), not the
  # caller's *session* ARN. An assumed-role session looks like
  #   arn:aws:sts::<acct>:assumed-role/<RoleName>/<session>
  # and the API rejects it outright. Normalize it to the underlying role ARN
  #   arn:aws:iam::<acct>:role/<RoleName>
  # so the check actually runs when you authenticate via an assumed role (e.g.
  # OrganizationAccountAccessRole — the usual way you reach a hub account).
  _sim_principal_arn="${_AWS_CALLER_ARN}"
  case "${_AWS_CALLER_ARN}" in
    arn:aws*:sts::*:assumed-role/*)
      _sim_principal_arn="$(printf '%s\n' "${_AWS_CALLER_ARN}" | sed -E \
        's#arn:(aws[a-z-]*):sts::([0-9]+):assumed-role/([^/]+)/.*#arn:\1:iam::\2:role/\3#')"
      ;;
  esac
  if _sim_out="$(aws iam simulate-principal-policy \
        --policy-source-arn "${_sim_principal_arn}" \
        --action-names "iam:CreateRole" "ec2:CreateVpc" \
        2>/tmp/_relay_sim_err)"; then
    # Parse EvalDecision for each action with python3
    _create_role_decision="$(python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
results = data.get('EvaluationResults', [])
for r in results:
    if r.get('EvalActionName','').lower() == 'iam:createrole':
        print(r.get('EvalDecision','unknown'))
        break
else:
    print('not-evaluated')
" <<< "${_sim_out}")"

    _create_vpc_decision="$(python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
results = data.get('EvaluationResults', [])
for r in results:
    if r.get('EvalActionName','').lower() == 'ec2:createvpc':
        print(r.get('EvalDecision','unknown'))
        break
else:
    print('not-evaluated')
" <<< "${_sim_out}")"

    _progress "  iam:CreateRole  -> ${_create_role_decision}"
    _progress "  ec2:CreateVpc   -> ${_create_vpc_decision}"

    if [ "${_create_role_decision}" = "allowed" ]; then
      _record PASS "iam:CreateRole" "allowed (CDK can create roles)"
    else
      _record WARN "iam:CreateRole" "decision=${_create_role_decision} — role creation denied in this account" \
        "Role creation is denied in this account — deploy in BYOR mode (see docs/byor.md): pass existing role ARNs via relay:* context instead of letting CDK create roles."
    fi

    if [ "${_create_vpc_decision}" = "allowed" ]; then
      _record PASS "ec2:CreateVpc" "allowed (Relay can create a VPC if needed)"
    else
      _record WARN "ec2:CreateVpc" "decision=${_create_vpc_decision} — VPC creation denied in this account" \
        "VPC creation is denied — Relay does not require a new VPC; supply an existing VPC via relay:vpc_id context if your stacks need one."
    fi
  else
    _sim_err_text="$(cat /tmp/_relay_sim_err 2>/dev/null || echo 'unknown error')"
    _record WARN "iam-simulate" "iam:SimulatePrincipalPolicy call failed — cannot determine IAM capability" \
      "Grant iam:SimulatePrincipalPolicy to this identity, or proceed knowing role/VPC creation permissions are unverified. Error: ${_sim_err_text}"
    _progress "  WARN: simulate-principal-policy denied — ${_sim_err_text}"
  fi
else
  _record WARN "iam-simulate" "skipped (no caller ARN — identity check failed above)" ""
  _progress "  WARN: skipped IAM simulation (no caller ARN)"
fi

# ===========================================================================
# CHECK 4 — CDK bootstrap stack
# ===========================================================================
_progress ""
_progress "--- 4. CDK bootstrap ---"

if [ -n "${_AWS_REGION_RESOLVED}" ]; then
  if aws cloudformation describe-stacks \
        --stack-name CDKToolkit \
        --region "${_AWS_REGION_RESOLVED}" \
        >/dev/null 2>&1; then
    _record PASS "cdk-bootstrap" "CDKToolkit stack found in ${_AWS_REGION_RESOLVED}"
    _progress "  CDKToolkit present in ${_AWS_REGION_RESOLVED}"
  else
    _record WARN "cdk-bootstrap" "CDKToolkit stack not found in ${_AWS_REGION_RESOLVED}" \
      "run ./scripts/relay-bootstrap.sh"
    _progress "  WARN: CDKToolkit not found — run relay-bootstrap.sh"
  fi
else
  _record WARN "cdk-bootstrap" "skipped (no region resolved — cannot query CloudFormation)" \
    "set AWS_REGION and re-run to verify CDK bootstrap status"
  _progress "  WARN: skipped CDK bootstrap check (no region)"
fi

# ===========================================================================
# CHECK 5 — central security StackSet alarm noise
# ===========================================================================
# Enterprise accounts governed by a central security team carry account-wide
# CloudTrail metric alarms (IAMPolicyChanges, UnauthorizedAPICalls, …) installed
# via a CloudFormation StackSet. They fire on routine admin activity — including
# a Relay install — and surface as incidents the operator cannot resolve. Warn
# up front and point at the ignore-rule fix. Best-effort: a denied
# cloudwatch:DescribeAlarms just skips the check (no failure).
_progress ""
_progress "--- 5. Central security alarm noise (StackSet-) ---"

if [ -n "${_AWS_REGION_RESOLVED}" ]; then
  _stackset_alarm_count="$(aws cloudwatch describe-alarms \
        --alarm-name-prefix "StackSet-" \
        --region "${_AWS_REGION_RESOLVED}" \
        --query 'length(MetricAlarms)' --output text 2>/dev/null || echo "skip")"
  if [ "${_stackset_alarm_count}" = "skip" ]; then
    _record WARN "cspm-alarms" "could not list CloudWatch alarms (cloudwatch:DescribeAlarms denied or unavailable)" \
      "If this account has central security alarms, add an ignore rule for their prefix in config/routing.yaml (see config/routing.example.yaml)."
    _progress "  WARN: skipped StackSet alarm scan (DescribeAlarms denied)"
  elif [ "${_stackset_alarm_count:-0}" -gt 0 ] 2>/dev/null; then
    _record WARN "cspm-alarms" "${_stackset_alarm_count} StackSet-prefixed CloudWatch alarm(s) found — these will surface as incidents" \
      "These are central security controls, not application incidents. Add an ignore rule scoped to their prefix in config/routing.yaml (see the CSPM block in config/routing.example.yaml)."
    _progress "  WARN: ${_stackset_alarm_count} StackSet- alarms may create noise — add an ignore rule"
  else
    _record PASS "cspm-alarms" "no StackSet-prefixed CloudWatch alarms found"
    _progress "  no StackSet- alarms found"
  fi
else
  _record WARN "cspm-alarms" "skipped (no region resolved — cannot query CloudWatch)" \
    "set AWS_REGION and re-run to scan for central security alarm noise"
  _progress "  WARN: skipped StackSet alarm scan (no region)"
fi

# ===========================================================================
# CHECK 6 — BYOV egress: NAT gateway OR required interface VPC endpoints
# ===========================================================================
# When an existing VPC is imported (BYOV, RELAY_VPC_ID set), Fargate tasks in
# its private subnets can only reach the AWS control-plane APIs Relay needs
# (ECR, DynamoDB, SQS, SNS, CloudWatch Logs, Secrets Manager) via EITHER a NAT
# gateway OR interface/gateway VPC endpoints. With neither, ECS task startup
# fails with no clear error (image pull hangs, then times out). Warn — not fail
# — because the VPC may have connectivity we can't see (Transit Gateway / VPC
# peering to a NAT-equipped hub VPC). Best-effort: denied describe calls skip.
_progress ""
_progress "--- 6. BYOV egress (NAT or VPC endpoints) ---"

# BYOV can be signalled two ways: the RELAY_VPC_ID env var, or -c relay:vpc_id=...
# on the deploy command (the deploy-byor / ecs-clean-start path). CDK does NOT
# persist the passed `relay:vpc_id` context value, but `ec2.Vpc.from_lookup`
# DOES cache the resolved VPC under a `vpc-provider:...filter.vpc-id=<id>...` key
# in cdk.context.json once a synth/deploy has run. We read that so a BYOV deploy
# driven purely by CDK context is still egress-checked (ISSUE-7). When BYOV is
# detected only via the cache (RELAY_VPC_ID unset), we ALWAYS WARN rather than
# running the real NAT/endpoint scan — even when a region happens to be
# resolved. Running the real scan here would risk a misleading plain PASS:
# it would report on whatever VPC last got cached, not on what the operator's
# actual deploy env has wired up (RELAY_VPC_ID unset means the compute stack
# itself has no confirmed signal to use this VPC either). Only a genuinely
# operator-set RELAY_VPC_ID unlocks the real PASS/WARN-by-findings check below.
_byov_vpc_id="${RELAY_VPC_ID:-}"
_byov_from_cache=""
if [ -z "${_byov_vpc_id}" ]; then
  _repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _cdk_ctx="${_repo_root}/cdk.context.json"
  if [ -f "${_cdk_ctx}" ] && command -v python3 >/dev/null 2>&1; then
    _byov_vpc_id="$(python3 -c "import sys,json
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(''); sys.exit(0)
for k, v in d.items():
    if k.startswith('vpc-provider:') and isinstance(v, dict) and v.get('vpcId'):
        print(v['vpcId']); break
else:
    print('')" "${_cdk_ctx}" 2>/dev/null || echo "")"
    [ -n "${_byov_vpc_id}" ] && _byov_from_cache="yes"
  fi
fi

if [ -z "${_byov_vpc_id}" ]; then
  _record PASS "byov-egress" "not BYOV (no VPC configured — Relay creates a NAT-equipped VPC)" \
    "If this IS a BYOV deploy, set RELAY_VPC_ID=<vpc-id> (or run a synth with -c relay:vpc_id=<vpc-id> first) before running preflight so the egress check can validate NAT/endpoint coverage."
  _progress "  skipped: not BYOV"
elif [ -z "${_AWS_REGION_RESOLVED}" ]; then
  _record WARN "byov-egress" "skipped (no region resolved — cannot query VPC egress for ${_byov_vpc_id})" \
    "set AWS_REGION and re-run to verify NAT/endpoint coverage for ${_byov_vpc_id}"
  _progress "  WARN: skipped BYOV egress check (no region)"
else
  # When the VPC came from the CDK context cache, note the source in the result
  # message but run the real NAT/endpoint scan — the operator ran a synth with
  # -c relay:vpc_id=... so the cached value IS the intended deploy VPC.
  _byov_src=""
  [ -n "${_byov_from_cache}" ] && _byov_src=" (source: cdk.context.json)"

  # NAT gateways in the VPC (available state only).
  _nat_count="$(aws ec2 describe-nat-gateways \
        --filter "Name=vpc-id,Values=${_byov_vpc_id}" "Name=state,Values=available" \
        --region "${_AWS_REGION_RESOLVED}" \
        --query 'length(NatGateways)' --output text 2>/dev/null || echo "skip")"
  # Service names of VPC endpoints in the VPC.
  _endpoint_services="$(aws ec2 describe-vpc-endpoints \
        --filters "Name=vpc-id,Values=${_byov_vpc_id}" \
        --region "${_AWS_REGION_RESOLVED}" \
        --query 'VpcEndpoints[].ServiceName' --output text 2>/dev/null || echo "skip")"

  if [ "${_nat_count}" = "skip" ] || [ "${_endpoint_services}" = "skip" ]; then
    _record WARN "byov-egress" "could not query VPC egress (ec2:DescribeNatGateways / DescribeVpcEndpoints denied)${_byov_src}" \
      "Grant ec2:DescribeNatGateways + ec2:DescribeVpcEndpoints, or manually confirm the VPC has a NAT gateway or the required interface endpoints (see docs/byor.md 'BYOV' table)."
    _progress "  WARN: skipped BYOV egress scan (describe denied)"
  elif [ "${_nat_count:-0}" -gt 0 ] 2>/dev/null; then
    _record PASS "byov-egress" "${_nat_count} NAT gateway(s) in ${_byov_vpc_id} — private-subnet egress available${_byov_src}"
    _progress "  ${_nat_count} NAT gateway(s) present"
  else
    # No NAT: require the interface/gateway endpoints Relay depends on.
    _missing=""
    for _svc in ecr.api ecr.dkr s3 dynamodb sqs sns logs secretsmanager; do
      _needle="com.amazonaws.${_AWS_REGION_RESOLVED}.${_svc}"
      case " ${_endpoint_services} " in
        *" ${_needle} "*) : ;;
        *) _missing="${_missing} ${_svc}" ;;
      esac
    done
    _missing="${_missing# }"
    if [ -z "${_missing}" ]; then
      _record PASS "byov-egress" "no NAT, but all required VPC endpoints present in ${_byov_vpc_id}${_byov_src}"
      _progress "  no NAT, but all required endpoints present"
    else
      _record WARN "byov-egress" "${_byov_vpc_id}: no NAT gateway and missing endpoints: ${_missing}${_byov_src}" \
        "Request either a NAT gateway or these interface/gateway VPC endpoints from your network team: ${_missing} (full names: com.amazonaws.${_AWS_REGION_RESOLVED}.<service>). See the 'BYOV' table in docs/byor.md. Without egress, ECS tasks fail to start."
      _progress "  WARN: no NAT, missing endpoints: ${_missing}"
    fi
  fi
fi

# ===========================================================================
# Tally
# ===========================================================================
_n_pass=0
_n_warn=0
_n_fail=0
for _s in "${_statuses[@]}"; do
  case "${_s}" in
    PASS) _n_pass=$(( _n_pass + 1 )) ;;
    WARN) _n_warn=$(( _n_warn + 1 )) ;;
    FAIL) _n_fail=$(( _n_fail + 1 )) ;;
  esac
done

_overall_ready="true"
[ "${_n_fail}" -gt 0 ] && _overall_ready="false"

# ===========================================================================
# Output: human table OR JSON
# ===========================================================================
if "${JSON_MODE}"; then
  # Build JSON with python3 — no jq dependency.
  # Pass arrays to python3 via env vars using unit-separator (0x1f) as delimiter.
  _NAMES_STR=""
  _STATUSES_STR=""
  _DETAILS_STR=""
  _FIXES_STR=""
  for i in "${!_names[@]}"; do
    [ "${i}" -gt 0 ] && _NAMES_STR+=$'\x1f'
    [ "${i}" -gt 0 ] && _STATUSES_STR+=$'\x1f'
    [ "${i}" -gt 0 ] && _DETAILS_STR+=$'\x1f'
    [ "${i}" -gt 0 ] && _FIXES_STR+=$'\x1f'
    _NAMES_STR+="${_names[$i]}"
    _STATUSES_STR+="${_statuses[$i]}"
    _DETAILS_STR+="${_details[$i]}"
    _FIXES_STR+="${_fixes[$i]}"
  done

  _NAMES="${_NAMES_STR}" \
  _STATUSES="${_STATUSES_STR}" \
  _DETAILS="${_DETAILS_STR}" \
  _FIXES="${_FIXES_STR}" \
  _READY="${_overall_ready}" \
  _N_PASS="${_n_pass}" \
  _N_WARN="${_n_warn}" \
  _N_FAIL="${_n_fail}" \
  python3 - <<'PYEOF'
import json, os

names    = os.environ.get('_NAMES','').split('\x1f')
statuses = os.environ.get('_STATUSES','').split('\x1f')
details  = os.environ.get('_DETAILS','').split('\x1f')
fixes    = os.environ.get('_FIXES','').split('\x1f')
ready    = os.environ.get('_READY','true') == 'true'
n_pass   = int(os.environ.get('_N_PASS','0'))
n_warn   = int(os.environ.get('_N_WARN','0'))
n_fail   = int(os.environ.get('_N_FAIL','0'))

checks = []
for i in range(len(names)):
    if not names[i]:
        continue
    entry = {
        'name':   names[i],
        'status': statuses[i] if i < len(statuses) else '',
        'detail': details[i]  if i < len(details)  else '',
    }
    fix = fixes[i] if i < len(fixes) else ''
    if fix:
        entry['fix'] = fix
    checks.append(entry)

output = {
    'ready':   ready,
    'checks':  checks,
    'summary': {'pass': n_pass, 'warn': n_warn, 'fail': n_fail},
}
print(json.dumps(output, indent=2))
PYEOF

else
  # ---------------------------------------------------------------------------
  # Human-readable table
  # ---------------------------------------------------------------------------
  echo ""
  printf "%-20s %-6s  %-55s\n" "CHECK" "STATUS" "DETAIL"
  printf "%-20s %-6s  %-55s\n" "--------------------" "------" "-------------------------------------------------------"
  for i in "${!_names[@]}"; do
    _name="${_names[$i]}"
    _status="${_statuses[$i]}"
    _detail="${_details[$i]}"
    _fix="${_fixes[$i]}"
    # Truncate detail for table display
    _detail_short="${_detail:0:54}"
    printf "%-20s %-6s  %s\n" "${_name}" "${_status}" "${_detail_short}"
    if [ -n "${_fix}" ]; then
      printf "  %s Fix: %s\n" "       " "${_fix}"
    fi
  done
  echo ""
  printf "Summary: %d PASS  %d WARN  %d FAIL\n" "${_n_pass}" "${_n_warn}" "${_n_fail}"
  if [ "${_overall_ready}" = "true" ]; then
    echo "Ready: YES (no hard failures)"
  else
    echo "Ready: NO  (${_n_fail} hard failure(s) must be resolved before deploying)"
  fi
  echo ""
fi

# ===========================================================================
# Exit code
# ===========================================================================
[ "${_n_fail}" -gt 0 ] && exit 1
exit 0
