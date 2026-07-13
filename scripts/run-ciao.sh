#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

# Bootstrap: ensure all system-level dependencies, Python venv, pip packages,
# Node.js, npm packages, and PWA build are present and up to date.
# This runs once on initial invocation; hot restarts (exit 75) skip it.
"${ROOT_DIR}/scripts/ensure-deps.sh" || { echo "Dependency setup failed."; exit 1; }

source "${VENV_DIR}/bin/activate"

[[ -f "${ROOT_DIR}/.env" ]] && { set -a; source "${ROOT_DIR}/.env"; set +a; }
[[ -n "${PWA_AUTH_TOKEN:-}" ]] || { echo "PWA_AUTH_TOKEN is not set."; exit 1; }

command -v claude >/dev/null || { echo "claude CLI not on PATH."; exit 1; }

cd "${ROOT_DIR}"

# Restart loop: exit code 75 = restart requested
while true; do
  # Re-source .env on every iteration so Deploy (exit 75) picks up new keys
  # without needing a full launchctl unload/load.
  [[ -f "${ROOT_DIR}/.env" ]] && { set -a; source "${ROOT_DIR}/.env"; set +a; }
  python -m ciao.main &
  child_pid=$!
  wait "${child_pid}" && exit_code=0 || exit_code=$?
  [[ "${exit_code}" -eq 75 ]] && { echo "Restart requested, restarting..."; sleep 1; continue; }
  echo "ciao exited with code ${exit_code}" >&2
  exit "${exit_code}"
done
