#!/usr/bin/env bash
# install.sh — Relay one-shot installer.
#
# Designed to be run as:
#   curl -fsSL https://raw.githubusercontent.com/Westport-Partners/relay/main/install.sh | bash
# or locally:
#   ./install.sh [flags]
#
# Flags:
#   --dir <path>         Clone location (default: ~/relay; env: RELAY_HOME)
#   --ref <git-ref>      Branch, tag, or SHA to check out (default: main)
#   --config-dir <path>  Live team config dir (default: ~/.relay/config; env: RELAY_CONFIG_DIR)
#   --no-deps            Skip tooling install checks
#   --yes / -y           Non-interactive; skip consent prompts
#   --help               Show this message and exit
#
# Supported: Linux x86_64 and aarch64/arm64 only.
set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RELAY_REPO_URL="https://github.com/Westport-Partners/relay.git"
RELAY_RAW_BASE="https://raw.githubusercontent.com/Westport-Partners/relay/main"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
RELAY_HOME="${RELAY_HOME:-${HOME}/relay}"
RELAY_CONFIG_DIR="${RELAY_CONFIG_DIR:-${HOME}/.relay/config}"
REF="main"
NO_DEPS=0
YES=0

usage() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        RELAY_HOME="$2";       shift 2 ;;
    --ref)        REF="$2";              shift 2 ;;
    --config-dir) RELAY_CONFIG_DIR="$2"; shift 2 ;;
    --no-deps)    NO_DEPS=1;             shift   ;;
    --yes|-y)     YES=1;                 shift   ;;
    --help|-h)    usage ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "==> $*" >&2; }
warn()  { echo "WARN: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

# Prompt for y/N consent before doing system-level installs.
# Returns 0 if proceeding, 1 if declined.
ask_consent() {
  local prompt="$1"
  if [[ "${YES}" -eq 1 ]]; then
    return 0
  fi
  # If stdin is not a TTY (piped via curl) we cannot interactively prompt.
  if [[ ! -t 0 ]]; then
    return 1
  fi
  printf "%s [y/N] " "${prompt}" >&2
  local reply
  read -r reply
  [[ "${reply}" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
# Step 1: Detect arch and OS
# ---------------------------------------------------------------------------
info "Step 1/6: Detecting architecture and OS"

_UNAME_OS="$(uname -s)"
[[ "${_UNAME_OS}" == "Linux" ]] || die "Relay requires Linux. Detected OS: ${_UNAME_OS}"

_UNAME_ARCH="$(uname -m)"
case "${_UNAME_ARCH}" in
  x86_64)           ARCH="x86_64" ;;
  aarch64|arm64)    ARCH="aarch64" ;;
  *)                die "Unsupported architecture: ${_UNAME_ARCH}. Relay supports x86_64 and aarch64/arm64 only." ;;
esac

info "OS: Linux  Arch: ${ARCH}"

# Detect package manager by probing in order of preference.
PKG_MGR=""
if   command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt-get"
elif command -v dnf     >/dev/null 2>&1; then PKG_MGR="dnf"
elif command -v yum     >/dev/null 2>&1; then PKG_MGR="yum"
elif command -v apk     >/dev/null 2>&1; then PKG_MGR="apk"
elif command -v pacman  >/dev/null 2>&1; then PKG_MGR="pacman"
fi

if [[ -n "${PKG_MGR}" ]]; then
  info "Package manager: ${PKG_MGR}"
else
  warn "No supported package manager found (apt-get/dnf/yum/apk/pacman). Tooling installs will be skipped."
fi

# ---------------------------------------------------------------------------
# Step 2: Ensure baseline tooling (unless --no-deps)
# ---------------------------------------------------------------------------
if [[ "${NO_DEPS}" -eq 1 ]]; then
  info "Step 2/6: Skipping dependency checks (--no-deps)"
else
  info "Step 2/6: Checking baseline tooling"

  # --- Helper: install a package via the detected PKG_MGR ---
  pkg_install() {
    local pkg="$1"
    local label="${2:-${pkg}}"
    if [[ -z "${PKG_MGR}" ]]; then
      warn "Cannot install ${label}: no package manager detected. Please install it manually."
      return 1
    fi
    info "Installing ${label} via ${PKG_MGR} ..."
    case "${PKG_MGR}" in
      apt-get) sudo apt-get install -y "${pkg}" ;;
      dnf)     sudo dnf install -y "${pkg}" ;;
      yum)     sudo yum install -y "${pkg}" ;;
      apk)     sudo apk add --no-cache "${pkg}" ;;
      pacman)  sudo pacman -S --noconfirm "${pkg}" ;;
    esac
  }

  # --- Helper: print manual install guidance then exit ---
  manual_and_exit() {
    local tool="$1"; shift
    echo "" >&2
    echo "  Relay requires '${tool}' but could not install it automatically" >&2
    echo "  because this script is running non-interactively (piped via curl)" >&2
    echo "  and --yes was not passed." >&2
    echo "" >&2
    echo "  Please install it manually:" >&2
    for line in "$@"; do
      echo "    ${line}" >&2
    done
    echo "" >&2
    echo "  Then re-run the installer:" >&2
    echo "    curl -fsSL ${RELAY_RAW_BASE}/install.sh | bash -s -- --yes" >&2
    echo "" >&2
    exit 1
  }

  # --- git ---
  if ! command -v git >/dev/null 2>&1; then
    if ask_consent "Install git?"; then
      pkg_install git git
    else
      manual_and_exit git \
        "apt-get install -y git" \
        "dnf install -y git"
    fi
  else
    info "git: $(git --version)"
  fi

  # --- curl ---
  if ! command -v curl >/dev/null 2>&1; then
    if ask_consent "Install curl?"; then
      pkg_install curl curl
    else
      manual_and_exit curl \
        "apt-get install -y curl" \
        "dnf install -y curl"
    fi
  else
    info "curl: $(curl --version | head -1)"
  fi

  # --- unzip ---
  if ! command -v unzip >/dev/null 2>&1; then
    if ask_consent "Install unzip?"; then
      pkg_install unzip unzip
    else
      manual_and_exit unzip \
        "apt-get install -y unzip" \
        "dnf install -y unzip"
    fi
  else
    info "unzip: present"
  fi

  # --- docker ---
  if ! command -v docker >/dev/null 2>&1; then
    if ask_consent "Install Docker (via get.docker.com script)?"; then
      curl -fsSL https://get.docker.com | sudo sh
    else
      manual_and_exit docker \
        "curl -fsSL https://get.docker.com | sudo sh" \
        "  — or follow https://docs.docker.com/engine/install/"
    fi
  else
    info "docker: $(docker --version)"
  fi

  # --- node >= 18 ---
  _NODE_OK=0
  if command -v node >/dev/null 2>&1; then
    _NODE_VER="$(node --version | sed 's/^v//' | cut -d. -f1)"
    if [[ "${_NODE_VER}" -ge 18 ]] 2>/dev/null; then
      _NODE_OK=1
      info "node: $(node --version)"
    else
      warn "node $(node --version) is below minimum v18"
    fi
  fi
  if [[ "${_NODE_OK}" -eq 0 ]]; then
    _NODE_INSTALLED=0
    if ask_consent "Install Node.js >= 18?"; then
      # Try NodeSource setup script (works on apt-get / dnf / yum distros).
      if [[ "${PKG_MGR}" == "apt-get" ]]; then
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - \
          && sudo apt-get install -y nodejs && _NODE_INSTALLED=1
      elif [[ "${PKG_MGR}" == "dnf" || "${PKG_MGR}" == "yum" ]]; then
        curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash - \
          && sudo "${PKG_MGR}" install -y nodejs && _NODE_INSTALLED=1
      elif [[ "${PKG_MGR}" == "apk" ]]; then
        sudo apk add --no-cache nodejs npm && _NODE_INSTALLED=1
      elif [[ "${PKG_MGR}" == "pacman" ]]; then
        sudo pacman -S --noconfirm nodejs npm && _NODE_INSTALLED=1
      fi
      if [[ "${_NODE_INSTALLED}" -eq 0 ]]; then
        warn "Could not install Node.js automatically on this distro."
        warn "Install Node >= 18 manually from https://nodejs.org then re-run the installer."
        warn "Continuing — preflight will flag this if it is required."
      fi
    else
      warn "Skipping Node.js install. Install Node >= 18 from https://nodejs.org"
      warn "Continuing — preflight will flag this if it is required."
    fi
  fi

  # --- python3 >= 3.12 ---
  _PY_OK=0
  if command -v python3 >/dev/null 2>&1; then
    # Extract major.minor as two integers for comparison.
    _PY_MAJ="$(python3 --version 2>&1 | sed 's/Python //' | cut -d. -f1)"
    _PY_MIN="$(python3 --version 2>&1 | sed 's/Python //' | cut -d. -f2)"
    if [[ "${_PY_MAJ}" -gt 3 ]] || { [[ "${_PY_MAJ}" -eq 3 ]] && [[ "${_PY_MIN}" -ge 12 ]]; }; then
      _PY_OK=1
      info "python3: $(python3 --version)"
    else
      warn "python3 $(python3 --version 2>&1) is below minimum 3.12"
    fi
  fi
  if [[ "${_PY_OK}" -eq 0 ]]; then
    if ask_consent "Install Python 3.12+?"; then
      case "${PKG_MGR}" in
        apt-get)
          # Try the deadsnakes PPA on Ubuntu/Debian for a modern Python.
          if command -v add-apt-repository >/dev/null 2>&1; then
            sudo add-apt-repository -y ppa:deadsnakes/ppa \
              && sudo apt-get update \
              && sudo apt-get install -y python3.12 python3.12-venv python3.12-distutils \
              && sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
              || warn "deadsnakes PPA install failed; falling back to distro python3"
          else
            sudo apt-get install -y python3 python3-venv || true
          fi ;;
        dnf|yum)
          sudo "${PKG_MGR}" install -y python3.12 python3.12-pip || \
            sudo "${PKG_MGR}" install -y python3 python3-pip || true ;;
        apk)    sudo apk add --no-cache python3 py3-pip || true ;;
        pacman) sudo pacman -S --noconfirm python python-pip || true ;;
      esac
    else
      warn "Skipping Python install. Install python3 >= 3.12 manually."
      warn "Continuing — preflight will flag this if it is required."
    fi
  fi

  # --- AWS CLI v2 ---
  if ! command -v aws >/dev/null 2>&1; then
    if ask_consent "Install AWS CLI v2?"; then
      _AWS_TMP="$(mktemp -d)"
      if [[ "${ARCH}" == "aarch64" ]]; then
        _AWS_ZIP_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
      else
        _AWS_ZIP_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
      fi
      info "Downloading AWS CLI v2 from ${_AWS_ZIP_URL} ..."
      curl -fsSL "${_AWS_ZIP_URL}" -o "${_AWS_TMP}/awscli.zip"
      unzip -q "${_AWS_TMP}/awscli.zip" -d "${_AWS_TMP}"
      sudo "${_AWS_TMP}/aws/install"
      rm -rf "${_AWS_TMP}"
      info "AWS CLI v2 installed: $(aws --version)"
    else
      manual_and_exit "aws CLI v2" \
        "# x86_64:" \
        "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o awscliv2.zip" \
        "unzip awscliv2.zip && sudo ./aws/install" \
        "# aarch64:" \
        "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o awscliv2.zip" \
        "unzip awscliv2.zip && sudo ./aws/install"
    fi
  else
    info "aws: $(aws --version)"
  fi

fi  # end --no-deps block

# ---------------------------------------------------------------------------
# Step 3: Clone or update the Relay repo
# ---------------------------------------------------------------------------
info "Step 3/6: Cloning/updating repo to ${RELAY_HOME} (ref: ${REF})"

if [[ -d "${RELAY_HOME}" ]]; then
  # Directory exists — validate it is our repo before touching it.
  if [[ -d "${RELAY_HOME}/.git" ]]; then
    _REMOTE="$(git -C "${RELAY_HOME}" remote get-url origin 2>/dev/null || true)"
    if [[ "${_REMOTE}" != "${RELAY_REPO_URL}" ]]; then
      die "${RELAY_HOME} is a git repo but its origin remote (${_REMOTE}) does not match ${RELAY_REPO_URL}. Aborting to avoid overwriting unrelated code. Use --dir to specify a different path."
    fi
    info "${RELAY_HOME} already exists and is our repo — fetching and checking out ${REF} ..."
    git -C "${RELAY_HOME}" fetch origin
    git -C "${RELAY_HOME}" checkout "${REF}"
    # Fast-forward if this is a branch (ignore errors on detached SHA/tags).
    git -C "${RELAY_HOME}" pull --ff-only origin "${REF}" 2>/dev/null || true
  else
    # Non-empty non-git directory — refuse.
    if [[ -n "$(ls -A "${RELAY_HOME}" 2>/dev/null)" ]]; then
      die "${RELAY_HOME} exists and is not empty, and is not a git repo. Use --dir to specify a different path."
    fi
    # Empty directory — proceed with clone.
    git clone --branch "${REF}" "${RELAY_REPO_URL}" "${RELAY_HOME}"
  fi
else
  git clone --branch "${REF}" "${RELAY_REPO_URL}" "${RELAY_HOME}"
fi

info "Repo ready at ${RELAY_HOME}"

# ---------------------------------------------------------------------------
# Step 4: Python venv and pip install
# ---------------------------------------------------------------------------
info "Step 4/6: Setting up Python venv and installing Relay package"

VENV_DIR="${RELAY_HOME}/.venv"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "${RELAY_HOME}"

info "Python venv ready: ${VENV_DIR}"

# ---------------------------------------------------------------------------
# Step 5: Seed config files
# ---------------------------------------------------------------------------
info "Step 5/6: Seeding config in ${RELAY_CONFIG_DIR}"

mkdir -p "${RELAY_CONFIG_DIR}"

# Templates → required live files (add more pairs here if needed)
declare -A CONFIG_TEMPLATES
CONFIG_TEMPLATES["escalation"]="escalation"
CONFIG_TEMPLATES["routing"]="routing"
CONFIG_TEMPLATES["environments"]="environments"

for key in "${!CONFIG_TEMPLATES[@]}"; do
  _src="${RELAY_HOME}/config/${key}.example.yaml"
  _dst="${RELAY_CONFIG_DIR}/${CONFIG_TEMPLATES[${key}]}.yaml"
  if [[ ! -f "${_src}" ]]; then
    warn "Template not found: ${_src} — skipping"
    continue
  fi
  if [[ -f "${_dst}" ]]; then
    info "  PRESERVED (already exists): ${_dst}"
  else
    cp "${_src}" "${_dst}"
    info "  SEEDED: ${_dst}"
  fi
done

# ---------------------------------------------------------------------------
# Step 6: Run preflight
# ---------------------------------------------------------------------------
info "Step 6/6: Running preflight checks"

_PREFLIGHT="${RELAY_HOME}/scripts/relay-preflight.sh"

if [[ ! -x "${_PREFLIGHT}" ]]; then
  chmod +x "${_PREFLIGHT}" 2>/dev/null || true
fi

# Run preflight; capture its exit code without letting set -e kill us here.
_PREFLIGHT_RC=0
bash "${_PREFLIGHT}" >&2 || _PREFLIGHT_RC=$?

# relay-preflight.sh exits 1 only on a hard FAIL (missing tool, bad creds);
# WARN-level findings keep exit 0, so a WARN never aborts the install.
if [[ "${_PREFLIGHT_RC}" -eq 1 ]]; then
  echo "" >&2
  echo "  Preflight reported one or more hard failures (see above)." >&2
  echo "  Resolve them before deploying." >&2
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "" >&2
echo "========================================" >&2
echo "  Relay installation complete" >&2
echo "  Relay home:   ${RELAY_HOME}" >&2
echo "  Config dir:   ${RELAY_CONFIG_DIR}" >&2
echo "  Ref:          ${REF}" >&2
echo "========================================" >&2
echo "" >&2
echo "Next steps (team install — deploys the Node + a local Hub together):" >&2
echo "  1. Edit your config files in ${RELAY_CONFIG_DIR}" >&2
echo "     - escalation.yaml  (escalation policies — page by role)" >&2
echo "     - routing.yaml     (alarm-to-policy routing rules)" >&2
echo "  2. Synthesize the CDK app (defaults to the 'team' topology):" >&2
echo "     RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> ${RELAY_HOME}/scripts/relay-synth.sh" >&2
echo "  3. Deploy to AWS (one container in your own account):" >&2
echo "     RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> ${RELAY_HOME}/scripts/relay-deploy.sh" >&2
echo "  4. Full documentation:" >&2
echo "     ${RELAY_HOME}/docs/install.md" >&2
echo "" >&2
