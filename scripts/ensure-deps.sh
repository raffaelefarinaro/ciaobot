#!/usr/bin/env bash
# Central dependency verification and environment setup helper for Ciao.
# This script handles Python venv creation, package updates, Node.js installation (local/system),
# npm package installations, and frontend PWA builds.
#
# macOS-only: ciao now runs solely on macOS (Apple Silicon). Auto-install
# fallbacks use Homebrew; there are no Linux/apt-get branches.
#
# Can be executed directly or sourced (e.g. from run-ciao.sh or dev.sh).

ciao_ensure_deps() {
  local DEV_MODE=0
  local UPGRADE_DEPS=0
  for arg in "$@"; do
    if [[ "$arg" == "--dev" ]]; then
      DEV_MODE=1
    elif [[ "$arg" == "--upgrade" ]]; then
      UPGRADE_DEPS=1
    fi
  done

  local ROOT_DIR
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "${ROOT_DIR}"

  # Design Aesthetics Logging
  log_info() {
    echo -e "\033[0;32m[Ciao Setup] $1\033[0m"
  }
  log_warn() {
    echo -e "\033[0;33m[Ciao Setup] WARNING: $1\033[0m" >&2
  }
  log_err() {
    echo -e "\033[0;31m[Ciao Setup] ERROR: $1\033[0m" >&2
  }

  log_info "Verifying OS and system architecture..."
  local OS
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  local ARCH
  ARCH="$(uname -m)"

  # macOS 26+ ships a libexpat missing symbols that Homebrew Python's pyexpat needs.
  # Point dyld at Homebrew's libexpat before any Python invocation.
  if [[ "$OS" == "darwin" && -d /opt/homebrew/opt/expat/lib ]]; then
    export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
  fi
  local NODE_ARCH="$ARCH"
  if [[ "$ARCH" == "x86_64" ]]; then
    NODE_ARCH="x64"
  elif [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
    NODE_ARCH="arm64"
  fi

  # --- 1. Git Validation ---
  if ! command -v git >/dev/null 2>&1; then
    log_warn "git is not installed. Attempting auto-install via Homebrew..."
    if command -v brew >/dev/null 2>&1; then
      brew install git || { log_err "Failed to install git via Homebrew."; return 1; }
    else
      log_err "git is missing and Homebrew is not available. Install Homebrew (https://brew.sh) or git."
      return 1
    fi
  fi

  # --- 2. Python Validation & Venv Setup ---
  pick_python() {
    if [[ -n "${CIAO_PYTHON:-}" ]]; then echo "${CIAO_PYTHON}"; return; fi
    for cand in python3.13 python3.12 python3.11 python3; do
      if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c "import ensurepip" >/dev/null 2>&1; then
          echo "$cand"; return
        fi
      fi
    done
  }

  local PY_BIN
  PY_BIN=$(pick_python || true)
  if [[ -z "$PY_BIN" ]]; then
    log_warn "Python 3 or ensurepip module is missing. Attempting to install Python 3 via Homebrew..."
    if command -v brew >/dev/null 2>&1; then
      brew install python || { log_err "Failed to install Python via Homebrew."; return 1; }
    else
      log_err "Python 3 is missing and Homebrew is not available. Install Homebrew (https://brew.sh) or python3."
      return 1
    fi

    PY_BIN=$(pick_python || true)
    if [[ -z "$PY_BIN" ]]; then
      log_err "Failed to configure a working Python 3 installation."
      return 1
    fi
  fi

  # Ensure venv exists and is healthy
  if [[ -d .venv && ! -x .venv/bin/python ]]; then
    log_warn "Removing broken virtualenv at .venv..."
    rm -rf .venv
  fi

  if [[ ! -d .venv ]]; then
    log_info "Creating Python virtual environment using ${PY_BIN}..."
    "${PY_BIN}" -m venv .venv || { log_err "Failed to create virtual environment."; return 1; }
  fi

  # macOS 26+ libexpat workaround: ensure activate exports the override so any
  # process that sources .venv/bin/activate (and not this script) still works.
  if [[ "$OS" == "darwin" && -d /opt/homebrew/opt/expat/lib ]]; then
    if ! grep -q "DYLD_LIBRARY_PATH=.*expat" .venv/bin/activate 2>/dev/null; then
      cat >> .venv/bin/activate <<'EOF'

# Ciao: macOS 26+ libexpat workaround (added by scripts/ensure-deps.sh)
if [ -d /opt/homebrew/opt/expat/lib ]; then
    export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi
EOF
    fi
  fi

  # Point stdlib ssl/urllib at certifi's CA bundle. The python.org Python (often
  # picked first on macOS) ships no CA bundle, so bare urllib.request calls
  # (e.g. the OAuth token refresh in ciao/web/auth.py) fail cert verification.
  # certifi is in the venv (via httpx).
  if ! grep -q "SSL_CERT_FILE=.*certifi" .venv/bin/activate 2>/dev/null; then
    cat >> .venv/bin/activate <<'EOF'

# Ciao: use certifi CA bundle for stdlib ssl/urllib (added by scripts/ensure-deps.sh)
_ciao_ca="$(python -c 'import certifi; print(certifi.where())' 2>/dev/null)"
if [ -n "$_ciao_ca" ]; then
    export SSL_CERT_FILE="$_ciao_ca"
fi
unset _ciao_ca
EOF
  fi

  log_info "Activating Python virtual environment..."
  source .venv/bin/activate

  local PIP_UPGRADE_ARG=""
  if [[ "${UPGRADE_DEPS}" -eq 1 ]]; then
    PIP_UPGRADE_ARG="--upgrade"
  fi

  log_info "Upgrading pip, setuptools, and wheel..."
  pip install ${PIP_UPGRADE_ARG} -q pip setuptools wheel || log_warn "Pip self-upgrade failed, continuing..."

  log_info "Installing backend dependencies from pyproject.toml..."
  # Mac-only deployment: voice-local (mlx-whisper, Apple Silicon) is part of the
  # default install. Drop it from the extras if this ever runs on non-arm64.
  pip install -q ${PIP_UPGRADE_ARG} -e '.[test,voice-local]' || { log_err "Failed to install python dependencies."; return 1; }

  log_info "Ensuring key Python packages are installed..."
  pip install -q ${PIP_UPGRADE_ARG} notebooklm-py playwright "starlette<1" || log_warn "Extra pip dependencies setup failed, continuing..."

  log_info "Verifying Playwright browser binaries..."
  # Only run playwright install if chromium is not installed or if upgrading
  if [[ "${UPGRADE_DEPS}" -eq 1 ]] || ! ls ~/Library/Caches/ms-playwright/chromium-* ~/.cache/ms-playwright/chromium-* &>/dev/null; then
    playwright install chromium 2>/dev/null || log_warn "Playwright chromium download failed or skipped (non-fatal)."
  fi

  # --- 3. Node.js & NPM Validation ---
  local NODE_VER_TARGET="v22.11.0"
  local LOCAL_NODE_DIR="${ROOT_DIR}/.node"

  check_node_version() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
      return 1
    fi
    local ver
    ver=$("$cmd" -v | tr -d 'v')
    local major
    major=$(echo "$ver" | cut -d. -f1)
    if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 22 )); then
      return 0
    fi
    return 1
  }

  if check_node_version "node"; then
    log_info "System Node.js version is healthy ($(node -v))."
  elif [[ -x "${LOCAL_NODE_DIR}/bin/node" ]] && check_node_version "${LOCAL_NODE_DIR}/bin/node"; then
    log_info "Local portable Node.js version is healthy ($("${LOCAL_NODE_DIR}/bin/node" -v))."
    export PATH="${LOCAL_NODE_DIR}/bin:${PATH}"
  else
    log_info "System Node.js >= 22 not found. Preparing to download portable Node.js ${NODE_VER_TARGET}..."
    rm -rf "${LOCAL_NODE_DIR}"
    mkdir -p "${LOCAL_NODE_DIR}"

    local TAR_NAME="node-${NODE_VER_TARGET}-${OS}-${NODE_ARCH}.tar.gz"
    local DOWNLOAD_URL="https://nodejs.org/dist/${NODE_VER_TARGET}/${TAR_NAME}"
    local TEMP_TAR="${ROOT_DIR}/.runtime/${TAR_NAME}"
    mkdir -p "${ROOT_DIR}/.runtime"

    log_info "Downloading Node.js package from ${DOWNLOAD_URL}..."
    if command -v curl >/dev/null 2>&1; then
      curl -sfL -o "${TEMP_TAR}" "${DOWNLOAD_URL}" || { log_err "Failed to download Node.js via curl."; return 1; }
    elif command -v wget >/dev/null 2>&1; then
      wget -q -O "${TEMP_TAR}" "${DOWNLOAD_URL}" || { log_err "Failed to download Node.js via wget."; return 1; }
    else
      log_warn "curl and wget not found. Attempting to install curl via Homebrew..."
      if command -v brew >/dev/null 2>&1; then
        brew install curl || { log_err "Failed to install curl."; return 1; }
        curl -sfL -o "${TEMP_TAR}" "${DOWNLOAD_URL}" || { log_err "Failed to download Node.js."; return 1; }
      else
        log_err "Neither curl nor wget is available to download Node.js. Install curl or wget."
        return 1
      fi
    fi

    log_info "Extracting Node.js package locally..."
    tar -xzf "${TEMP_TAR}" -C "${LOCAL_NODE_DIR}" --strip-components=1 || { log_err "Failed to unpack Node.js."; return 1; }
    rm -f "${TEMP_TAR}"

    if [[ -x "${LOCAL_NODE_DIR}/bin/node" ]]; then
      log_info "Successfully installed local portable Node.js ($("${LOCAL_NODE_DIR}/bin/node" -v)) at ${LOCAL_NODE_DIR}"
      export PATH="${LOCAL_NODE_DIR}/bin:${PATH}"
    else
      log_err "Local Node.js installation verification failed."
      return 1
    fi
  fi

  # --- 4. NPM Packages Installation & Update ---
  if [[ -f "package.json" ]]; then
    log_info "Checking root Node packages..."
    npm install --silent --prefer-offline --no-audit --no-fund || log_warn "Root npm install reported errors/warnings."
    if [[ "${UPGRADE_DEPS}" -eq 1 ]]; then
      log_info "Updating root Node packages..."
      npm update --silent --no-audit --no-fund || log_warn "Root npm update reported errors/warnings."
    fi
  fi

  if [[ -d "web" && -f "web/package.json" ]]; then
    log_info "Checking web frontend Node packages..."
    (cd web && npm install --silent --prefer-offline --no-audit --no-fund) || log_warn "Web npm install reported errors/warnings."
    if [[ "${UPGRADE_DEPS}" -eq 1 ]]; then
      log_info "Updating web frontend Node packages..."
      (cd web && npm update --silent --no-audit --no-fund) || log_warn "Web npm update reported errors/warnings."
    fi
  fi

  # --- 5. Compile PWA Frontend (Production build) ---
  if [[ "${DEV_MODE}" -ne 1 ]]; then
    local NEEDS_BUILD=0
    if [[ ! -f "ciao/web/static/index.html" ]]; then
      NEEDS_BUILD=1
    else
      # Rebuild if anything under web/src or web/package.json is newer than the
      # built index.html (BSD find on macOS).
      if [[ -n "$(find -E web/src web/package.json -newer ciao/web/static/index.html 2>/dev/null)" ]]; then
        NEEDS_BUILD=1
      fi
    fi

    if [[ "$NEEDS_BUILD" -eq 1 ]]; then
      log_info "Building PWA frontend..."
      (cd web && npm run build) || { log_err "PWA frontend build failed."; return 1; }
    else
      log_info "PWA frontend is up to date."
    fi
  fi

  log_info "All dependencies verified and up to date."
  return 0
}

ciao_ensure_deps "$@"
