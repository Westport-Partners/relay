#!/usr/bin/env bash
# relay-setup-oidc.sh — wire an ALB authenticate-oidc action onto the Relay HTTPS
# listener and flip environments.yaml to auth.mode=alb.
#
# Run this once after deploying Relay with a certificate (relay:phz_id/phz_name or
# relay:certificate_arn). It adds an authenticate-oidc default action to the
# HTTPS:443 listener so every request is authenticated before reaching the Hub.
# After running, redeploy the compute stack so the container picks up
# RELAY_AUTH_MODE=alb (a task-definition env var, not baked into the image — no
# rebuild needed for auth; rebuild only if app code or baked config/ changed).
#
# Usage:
#   ./scripts/relay-setup-oidc.sh \
#       --client-id <id> --client-secret <secret> \
#       [--idp github] [--scopes "read:user user:email"] \
#       [--allowed-users "alice,bob"] [--listener-arn <arn>] [--region us-east-1]
#
# Inputs (flags or env fallbacks):
#   --idp                   IdP preset: github (default). Extend for other IdPs.
#   --client-id             OAuth client ID   (required)
#   --client-secret         OAuth client secret (required; also RELAY_OIDC_CLIENT_SECRET)
#   --scopes                Space-separated OIDC scopes (default: openid)
#                           GitHub typically needs: "read:user user:email"
#   --allowed-users         Comma-separated OIDC usernames allowed to WRITE
#                           (enables auth.access_control in environments.yaml)
#   --issuer                Override IdP issuer URL
#   --authorization-endpoint Override IdP authorization endpoint
#   --token-endpoint        Override IdP token endpoint
#   --user-info-endpoint    Override IdP userinfo endpoint
#   --listener-arn          ALB HTTPS listener ARN. Auto-discovered when omitted.
#   --region                AWS region (default: AWS_REGION or us-east-1)
set -euo pipefail

# ---------------------------------------------------------------------------
# Source shared helpers (region default, relay_resolve_account) when available.
# relay-setup-oidc.sh is also runnable standalone without a full Relay clone.
# ---------------------------------------------------------------------------
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_SCRIPT_DIR}/relay-context.sh" ]]; then
  # shellcheck disable=SC1091
  source "${_SCRIPT_DIR}/relay-context.sh"
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
IDP="github"
CLIENT_ID=""
CLIENT_SECRET="${RELAY_OIDC_CLIENT_SECRET:-}"
SCOPES="openid"
ALLOWED_USERS=""
LISTENER_ARN=""
ISSUER=""
AUTHORIZATION_ENDPOINT=""
TOKEN_ENDPOINT=""
USER_INFO_ENDPOINT=""

usage() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --idp)                   IDP="$2";                    shift 2 ;;
    --client-id)             CLIENT_ID="$2";              shift 2 ;;
    --client-secret)         CLIENT_SECRET="$2";          shift 2 ;;
    --scopes)                SCOPES="$2";                 shift 2 ;;
    --allowed-users)         ALLOWED_USERS="$2";          shift 2 ;;
    --issuer)                ISSUER="$2";                 shift 2 ;;
    --authorization-endpoint) AUTHORIZATION_ENDPOINT="$2"; shift 2 ;;
    --token-endpoint)        TOKEN_ENDPOINT="$2";         shift 2 ;;
    --user-info-endpoint)    USER_INFO_ENDPOINT="$2";     shift 2 ;;
    --listener-arn)          LISTENER_ARN="$2";           shift 2 ;;
    --region)                AWS_REGION="$2";             shift 2 ;;
    --help|-h)               usage ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

[[ -z "${CLIENT_ID}" ]]     && { echo "ERROR: --client-id is required" >&2; exit 1; }
[[ -z "${CLIENT_SECRET}" ]] && { echo "ERROR: --client-secret is required (or set RELAY_OIDC_CLIENT_SECRET)" >&2; exit 1; }

# ---------------------------------------------------------------------------
# IdP presets
# ---------------------------------------------------------------------------
case "${IDP}" in
  github)
    ISSUER="${ISSUER:-https://github.com/login/oauth}"
    AUTHORIZATION_ENDPOINT="${AUTHORIZATION_ENDPOINT:-https://github.com/login/oauth/authorize}"
    TOKEN_ENDPOINT="${TOKEN_ENDPOINT:-https://github.com/login/oauth/access_token}"
    USER_INFO_ENDPOINT="${USER_INFO_ENDPOINT:-https://api.github.com/user}"
    ;;
  *)
    # Custom IdP — all four endpoints must be supplied explicitly.
    [[ -z "${ISSUER}" ]]                 && { echo "ERROR: --issuer required for IdP '${IDP}'" >&2; exit 1; }
    [[ -z "${AUTHORIZATION_ENDPOINT}" ]] && { echo "ERROR: --authorization-endpoint required for IdP '${IDP}'" >&2; exit 1; }
    [[ -z "${TOKEN_ENDPOINT}" ]]         && { echo "ERROR: --token-endpoint required for IdP '${IDP}'" >&2; exit 1; }
    [[ -z "${USER_INFO_ENDPOINT}" ]]     && { echo "ERROR: --user-info-endpoint required for IdP '${IDP}'" >&2; exit 1; }
    ;;
esac

echo "=== Relay OIDC setup ===" >&2
echo "IdP:      ${IDP}" >&2
echo "Region:   ${AWS_REGION}" >&2
echo "Scopes:   ${SCOPES}" >&2
[[ -n "${ALLOWED_USERS}" ]] && echo "Allowed users: ${ALLOWED_USERS}" >&2

# ---------------------------------------------------------------------------
# 1. Discover the HTTPS listener ARN (if not provided)
# ---------------------------------------------------------------------------
if [[ -z "${LISTENER_ARN}" ]]; then
  echo "" >&2
  echo "--- Discovering Relay ALB HTTPS listener ---" >&2

  # Find all ALBs with "relay" in the name (case-insensitive via describe filter).
  _ALB_ARNS="$(aws elbv2 describe-load-balancers \
    --region "${AWS_REGION}" \
    --query "LoadBalancers[?contains(LoadBalancerName, 'relay') || contains(LoadBalancerName, 'Relay')].LoadBalancerArn" \
    --output text)"

  if [[ -z "${_ALB_ARNS}" ]]; then
    echo "ERROR: No load balancers with 'relay' in the name found in region ${AWS_REGION}." >&2
    echo "       Deploy Relay first (scripts/relay-deploy.sh), then re-run this script." >&2
    exit 1
  fi

  LISTENER_ARN=""
  for _ALB_ARN in ${_ALB_ARNS}; do
    _HTTPS_ARN="$(aws elbv2 describe-listeners \
      --load-balancer-arn "${_ALB_ARN}" \
      --region "${AWS_REGION}" \
      --query "Listeners[?Port==\`443\`].ListenerArn | [0]" \
      --output text 2>/dev/null || true)"
    if [[ -n "${_HTTPS_ARN}" && "${_HTTPS_ARN}" != "None" ]]; then
      LISTENER_ARN="${_HTTPS_ARN}"
      echo "Found HTTPS listener: ${LISTENER_ARN} (ALB: ${_ALB_ARN})" >&2
      break
    fi
  done

  if [[ -z "${LISTENER_ARN}" ]]; then
    echo "ERROR: No HTTPS (port 443) listener found on any Relay ALB in region ${AWS_REGION}." >&2
    echo "       Deploy Relay with a certificate first:" >&2
    echo "         relay:phz_id + relay:phz_name  — Route53 private hosted zone (issues ACM cert)" >&2
    echo "         relay:certificate_arn           — bring your own ACM cert" >&2
    echo "       Then re-run scripts/relay-deploy.sh and retry this script." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. Read the existing forward target group from the listener
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Reading existing listener configuration ---" >&2

_LISTENER_JSON="$(aws elbv2 describe-listeners \
  --listener-arns "${LISTENER_ARN}" \
  --region "${AWS_REGION}" \
  --output json)"

_TG_ARN="$(LISTENER_JSON="${_LISTENER_JSON}" python3 -c "
import json, os, sys
data = json.loads(os.environ['LISTENER_JSON'])
listeners = data.get('Listeners', [])
if not listeners:
    sys.exit('ERROR: no listener data returned')
actions = listeners[0].get('DefaultActions', [])
for a in actions:
    if a.get('Type') == 'forward':
        tg = a.get('TargetGroupArn') or (a.get('ForwardConfig', {}).get('TargetGroups') or [{}])[0].get('TargetGroupArn', '')
        if tg:
            print(tg)
            sys.exit(0)
sys.exit('ERROR: no forward action found in listener default actions')
" 2>&1)" || { echo "ERROR: could not extract target group ARN: ${_TG_ARN}" >&2; exit 1; }

echo "Target group: ${_TG_ARN}" >&2

# ---------------------------------------------------------------------------
# 3. Build the authenticate-oidc + forward default-actions JSON
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Building authenticate-oidc action ---" >&2

# Values are passed via the environment (NOT string-interpolated) so a secret
# or endpoint containing quotes/specials can never break the snippet or inject.
_ACTIONS_JSON="$(
  OIDC_ISSUER="${ISSUER}" \
  OIDC_AUTH_EP="${AUTHORIZATION_ENDPOINT}" \
  OIDC_TOKEN_EP="${TOKEN_ENDPOINT}" \
  OIDC_USERINFO_EP="${USER_INFO_ENDPOINT}" \
  OIDC_CLIENT_ID="${CLIENT_ID}" \
  OIDC_CLIENT_SECRET="${CLIENT_SECRET}" \
  OIDC_SCOPES="${SCOPES}" \
  OIDC_TG_ARN="${_TG_ARN}" \
  python3 -c "
import json, os

oidc_action = {
    'Type': 'authenticate-oidc',
    'Order': 1,
    'AuthenticateOidcConfig': {
        'Issuer': os.environ['OIDC_ISSUER'],
        'AuthorizationEndpoint': os.environ['OIDC_AUTH_EP'],
        'TokenEndpoint': os.environ['OIDC_TOKEN_EP'],
        'UserInfoEndpoint': os.environ['OIDC_USERINFO_EP'],
        'ClientId': os.environ['OIDC_CLIENT_ID'],
        'ClientSecret': os.environ['OIDC_CLIENT_SECRET'],
        'Scope': os.environ['OIDC_SCOPES'],
        'OnUnauthenticatedRequest': 'authenticate',
        'SessionTimeout': 604800,
        'SessionCookieName': 'AWSELBAuthSessionCookie',
    },
}

forward_action = {
    'Type': 'forward',
    'Order': 2,
    'TargetGroupArn': os.environ['OIDC_TG_ARN'],
}

print(json.dumps([oidc_action, forward_action]))
"
)"

echo "Actions JSON assembled." >&2

# ---------------------------------------------------------------------------
# 4. Apply the authenticate-oidc action to the listener
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Modifying listener ${LISTENER_ARN} ---" >&2

aws elbv2 modify-listener \
  --listener-arn "${LISTENER_ARN}" \
  --region "${AWS_REGION}" \
  --default-actions "${_ACTIONS_JSON}" \
  --output json | python3 -c "
import json, sys
data = json.load(sys.stdin)
state = data.get('Listeners', [{}])[0].get('ListenerArn', '?')
print(f'Listener updated: {state}', file=sys.stderr)
"

echo "ALB OIDC action applied successfully." >&2

# ---------------------------------------------------------------------------
# 5. Update environments.yaml: set auth.mode=alb (and optionally access_control)
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Updating environments.yaml ---" >&2

_RELAY_ROOT="$(cd "${_SCRIPT_DIR}/.." && pwd)"
_ENV_YAML=""
if [[ -f "${RELAY_CONFIG_DIR:-${HOME}/.relay/config}/environments.yaml" ]]; then
  _ENV_YAML="${RELAY_CONFIG_DIR:-${HOME}/.relay/config}/environments.yaml"
elif [[ -f "${_RELAY_ROOT}/config/environments.yaml" ]]; then
  _ENV_YAML="${_RELAY_ROOT}/config/environments.yaml"
fi

if [[ -z "${_ENV_YAML}" ]]; then
  echo "WARN: environments.yaml not found; skipping config update." >&2
  echo "      Set auth.mode: alb manually in your environments.yaml." >&2
else
  ENV_YAML_PATH="${_ENV_YAML}" ALLOWED_USERS_STR="${ALLOWED_USERS}" python3 -c "
import os, sys
try:
    import yaml
except ImportError:
    sys.exit('ERROR: PyYAML not available. Install it (pip install pyyaml) and retry.')

path = os.environ['ENV_YAML_PATH']
allowed_users_str = os.environ['ALLOWED_USERS_STR']

with open(path) as f:
    data = yaml.safe_load(f) or {}

# Ensure auth block exists
if 'auth' not in data or data['auth'] is None:
    data['auth'] = {}

data['auth']['mode'] = 'alb'

if allowed_users_str:
    users = [u.strip() for u in allowed_users_str.split(',') if u.strip()]
    if 'access_control' not in data['auth'] or data['auth']['access_control'] is None:
        data['auth']['access_control'] = {}
    data['auth']['access_control']['enabled'] = True
    data['auth']['access_control']['allowed_users'] = users

with open(path, 'w') as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f'Updated: {path}', file=sys.stderr)
print('NOTE: YAML comments are not preserved on the live config copy.', file=sys.stderr)
"
fi

# ---------------------------------------------------------------------------
# 6. Next steps
# ---------------------------------------------------------------------------
echo "" >&2
echo "========================================" >&2
echo "  OIDC setup complete" >&2
echo "========================================" >&2
echo "" >&2
echo "Next step — redeploy the compute stack so the container picks up" >&2
echo "RELAY_AUTH_MODE=alb (it is a task-definition env var, not baked into the" >&2
echo "image; the redeploy mints a new task-def revision and rolls the task):" >&2
echo "  RELAY_STACK_SELECTOR=compute ${_SCRIPT_DIR}/relay-deploy.sh" >&2
echo "" >&2
echo "No image rebuild is needed for auth — only redeploy. Rebuild" >&2
echo "(${_SCRIPT_DIR}/relay-build-hub-image.sh) is required ONLY if you" >&2
echo "also changed app code or the baked config/ files." >&2
echo "" >&2
