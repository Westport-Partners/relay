#!/usr/bin/env bash
# relay-deploy.sh — deploy the selected Relay topology into the caller's account.
# Assumes CDK is already bootstrapped (see relay-bootstrap.sh). Used by the
# pipeline's deploy job and locally; identical logic either way.
#
# Usage:  RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=westport ./scripts/relay-deploy.sh
#         (RELAY_DEPLOY_TYPE: team | federated-hub; default team)
#   RELAY_REQUIRE_APPROVAL  never | any-change | broadening  (default: never)
#
# Extra arguments are forwarded verbatim to `cdk deploy`, so you can append
# additional `-c key=value` context or other CDK flags, e.g.:
#   ./scripts/relay-deploy.sh -c relay:tz=America/New_York
# NOTE: the Hub container image is supplied via the RELAY_HUB_IMAGE_URI env var
# (relay-context.sh turns it into `-c relay:hub_image_uri=...`), not as a bare
# flag here — though a flag would now be forwarded too.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/relay-context.sh"

RELAY_REQUIRE_APPROVAL="${RELAY_REQUIRE_APPROVAL:-never}"

# cdk.json runs a bare `python3 infra/app.py`; without the venv on PATH that
# fails ModuleNotFoundError: aws_cdk (debug doc gotcha #3). Activate it if present.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate"
fi

relay_resolve_account
relay_build_context

cd "${RELAY_ROOT}"
echo "Deploying: ${RELAY_STACKS}" >&2
# --exclusively: deploy ONLY the named stack(s), never their dependencies. A
# compute deploy can't silently re-deploy (or wedge) the data plane, and a
# narrowed RELAY_STACK_SELECTOR=compute deploy touches exactly one stack
# (kills debug-doc gotcha #2 where a deploy fanned out to a sibling stack).
# shellcheck disable=SC2086
relay_cdk deploy ${RELAY_STACKS} ${RELAY_CDK_CONTEXT} \
  --exclusively \
  --require-approval "${RELAY_REQUIRE_APPROVAL}" \
  --outputs-file cdk.outputs.json \
  "$@"

echo "Deploy complete. Outputs:" >&2
cat cdk.outputs.json
