#!/usr/bin/env bash
# relay-context.sh — shared helpers for Relay deploy scripts.
#
# Sourced by relay-synth.sh / relay-deploy.sh / relay-bootstrap.sh. Holds the
# logic that is identical whether run locally (Westport, GitHub) or by a GitLab
# runner at the client: account discovery and CDK context assembly.
#
# Inputs (env vars; the pipeline sets these from its inputs form, a human sets
# them in their shell):
#   RELAY_DEPLOY_TYPE   team | federated-hub           (default: team)
#                       (deprecated aliases: standalone/node → team, hub → federated-hub)
#   AWS_REGION          target region                  (default: us-east-1)
#   RELAY_TEAM_NAME     team id (team topology)
#   RELAY_ORG_ID        AWS org id (federated-hub only)
#   RELAY_SERVICENOW_INSTANCE servicenow host (optional)
#   RELAY_HUB_SCOPE     local | local-federated        (team)
#   RELAY_UPSTREAM_HUB_BUS_ARN  upstream federated-hub bus (local-federated)
#                       (deprecated alias: RELAY_CENTRAL_HUB_BUS_ARN)
#   RELAY_GITLAB_REPO   config repo path (optional)
#   RELAY_GITLAB_SECRET_NAME   secrets manager name     (default: relay/gitlab-token)
#   RELAY_INTERNAL_ALB  true (default) internal ALB | false public/internet-facing.
#                       Unset => stack default (internal). Set "false" only when
#                       the account has no VPN/peering into the VPC and the ALB
#                       must be reachable from the internet.
#   RELAY_PHZ_ID        Route53 private hosted zone ID (deployment.private_hosted_zone_id)
#   RELAY_PHZ_NAME      private hosted zone name, e.g. corp.example.internal
#   RELAY_ALB_SUBDOMAIN ALB subdomain (default: relay)
#   RELAY_CERT_ARN      explicit ACM certificate ARN (overrides PHZ-derived)
#   (RELAY_INTERNAL_ALB already documented above)
#   RELAY_ACCESS_CONTROL  true | false — fine-grained write access control
#   RELAY_AUTH_ALLOWED_USERS  comma-separated list of OIDC usernames allowed to WRITE
#
# Build-time-only env vars (NOT passed as CDK context flags):
#   RELAY_CONFIG_DIR    path to a team's live local config directory
#                       (default: ~/.relay/config; any path outside the clone).
#                       Config lives here so `git pull` never clobbers it.
#                       Consumed by scripts/relay-build-hub-image.sh at image-
#                       build time — the external *.yaml files are baked into
#                       the image in place of the in-repo config/ defaults.
#                       The image-internal path is always /app/config; this var
#                       is a host-side source selector only and has no CDK flag.
set -euo pipefail

RELAY_DEPLOY_TYPE="${RELAY_DEPLOY_TYPE:-team}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
RELAY_GITLAB_SECRET_NAME="${RELAY_GITLAB_SECRET_NAME:-relay/gitlab-token}"

# Resolve repo root regardless of where the script is invoked from.
RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RELAY_ROOT

# cdk wrapper — uses a local cdk if present, else npx (Node already required).
relay_cdk() {
  if command -v cdk >/dev/null 2>&1; then
    cdk "$@"
  else
    npx --yes aws-cdk@2 "$@"
  fi
}

# Discover the account id from the caller's own identity (works for an
# in-account GitLab runner's instance role AND for a local user/assumed role).
relay_resolve_account() {
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  export AWS_ACCOUNT_ID CDK_DEFAULT_ACCOUNT="${AWS_ACCOUNT_ID}" CDK_DEFAULT_REGION="${AWS_REGION}"
  echo "Account ${AWS_ACCOUNT_ID} / region ${AWS_REGION} / role ${RELAY_DEPLOY_TYPE}" >&2
}

# Read environments.yaml (if present) and populate _RELAY_ENV_CTX with additional
# -c relay:* flags derived from the deployment: and auth: blocks.  Env-var overrides
# always win over the file value (the caller checks that before appending).
# Sets global _RELAY_ENV_CTX (empty string when nothing to add).
relay_load_env_config() {
  _RELAY_ENV_CTX=""

  # Locate environments.yaml: prefer RELAY_CONFIG_DIR, fall back to in-repo config/.
  local _env_yaml=""
  if [[ -f "${RELAY_CONFIG_DIR:-${HOME}/.relay/config}/environments.yaml" ]]; then
    _env_yaml="${RELAY_CONFIG_DIR:-${HOME}/.relay/config}/environments.yaml"
  elif [[ -f "${RELAY_ROOT}/config/environments.yaml" ]]; then
    _env_yaml="${RELAY_ROOT}/config/environments.yaml"
  else
    return 0
  fi

  # Require python3 + PyYAML for YAML parsing.
  if ! command -v python3 >/dev/null 2>&1; then
    echo "WARN: python3 not found; skipping environments.yaml config load" >&2
    return 0
  fi

  local _parsed
  _parsed="$(python3 -c "
import sys
try:
    import yaml
except ImportError:
    print('WARN: PyYAML not available; skipping environments.yaml config load', file=sys.stderr)
    sys.exit(0)

with open('${_env_yaml}') as f:
    data = yaml.safe_load(f) or {}

dep = data.get('deployment') or {}
auth = data.get('auth') or {}
ac = auth.get('access_control') or {}

def emit(key, val):
    if val is None:
        return
    if isinstance(val, bool):
        print(f'{key}={str(val).lower()}')
    elif isinstance(val, list):
        joined = ','.join(str(v) for v in val)
        if joined:
            print(f'{key}={joined}')
    else:
        print(f'{key}={val}')

emit('ENV_PHZ_ID',             dep.get('private_hosted_zone_id'))
emit('ENV_PHZ_NAME',           dep.get('private_hosted_zone_name'))
emit('ENV_ALB_SUBDOMAIN',      dep.get('alb_subdomain'))
emit('ENV_CERT_ARN',           dep.get('certificate_arn'))
emit('ENV_INTERNAL_ALB',       dep.get('internal_alb'))
emit('ENV_AUTH_MODE',          auth.get('mode'))
emit('ENV_ACCESS_CONTROL',     ac.get('enabled'))
emit('ENV_AUTH_ALLOWED_USERS', ac.get('allowed_users'))
" 2>&1)" || {
    echo "WARN: environments.yaml parse failed; skipping config load" >&2
    return 0
  }

  # Warn lines from stderr are already printed; filter them from the output.
  local ENV_PHZ_ID="" ENV_PHZ_NAME="" ENV_ALB_SUBDOMAIN="" ENV_CERT_ARN=""
  local ENV_INTERNAL_ALB="" ENV_AUTH_MODE="" ENV_ACCESS_CONTROL="" ENV_AUTH_ALLOWED_USERS=""

  while IFS='=' read -r _k _v; do
    [[ -z "${_k}" ]] && continue
    # Skip warning lines that leaked through stdout.
    [[ "${_k}" == WARN:* ]] && continue
    case "${_k}" in
      ENV_PHZ_ID)             ENV_PHZ_ID="${_v}" ;;
      ENV_PHZ_NAME)           ENV_PHZ_NAME="${_v}" ;;
      ENV_ALB_SUBDOMAIN)      ENV_ALB_SUBDOMAIN="${_v}" ;;
      ENV_CERT_ARN)           ENV_CERT_ARN="${_v}" ;;
      ENV_INTERNAL_ALB)       ENV_INTERNAL_ALB="${_v}" ;;
      ENV_AUTH_MODE)          ENV_AUTH_MODE="${_v}" ;;
      ENV_ACCESS_CONTROL)     ENV_ACCESS_CONTROL="${_v}" ;;
      ENV_AUTH_ALLOWED_USERS) ENV_AUTH_ALLOWED_USERS="${_v}" ;;
    esac
  done <<< "${_parsed}"

  # Env-var overrides win; only inject from file when the env var is unset.
  [ -z "${RELAY_PHZ_ID:-}"             ] && [ -n "${ENV_PHZ_ID}"             ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:phz_id=${ENV_PHZ_ID}"
  [ -z "${RELAY_PHZ_NAME:-}"           ] && [ -n "${ENV_PHZ_NAME}"           ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:phz_name=${ENV_PHZ_NAME}"
  [ -z "${RELAY_ALB_SUBDOMAIN:-}"      ] && [ -n "${ENV_ALB_SUBDOMAIN}"      ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:alb_subdomain=${ENV_ALB_SUBDOMAIN}"
  [ -z "${RELAY_CERT_ARN:-}"           ] && [ -n "${ENV_CERT_ARN}"           ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:certificate_arn=${ENV_CERT_ARN}"
  [ -z "${RELAY_INTERNAL_ALB:-}"       ] && [ -n "${ENV_INTERNAL_ALB}"       ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:internal_alb=${ENV_INTERNAL_ALB}"
  [ -z "${RELAY_UI_AUTH_MODE:-}"       ] && [ -n "${ENV_AUTH_MODE}"          ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:auth_mode=${ENV_AUTH_MODE}"
  [ -z "${RELAY_ACCESS_CONTROL:-}"     ] && [ -n "${ENV_ACCESS_CONTROL}"     ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:access_control=${ENV_ACCESS_CONTROL}"
  [ -z "${RELAY_AUTH_ALLOWED_USERS:-}" ] && [ -n "${ENV_AUTH_ALLOWED_USERS}" ] && _RELAY_ENV_CTX="${_RELAY_ENV_CTX} -c relay:auth_allowed_users=${ENV_AUTH_ALLOWED_USERS}"

  # Always succeed: the final && chain above evaluates false when the optional
  # value is unset, which would otherwise make this function return non-zero and
  # abort the caller under `set -e` (it's invoked as a bare statement).
  return 0
}

# Assemble the -c relay:* context flags + the target stack list into globals
# RELAY_CDK_CONTEXT and RELAY_STACKS, validating required inputs per role.
relay_build_context() {
  # relay:role is appended below once the topology is normalized.
  local ctx="-c relay:aws_account=${AWS_ACCOUNT_ID} -c relay:aws_region=${AWS_REGION}"
  _RELAY_ENV_CTX=""
  relay_load_env_config
  ctx="${ctx}${_RELAY_ENV_CTX}"
  ctx="${ctx} -c relay:gitlab_secret_name=${RELAY_GITLAB_SECRET_NAME}"
  [ -n "${RELAY_GITLAB_REPO:-}" ] && ctx="${ctx} -c relay:gitlab_repo=${RELAY_GITLAB_REPO}"
  [ -n "${RELAY_SERVICENOW_INSTANCE:-}" ] && ctx="${ctx} -c relay:servicenow_instance=${RELAY_SERVICENOW_INSTANCE}"
  # Hub container image (hub/standalone). From scripts/relay-build-hub-image.sh.
  [ -n "${RELAY_HUB_IMAGE_URI:-}" ] && ctx="${ctx} -c relay:hub_image_uri=${RELAY_HUB_IMAGE_URI}"
  # UI auth mode (hub/standalone): none | alb | dev. When unset the stack picks
  # an environment-aware default — prod boards stay read-only (none), non-prod
  # boards come up write-capable (dev). Set this to override either default.
  [ -n "${RELAY_UI_AUTH_MODE:-}" ] && ctx="${ctx} -c relay:auth_mode=${RELAY_UI_AUTH_MODE}"
  [ -n "${RELAY_UI_DEV_USER:-}" ] && ctx="${ctx} -c relay:dev_user=${RELAY_UI_DEV_USER}"
  # Hub config source (hub/standalone): "local" loads bundled config/*.yaml.
  [ -n "${RELAY_HUB_CONFIG_SOURCE:-}" ] && ctx="${ctx} -c relay:config_source=${RELAY_HUB_CONFIG_SOURCE}"
  # Team wall-clock timezone (IANA) for on-call schedule resolution; default UTC.
  [ -n "${RELAY_TZ:-}" ] && ctx="${ctx} -c relay:tz=${RELAY_TZ}"
  # Node self-identity (heartbeat + tile key). Default to team_name/unrouted when
  # unset; set these to align the Node's heartbeat tile with the deployment its
  # alarms resolve to in the bundled catalog (env/deployment_id form the tile key).
  [ -n "${RELAY_NODE_DEPLOYMENT_ID:-}" ] && ctx="${ctx} -c relay:deployment_id=${RELAY_NODE_DEPLOYMENT_ID}"
  [ -n "${RELAY_NODE_APP_NAME:-}" ] && ctx="${ctx} -c relay:app_name=${RELAY_NODE_APP_NAME}"
  [ -n "${RELAY_NODE_ENVIRONMENT:-}" ] && ctx="${ctx} -c relay:environment=${RELAY_NODE_ENVIRONMENT}"
  # AI augmentation (briefings + AARs); off unless "true".
  [ -n "${RELAY_AI_ENABLED:-}" ] && ctx="${ctx} -c relay:ai_enabled=${RELAY_AI_ENABLED}"
  [ -n "${RELAY_AI_MODEL_ID:-}" ] && ctx="${ctx} -c relay:ai_model_id=${RELAY_AI_MODEL_ID}"
  # AI provider: bedrock (default, in-AWS) | bedrock-converse | openai (OpenAI-
  # compatible: OpenAI/Azure/Gemini-compat/OpenRouter/Ollama/vLLM). The openai
  # provider also needs a base URL and (usually) an API-key secret name.
  [ -n "${RELAY_AI_PROVIDER:-}" ] && ctx="${ctx} -c relay:ai_provider=${RELAY_AI_PROVIDER}"
  [ -n "${RELAY_AI_BASE_URL:-}" ] && ctx="${ctx} -c relay:ai_base_url=${RELAY_AI_BASE_URL}"
  # Secrets Manager secret NAME holding the provider API key (never the key
  # itself). When set, the Hub task role is granted read on it.
  [ -n "${RELAY_AI_API_KEY_SECRET:-}" ] && ctx="${ctx} -c relay:ai_api_key_secret=${RELAY_AI_API_KEY_SECRET}"
  # Direct-to-phone SMS IAM grant (test page / targeted pages); off unless "true".
  [ -n "${RELAY_ENABLE_DIRECT_SMS:-}" ] && ctx="${ctx} -c relay:enable_direct_sms=${RELAY_ENABLE_DIRECT_SMS}"
  # Container log level (LOG_LEVEL in the container). Default INFO in the stack.
  [ -n "${RELAY_LOG_LEVEL:-}" ] && ctx="${ctx} -c relay:log_level=${RELAY_LOG_LEVEL}"
  # ALB exposure: unset => stack default (internal). Pass through only when set so
  # the stack's internal-by-default behavior stays authoritative for everyone else.
  [ -n "${RELAY_INTERNAL_ALB:-}" ] && ctx="${ctx} -c relay:internal_alb=${RELAY_INTERNAL_ALB}"

  # Normalize deprecated topology names to the two canonical ones:
  #   team (Node + local Hub, the default) <- standalone, node
  #   federated-hub (always-on upstream aggregator) <- hub
  case "${RELAY_DEPLOY_TYPE}" in
    standalone|node) _topology="team" ;;
    hub)             _topology="federated-hub" ;;
    team|federated-hub) _topology="${RELAY_DEPLOY_TYPE}" ;;
    *) echo "ERROR: unknown RELAY_DEPLOY_TYPE='${RELAY_DEPLOY_TYPE}' (team|federated-hub)" >&2; exit 1 ;;
  esac

  case "${_topology}" in
    team)
      [ -z "${RELAY_TEAM_NAME:-}" ] && { echo "ERROR: RELAY_TEAM_NAME required for team" >&2; exit 1; }
      ctx="${ctx} -c relay:team_name=${RELAY_TEAM_NAME}"
      [ -n "${RELAY_HUB_SCOPE:-}" ] && ctx="${ctx} -c relay:hub_scope=${RELAY_HUB_SCOPE}"
      # Upstream federated-hub bus to forward SEV1/2 up to (optional).
      # RELAY_CENTRAL_HUB_BUS_ARN kept as a deprecated alias for the same value.
      _upstream="${RELAY_UPSTREAM_HUB_BUS_ARN:-${RELAY_CENTRAL_HUB_BUS_ARN:-}}"
      [ -n "${_upstream}" ] && ctx="${ctx} -c relay:central_hub_bus_arn=${_upstream}"
      # Collapsed topology: durable data plane + always-on container. Data first
      # (the compute stack imports it); RELAY_STACK_SELECTOR can narrow to one.
      RELAY_STACKS="RelayDataStack RelayComputeStack"
      ;;
    federated-hub)
      [ -z "${RELAY_ORG_ID:-}" ] && { echo "ERROR: RELAY_ORG_ID required for federated-hub" >&2; exit 1; }
      ctx="${ctx} -c relay:org_id=${RELAY_ORG_ID}"
      # The aggregator also runs the bus teams forward up to (federation stack).
      RELAY_STACKS="RelayDataStack RelayComputeStack RelayFederationStack"
      ;;
  esac
  # Pass the canonical role through to the CDK app.
  ctx="${ctx} -c relay:role=${_topology}"

  # Optional narrowing: RELAY_STACK_SELECTOR=data|compute|federation deploys ONE
  # stack (the "independent starting points" of the collapse — plan §5). Default
  # is the full topology set above.
  case "${RELAY_STACK_SELECTOR:-}" in
    data)       RELAY_STACKS="RelayDataStack" ;;
    compute)    RELAY_STACKS="RelayComputeStack" ;;
    federation) RELAY_STACKS="RelayFederationStack" ;;
    ""|all)     : ;;  # keep the topology default
    *) echo "ERROR: unknown RELAY_STACK_SELECTOR='${RELAY_STACK_SELECTOR}' (data|compute|federation|all)" >&2; exit 1 ;;
  esac

  # The compute stack's real-image guard (compute_stack.py) only matters when the
  # compute stack is an actual deploy target. For a data-only (or federation-only)
  # deploy the compute stack is still constructed by infra/app.py but never
  # deployed (relay-deploy.sh runs --exclusively), so requiring a real ECR image
  # would needlessly block the documented "data plane first" step — which is the
  # only step a locked-down account (PassRole/CreateRole denied) can run at all.
  # Skip the guard when compute is out of scope; keep it on when compute deploys.
  case " ${RELAY_STACKS} " in
    *" RelayComputeStack "*) : ;;                       # compute deploys → keep guard
    *) ctx="${ctx} -c relay:image_check=false" ;;       # no compute → image not required
  esac

  export RELAY_CDK_CONTEXT="${ctx}" RELAY_STACKS
  echo "Stacks: ${RELAY_STACKS}" >&2
  echo "Context: ${RELAY_CDK_CONTEXT}" >&2
}
