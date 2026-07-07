#!/usr/bin/env bash
# relay-build-hub-image.sh — build the Relay Hub container and push to ECR.
#
# Usage:
#   ./scripts/relay-build-hub-image.sh
#
# Environment variables (all optional; safe defaults shown):
#   AWS_REGION        — target region (default: us-east-1)
#   IMAGE_TAG         — Docker image tag (default: git short SHA)
#   RELAY_CONFIG_DIR  — path to a directory of *.yaml config files to bake into
#                       the image instead of the in-repo config/ defaults.
#                       Use this to ship a team's live, upgrade-safe config
#                       (kept at ~/.relay/config or any path outside the clone)
#                       without ever editing files under version control.
#                       When unset the in-repo config/ directory is used as-is.
#                       The image-internal path is always /app/config (set by
#                       ENV RELAY_CONFIG_DIR in the Dockerfile); this var is only
#                       a build-time source selector — it is NOT passed to CDK.
#
# Outputs (stdout, last line):
#   The fully-qualified ECR image URI. Hand it to the deploy via the
#   RELAY_HUB_IMAGE_URI environment variable — relay-context.sh reads that and
#   turns it into the `-c relay:hub_image_uri=<uri>` context for CDK. (Passing
#   `-c ...` directly to relay-deploy.sh does NOT work — the script does not
#   forward trailing args, so the flag would be silently dropped and the Hub
#   would synth with the amazonlinux placeholder image.)
#
# Example:
#   RELAY_CONFIG_DIR=~/.relay/config \
#   export RELAY_HUB_IMAGE_URI="$(./scripts/relay-build-hub-image.sh | tail -1)"
#   RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> ./scripts/relay-deploy.sh
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
# Default tag is the git short SHA (unique per build) so ECS always pulls a
# fresh image and CDK gets a new task-def revision. Using ":latest" causes ECS
# to reuse the cached image and silently run stale code. Override with IMAGE_TAG.
_GIT_SHA="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --short HEAD 2>/dev/null || echo manual)"
IMAGE_TAG="${IMAGE_TAG:-${_GIT_SHA}}"
REPO_NAME="relay-hub"
LOCAL_TAG="${REPO_NAME}:${IMAGE_TAG}"

# Resolve repo root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Relay Hub image build ===" >&2
echo "Region:   ${AWS_REGION}" >&2
echo "Repo:     ${REPO_NAME}" >&2
echo "Tag:      ${IMAGE_TAG}" >&2
echo "Root:     ${RELAY_ROOT}" >&2

# ---------------------------------------------------------------------------
# 1. (Optional) Stage external config into the repo's config/ for the build.
#
# The Dockerfile does `COPY config/ ./config/` so Docker's build context must
# contain the desired YAML at config/ relative to RELAY_ROOT. Rather than
# adding a second COPY directive or a custom build-context arg (which would
# require a more complex BuildKit setup), we temporarily overlay the external
# *.yaml files onto the repo's config/ directory, run the build, then restore
# the originals via a trap — so the working tree is NEVER permanently changed
# even if the script is interrupted or the build fails.
# ---------------------------------------------------------------------------
_REPO_CONFIG_DIR="${RELAY_ROOT}/config"
_CONFIG_BACKUP_DIR=""

_restore_config() {
  if [ -n "${_CONFIG_BACKUP_DIR}" ] && [ -d "${_CONFIG_BACKUP_DIR}" ]; then
    # Remove any files we overlaid, then put the originals back.
    rm -f "${_REPO_CONFIG_DIR}"/*.yaml
    cp "${_CONFIG_BACKUP_DIR}"/*.yaml "${_REPO_CONFIG_DIR}/" 2>/dev/null || true
    rm -rf "${_CONFIG_BACKUP_DIR}"
    echo "Config restored (backup cleaned up)." >&2
  fi
}
trap _restore_config EXIT

if [ -n "${RELAY_CONFIG_DIR:-}" ] && [ "${RELAY_CONFIG_DIR}" != "${_REPO_CONFIG_DIR}" ]; then
  if [ ! -d "${RELAY_CONFIG_DIR}" ]; then
    echo "ERROR: RELAY_CONFIG_DIR='${RELAY_CONFIG_DIR}' does not exist or is not a directory." >&2
    exit 1
  fi
  echo "Config source: ${RELAY_CONFIG_DIR} (external — will be baked in place of in-repo defaults)" >&2
  # Back up the current repo config/*.yaml so we can restore after the build.
  _CONFIG_BACKUP_DIR="$(mktemp -d)"
  cp "${_REPO_CONFIG_DIR}"/*.yaml "${_CONFIG_BACKUP_DIR}/" 2>/dev/null || true
  # Overlay the external *.yaml files. Non-yaml files (e.g. README) are left
  # alone — the Dockerfile copies the whole config/ dir so they ride along.
  cp "${RELAY_CONFIG_DIR}"/*.yaml "${_REPO_CONFIG_DIR}/"
  echo "Staged $(ls "${RELAY_CONFIG_DIR}"/*.yaml 2>/dev/null | wc -l | tr -d ' ') YAML file(s) from ${RELAY_CONFIG_DIR} into ${_REPO_CONFIG_DIR}" >&2
else
  echo "Config source: ${_REPO_CONFIG_DIR} (in-repo defaults)" >&2
fi

# ---------------------------------------------------------------------------
# 2. Build the Docker image
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Building image ${LOCAL_TAG} ---" >&2
# Bake build provenance into the image so the running Hub can report it.
_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
# --network host: the default bridge network uses a separate netns; in
# environments where the host reaches package mirrors via a local resolver or
# policy route that the bridge does not inherit, `apt-get update` inside the
# build hangs while the host itself has connectivity. Host networking sidesteps
# that. It only affects build-time RUN steps (the pushed image is identical) and
# is Linux-only — macOS Docker Desktop silently ignores it (the VM's network,
# which already works, is used). Export RELAY_BUILD_NETWORK="" to disable, or set
# it to an alternate flag string (e.g. "--network default") to override.
docker build \
  ${RELAY_BUILD_NETWORK---network host} \
  --build-arg "RELAY_BUILD_SHA=${IMAGE_TAG}" \
  --build-arg "RELAY_BUILD_TIME=${_BUILD_TIME}" \
  -t "${LOCAL_TAG}" "${RELAY_ROOT}"
echo "Build complete: ${LOCAL_TAG} (sha=${IMAGE_TAG} time=${_BUILD_TIME})" >&2

# ---------------------------------------------------------------------------
# 3. Resolve AWS account ID
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Resolving AWS account ID ---" >&2
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account: ${AWS_ACCOUNT_ID}" >&2

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO_URI="${ECR_REGISTRY}/${REPO_NAME}"
ECR_IMAGE_URI="${ECR_REPO_URI}:${IMAGE_TAG}"

# ---------------------------------------------------------------------------
# 4. Ensure the ECR repository exists
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Ensuring ECR repository '${REPO_NAME}' exists ---" >&2
if aws ecr describe-repositories \
      --repository-names "${REPO_NAME}" \
      --region "${AWS_REGION}" \
      --query 'repositories[0].repositoryUri' \
      --output text >/dev/null 2>&1; then
  echo "ECR repository already exists: ${ECR_REPO_URI}" >&2
else
  echo "Creating ECR repository: ${REPO_NAME}" >&2
  aws ecr create-repository \
    --repository-name "${REPO_NAME}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256 \
    --query 'repository.repositoryUri' \
    --output text
fi

# ---------------------------------------------------------------------------
# 5. Authenticate Docker to ECR
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Logging into ECR ---" >&2
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
echo "ECR login successful" >&2

# ---------------------------------------------------------------------------
# 6. Tag and push
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Tagging and pushing ${ECR_IMAGE_URI} ---" >&2
docker tag "${LOCAL_TAG}" "${ECR_IMAGE_URI}"
docker push "${ECR_IMAGE_URI}"
echo "Push complete" >&2

# ---------------------------------------------------------------------------
# 7. Print the pushed image URI (last line — captured by callers)
# ---------------------------------------------------------------------------
echo "" >&2
echo "=== Done. Pushed image URI: ===" >&2
echo "${ECR_IMAGE_URI}"
