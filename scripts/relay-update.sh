#!/usr/bin/env bash
# relay-update.sh — update an existing Relay clone to a new ref, check
# config drift, and re-run preflight.
#
# Operates on the clone this script lives in; does NOT re-clone.
#
# Usage:
#   ./scripts/relay-update.sh [flags]
#
# Flags:
#   --ref <git-ref>   Branch, tag, or SHA to update to
#                     (default: current branch's upstream, then main)
#   --no-deps         Skip pip re-install
#   --force           Allow update even when the working tree has local changes
#   --help            Show this message and exit
#
# Environment variables:
#   RELAY_CONFIG_DIR  Live team config dir (default: ~/.relay/config)
set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root from this script's location (mirrors relay-context.sh)
# ---------------------------------------------------------------------------
RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
RELAY_CONFIG_DIR="${RELAY_CONFIG_DIR:-${HOME}/.relay/config}"
REF=""
NO_DEPS=0
FORCE=0

usage() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)     REF="$2";  shift 2 ;;
    --no-deps) NO_DEPS=1; shift   ;;
    --force)   FORCE=1;   shift   ;;
    --help|-h) usage ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "==> $*" >&2; }
warn()  { echo "WARN: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 1: Safety — refuse if working tree has uncommitted changes
# ---------------------------------------------------------------------------
info "Step 1/5: Checking for uncommitted local changes"

_DIRTY="$(git -C "${RELAY_ROOT}" status --short 2>/dev/null || true)"
if [[ -n "${_DIRTY}" ]]; then
  if [[ "${FORCE}" -eq 1 ]]; then
    warn "Working tree has uncommitted changes (--force given, continuing):"
    git -C "${RELAY_ROOT}" status --short >&2
  else
    echo "" >&2
    echo "  The Relay working tree has uncommitted changes to tracked files:" >&2
    git -C "${RELAY_ROOT}" status --short >&2
    echo "" >&2
    echo "  To preserve your changes, stash them first:" >&2
    echo "    git -C \"${RELAY_ROOT}\" stash push -m 'pre-update stash'" >&2
    echo "    ./scripts/relay-update.sh ${REF:+--ref ${REF}}" >&2
    echo "    git -C \"${RELAY_ROOT}\" stash pop" >&2
    echo "" >&2
    echo "  Or use --force to update anyway (local changes may be overwritten)." >&2
    exit 1
  fi
else
  info "Working tree is clean."
fi

# ---------------------------------------------------------------------------
# Step 2: git fetch + checkout/pull the target ref
# ---------------------------------------------------------------------------

# Determine the ref to update to if not provided.
if [[ -z "${REF}" ]]; then
  # Try the current branch's upstream tracking ref.
  _UPSTREAM="$(git -C "${RELAY_ROOT}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [[ -n "${_UPSTREAM}" ]]; then
    # Strip the remote name prefix (e.g. "origin/main" → "main").
    REF="${_UPSTREAM#*/}"
    info "Step 2/5: Updating to tracked upstream ref: ${REF}"
  else
    REF="main"
    info "Step 2/5: No upstream tracking ref; defaulting to: ${REF}"
  fi
else
  info "Step 2/5: Updating to: ${REF}"
fi

git -C "${RELAY_ROOT}" fetch origin

# Checkout the desired ref.
git -C "${RELAY_ROOT}" checkout "${REF}"

# Attempt fast-forward pull (works when REF is a branch; silently skipped for
# a detached SHA or tag where pull is not applicable).
_PULL_RC=0
git -C "${RELAY_ROOT}" pull --ff-only origin "${REF}" 2>/dev/null || _PULL_RC=$?
if [[ "${_PULL_RC}" -ne 0 ]]; then
  echo "" >&2
  echo "  Fast-forward pull failed for ref '${REF}'." >&2
  echo "  This usually means your local branch has diverged from the remote." >&2
  echo "  To reset to the remote state (discarding local commits):" >&2
  echo "    git -C \"${RELAY_ROOT}\" reset --hard origin/${REF}" >&2
  echo "  Or merge/rebase manually before retrying." >&2
  exit 1
fi

_NEW_SHA="$(git -C "${RELAY_ROOT}" rev-parse --short HEAD)"
info "Now at ${REF} (${_NEW_SHA})"

# ---------------------------------------------------------------------------
# Step 3: Re-install Python package (unless --no-deps)
# ---------------------------------------------------------------------------
if [[ "${NO_DEPS}" -eq 1 ]]; then
  info "Step 3/5: Skipping pip re-install (--no-deps)"
else
  info "Step 3/5: Re-installing Relay Python package"

  VENV_DIR="${RELAY_ROOT}/.venv"

  if [[ ! -d "${VENV_DIR}" ]]; then
    info "Creating Python venv at ${VENV_DIR} ..."
    python3 -m venv "${VENV_DIR}"
  fi

  "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
  "${VENV_DIR}/bin/pip" install --quiet -e "${RELAY_ROOT}"
  info "Python package updated."
fi

# ---------------------------------------------------------------------------
# Step 4: Config drift check
# ---------------------------------------------------------------------------
info "Step 4/5: Checking config drift against updated templates"

# Templates to inspect (name without .example.yaml / .yaml suffix).
CONFIG_NAMES="escalation routing"

# Python-based top-level key comparison (preferred — uses bundled pyyaml).
_drift_check_python() {
  local template_file="$1"
  local live_file="$2"
  local config_name="$3"

  VENV_DIR="${RELAY_ROOT}/.venv"
  _PY="${VENV_DIR}/bin/python3"
  [[ -x "${_PY}" ]] || _PY="python3"

  "${_PY}" - "${template_file}" "${live_file}" "${config_name}" <<'PYEOF'
import sys

template_path = sys.argv[1]
live_path     = sys.argv[2]
config_name   = sys.argv[3]

try:
    import yaml
except ImportError:
    sys.exit(99)  # signal: fall back to grep

def top_level_keys(path):
    with open(path, 'r') as fh:
        data = yaml.safe_load(fh) or {}
    if isinstance(data, dict):
        return set(data.keys())
    return set()

template_keys = top_level_keys(template_path)
live_keys     = top_level_keys(live_path)
new_keys      = template_keys - live_keys

if new_keys:
    print(f"  CONFIG DRIFT [{config_name}]: the following top-level keys are in the")
    print(f"  updated template but MISSING from your live config:")
    for k in sorted(new_keys):
        print(f"    + {k}")
    print(f"  Review {template_path}")
    print(f"  and add any new settings you want to {live_path}.")
    print(f"  Your existing config has NOT been modified.")
else:
    print(f"  [{config_name}] No config drift detected.")
PYEOF
}

# Grep-based fallback for top-level key comparison.
_drift_check_grep() {
  local template_file="$1"
  local live_file="$2"
  local config_name="$3"

  # Extract lines that start with a letter (top-level YAML keys) and strip trailing colon + whitespace.
  _template_keys="$(grep '^[a-zA-Z]' "${template_file}" | sed 's/:.*//' | sort)"
  _live_keys="$(grep '^[a-zA-Z]' "${live_file}" | sed 's/:.*//' | sort)"

  _new_keys="$(comm -23 <(echo "${_template_keys}") <(echo "${_live_keys}") || true)"

  if [[ -n "${_new_keys}" ]]; then
    echo "  CONFIG DRIFT [${config_name}] (grep-based check — install pyyaml for precise results):" >&2
    echo "  Top-level keys in the updated template but missing from your live config:" >&2
    echo "${_new_keys}" | while IFS= read -r k; do
      echo "    + ${k}" >&2
    done
    echo "  Review ${template_file}" >&2
    echo "  and add any new settings you want to ${live_file}." >&2
    echo "  Your existing config has NOT been modified." >&2
  else
    echo "  [${config_name}] No config drift detected (grep-based check)." >&2
  fi
}

_ANY_DRIFT=0
for _name in ${CONFIG_NAMES}; do
  _template="${RELAY_ROOT}/config/${_name}.example.yaml"
  _live="${RELAY_CONFIG_DIR}/${_name}.yaml"

  if [[ ! -f "${_template}" ]]; then
    warn "Template not found: ${_template} — skipping drift check for ${_name}"
    continue
  fi

  if [[ ! -f "${_live}" ]]; then
    warn "Live config not found: ${_live} — no drift check for ${_name} (run install.sh to seed it)"
    continue
  fi

  # Try Python; fall back to grep if pyyaml is unavailable (exit 99).
  _drift_out=""
  _drift_rc=0
  _drift_out="$(_drift_check_python "${_template}" "${_live}" "${_name}" 2>&1)" || _drift_rc=$?

  if [[ "${_drift_rc}" -eq 99 ]]; then
    warn "pyyaml not available — falling back to grep-based drift check for ${_name}"
    _drift_check_grep "${_template}" "${_live}" "${_name}"
  else
    echo "${_drift_out}" >&2
    # If the python script printed "CONFIG DRIFT", note it for summary.
    if echo "${_drift_out}" | grep -q "CONFIG DRIFT"; then
      _ANY_DRIFT=1
    fi
  fi
done

if [[ "${_ANY_DRIFT}" -eq 1 ]]; then
  warn "Config drift found (see above). Review and update your live config before redeploying."
fi

# ---------------------------------------------------------------------------
# Step 5: Re-run preflight
# ---------------------------------------------------------------------------
info "Step 5/5: Running preflight checks"

_PREFLIGHT="${RELAY_ROOT}/scripts/relay-preflight.sh"

if [[ ! -x "${_PREFLIGHT}" ]]; then
  chmod +x "${_PREFLIGHT}" 2>/dev/null || true
fi

_PREFLIGHT_RC=0
bash "${_PREFLIGHT}" >&2 || _PREFLIGHT_RC=$?

if [[ "${_PREFLIGHT_RC}" -eq 1 ]]; then
  echo "" >&2
  echo "  Preflight reported one or more hard failures (see above)." >&2
  echo "  Resolve them before redeploying." >&2
fi

# ---------------------------------------------------------------------------
# Summary / redeploy reminder
# ---------------------------------------------------------------------------
echo "" >&2
echo "========================================" >&2
echo "  Relay update complete" >&2
echo "  Relay root: ${RELAY_ROOT}" >&2
echo "  Now at:     ${REF} (${_NEW_SHA})" >&2
echo "========================================" >&2
echo "" >&2
echo "To redeploy with the updated code:" >&2
echo "  1. Rebuild the Hub container image:" >&2
echo "     ${RELAY_ROOT}/scripts/relay-build-hub-image.sh" >&2
echo "  2. Deploy to AWS:" >&2
echo "     ${RELAY_ROOT}/scripts/relay-deploy.sh" >&2
echo "" >&2
echo "Note: DynamoDB-backed data (contacts, schedules, incidents) is" >&2
echo "untouched by a code update. Only infrastructure/code changes are applied." >&2
echo "" >&2
